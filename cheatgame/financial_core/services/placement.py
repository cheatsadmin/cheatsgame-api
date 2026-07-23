import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from cheatgame.digital_products.models import (
    DigitalCheckoutLineSnapshot,
    DigitalInventoryReservation,
    DigitalInventoryReservationState,
    InventoryPool,
)
from cheatgame.financial_core.models import (
    FinancialActorType,
    IdempotencyRecord,
    IdempotencyStatus,
    Payment,
    PaymentCollectionStatus,
    PaymentObligationSource,
    PaymentObligationSourceKind,
)
from cheatgame.financial_core.services.events import append_financial_event
from cheatgame.financial_core.services.idempotency import IdempotencyConflict, canonical_request_hash
from cheatgame.financial_core.services.locks import LockRank, lock_many, lock_one, ordered_lock_scope, register_lock
from cheatgame.financial_core.services.money import normalize_obligation_money
from cheatgame.financial_core.services.outbox import append_outbox_message
from cheatgame.product.models import Product, ProductCommerceAuthority, ProductType
from cheatgame.shop.models import (
    Cart,
    CartLockReason,
    CartState,
    Checkout,
    CheckoutLine,
    CheckoutStatus,
    CommerceActorType,
    CommerceEventType,
    Order,
    OrderItem,
    OrderItemAttachment,
    OrderStatus,
    PaymentTransaction as LegacyPaymentTransaction,
    PaymentTransactionStatus as LegacyPaymentTransactionStatus,
    StockReservation,
    StockReservationState,
)
from cheatgame.shop.services.commerce_foundation import append_commerce_event


class PlacementNotEligible(ValidationError):
    pass


class ZeroValueOrderRequired(PlacementNotEligible):
    pass


class LegacyAdoptionRejected(ValidationError):
    def __init__(self, message, *, review_case_id=None):
        self.review_case_id = review_case_id
        super().__init__(message)


@dataclass(frozen=True)
class PlacementResult:
    order: Order
    payment: Payment
    replayed: bool


def _snapshot_hash(payload):
    def default(value):
        if isinstance(value, Decimal):
            return str(value)
        raise TypeError(type(value).__name__)

    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=default).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _completed_idempotency(*, scope, key, request_hash, result_type, result_id, safe_response):
    existing = IdempotencyRecord.objects.filter(scope=scope, key=str(key)).first()
    if existing:
        if existing.request_hash != request_hash:
            raise IdempotencyConflict("Idempotency key was reused with a different placement request.")
        return existing, False
    try:
        record = IdempotencyRecord.objects.create(
            scope=scope,
            key=str(key),
            request_hash=request_hash,
            status=IdempotencyStatus.COMPLETED,
            result_type=result_type,
            result_id=str(result_id),
            safe_response=safe_response,
            completed_at=timezone.now(),
        )
    except IntegrityError as exc:
        raise IdempotencyConflict("Concurrent idempotency ownership conflict.") from exc
    return record, True


def _line_payload(lines):
    return [
        {
            "line_id": line.id,
            "source_cart_item_id": line.source_cart_item_id,
            "product_id": line.product_id,
            "product_type": line.product_type,
            "authority": line.commerce_authority,
            "unit_original": line.unit_original_price,
            "unit_payable": line.unit_payable_price,
            "quantity": line.quantity,
            "original_total": line.line_original_total,
            "payable_total": line.line_payable_total,
            "attachments": [
                {
                    "attachment_id": attachment.attachment_id,
                    "type": attachment.attachment_type,
                    "unit_price": attachment.unit_price,
                    "quantity_basis": attachment.quantity_basis,
                    "total": attachment.total_price,
                }
                for attachment in line.attachments.all().order_by("pk")
            ],
        }
        for line in lines
    ]


def _validate_common_checkout(*, checkout, cart, expected_checkout_version):
    if checkout.user_id != cart.user_id:
        raise PlacementNotEligible("Checkout ownership is inconsistent with its Cart.")
    if checkout.status != CheckoutStatus.CHECKOUT_DRAFT:
        raise PlacementNotEligible("Checkout is not in the complete placement-ready projection.")
    if checkout.version != expected_checkout_version:
        raise PlacementNotEligible("Checkout version changed before placement.")
    if checkout.expires_at <= timezone.now():
        raise PlacementNotEligible("Checkout expired before placement.")
    if cart.state != CartState.LOCKED or cart.active_checkout_id != checkout.id:
        raise PlacementNotEligible("Checkout does not own the active Cart lock.")


def _lock_and_validate_standard(*, checkout, order, lines, now):
    if checkout.lines.filter(digital_snapshot__isnull=False).exists():
        raise PlacementNotEligible("Standard Checkout contains Digital snapshots.")
    if checkout.digital_inventory_reservations.exists():
        raise PlacementNotEligible("Standard Checkout contains Digital reservations.")
    try:
        shipping = checkout.shipping_snapshot
    except Exception as exc:
        raise PlacementNotEligible("Standard Checkout shipping terms are incomplete.") from exc
    if not shipping.is_pricing_finalized:
        raise PlacementNotEligible("Standard shipping pricing is not finalized.")
    if shipping.address_id is None or shipping.delivery_method_id is None:
        raise PlacementNotEligible("Standard shipping terms are incomplete.")
    product_ids = sorted({line.product_id for line in lines})
    products = lock_many(queryset=Product.objects.all(), rank=LockRank.COMMERCIAL_RESOURCE, pks=product_ids)
    product_map = {product.pk: product for product in products}
    requirements = {}
    for line in lines:
        if line.product_id not in product_map:
            raise PlacementNotEligible("Checkout Product no longer exists.")
        requirements[line.product_id] = requirements.get(line.product_id, 0) + line.quantity
    reservations = lock_many(
        queryset=StockReservation.objects.all(),
        rank=LockRank.RESERVATION,
        pks=StockReservation.objects.filter(checkout=checkout).values_list("pk", flat=True),
    )
    held = {reservation.product_id: reservation for reservation in reservations}
    if set(held) != set(requirements):
        raise PlacementNotEligible("Standard reservations are incomplete.")
    for product_id, quantity in requirements.items():
        reservation = held[product_id]
        if (
            reservation.state != StockReservationState.ACTIVE
            or reservation.quantity != quantity
            or reservation.expires_at <= now
            or reservation.order_id is not None
        ):
            raise PlacementNotEligible("Standard reservation is not eligible for placement.")
        reservation.state = StockReservationState.PAYMENT_HOLD
        reservation.order = order
        reservation.save(update_fields=("state", "order", "updated_at"))
    return shipping


def _lock_and_validate_digital(*, checkout, order, lines, now):
    if checkout.lines.filter(digital_snapshot__isnull=True).exists():
        raise PlacementNotEligible("Digital Checkout snapshots are incomplete.")
    if StockReservation.objects.filter(checkout=checkout).exists():
        raise PlacementNotEligible("Digital Checkout contains Standard reservations.")
    snapshots = list(
        DigitalCheckoutLineSnapshot.objects.filter(checkout_line__checkout=checkout)
        .select_related("checkout_line")
        .order_by("checkout_line_id")
    )
    if len(snapshots) != len(lines):
        raise PlacementNotEligible("Digital Checkout snapshots are incomplete.")
    pool_ids = sorted({snapshot.inventory_pool_id for snapshot in snapshots})
    lock_many(queryset=InventoryPool.objects.all(), rank=LockRank.COMMERCIAL_RESOURCE, pks=pool_ids)
    reservations = lock_many(
        queryset=DigitalInventoryReservation.objects.all(),
        rank=LockRank.RESERVATION,
        pks=DigitalInventoryReservation.objects.filter(checkout=checkout).values_list("pk", flat=True),
    )
    by_line = {reservation.checkout_line_id: reservation for reservation in reservations}
    if set(by_line) != {line.id for line in lines}:
        raise PlacementNotEligible("Digital reservations are incomplete.")
    for snapshot in snapshots:
        reservation = by_line[snapshot.checkout_line_id]
        if (
            reservation.state != DigitalInventoryReservationState.ACTIVE
            or reservation.inventory_pool_id != snapshot.inventory_pool_id
            or reservation.quantity != 1
            or reservation.expires_at <= now
            or reservation.order_id is not None
        ):
            raise PlacementNotEligible("Digital reservation is not eligible for placement.")
        reservation.state = DigitalInventoryReservationState.PAYMENT_HOLD
        reservation.order = order
        reservation.state_changed_at = now
        reservation.resolution_reason = "order_placed"
        reservation.save(
            update_fields=("state", "order", "state_changed_at", "resolution_reason", "updated_at")
        )
    return None


@transaction.atomic
def place_order_and_create_payment_obligation(
    *,
    checkout_id,
    expected_user_id,
    expected_checkout_version,
    source_unit,
    idempotency_key,
    actor_type=FinancialActorType.SYSTEM,
    actor_id=None,
):
    request_payload = {
        "checkout_id": int(checkout_id),
        "expected_user_id": int(expected_user_id),
        "expected_checkout_version": int(expected_checkout_version),
        "source_unit": str(source_unit).upper() if source_unit else "",
    }
    request_hash = canonical_request_hash(request_payload)
    scope = f"financial_core:place_checkout:{checkout_id}"
    identity = Checkout.objects.values("cart_id").get(pk=checkout_id)
    if identity["cart_id"] is None:
        raise PlacementNotEligible("Checkout has no Cart placement owner.")
    with ordered_lock_scope():
        cart = lock_one(queryset=Cart.objects.all(), rank=LockRank.CART, pk=identity["cart_id"])
        checkout = lock_one(queryset=Checkout.objects.all(), rank=LockRank.CHECKOUT, pk=checkout_id)
        if checkout.user_id != int(expected_user_id):
            raise PermissionDenied("Checkout does not belong to the expected customer.")

        existing_order = Order.objects.filter(checkout=checkout).first()
        if existing_order is not None:
            payment = Payment.objects.filter(order=existing_order).first()
            record = IdempotencyRecord.objects.filter(scope=scope, key=str(idempotency_key)).first()
            if record is None or record.request_hash != request_hash or payment is None:
                raise IdempotencyConflict("Checkout placement already exists with different command evidence.")
            return PlacementResult(existing_order, payment, True)

        _validate_common_checkout(
            checkout=checkout,
            cart=cart,
            expected_checkout_version=int(expected_checkout_version),
        )
        preview_lines = list(checkout.lines.prefetch_related("attachments").order_by("pk"))
        if not preview_lines:
            raise PlacementNotEligible("Checkout has no immutable commercial lines.")
        authorities = {line.commerce_authority for line in preview_lines}
        if len(authorities) != 1:
            raise PlacementNotEligible("Mixed or incoherent commerce authority is forbidden.")
        authority = next(iter(authorities))
        if authority not in (
            ProductCommerceAuthority.STANDARD_COMMERCE,
            ProductCommerceAuthority.DIGITAL_PRODUCTS,
        ):
            raise PlacementNotEligible("Checkout commerce authority is unsupported.")
        for line in preview_lines:
            line.full_clean()

        items_original = sum((line.line_original_total for line in preview_lines), Decimal("0"))
        items_payable = sum((line.line_payable_total for line in preview_lines), Decimal("0"))
        shipping_amount = Decimal("0")
        shipping_preview = getattr(checkout, "shipping_snapshot", None)
        if authority == ProductCommerceAuthority.STANDARD_COMMERCE:
            if shipping_preview is None or not shipping_preview.is_pricing_finalized:
                raise PlacementNotEligible("Standard shipping pricing is not finalized.")
            shipping_amount = shipping_preview.delivery_cost
        source_total = items_payable + shipping_amount
        if source_total == 0:
            raise ZeroValueOrderRequired(
                "Zero-value Orders require the separately controlled zero-value acceptance boundary."
            )
        normalized = normalize_obligation_money(
            source_amount=source_total,
            source_unit=source_unit,
            source_model="shop.Checkout",
            source_object_id=checkout.pk,
            source_field="computed_payable_total",
        )
        snapshot_payload = {
            "checkout_id": checkout.pk,
            "checkout_version": checkout.version,
            "cart_fingerprint": checkout.cart_fingerprint,
            "authority": authority,
            "lines": _line_payload(preview_lines),
            "items_original": items_original,
            "items_payable": items_payable,
            "shipping": shipping_amount,
            "source_total": source_total,
            "source_unit": normalized.source_unit,
        }
        commercial_snapshot_hash = _snapshot_hash(snapshot_payload)

        order = Order.objects.create(
            user_id=checkout.user_id,
            payment_status=OrderStatus.PENDDING.value,
            total_price=items_original + shipping_amount,
            total_price_discount=source_total,
            shipping_address_id=shipping_preview.address_id if shipping_preview else None,
            shipping_method_id=shipping_preview.delivery_method_id if shipping_preview else None,
            is_game=all(line.product_type == ProductType.GAME for line in preview_lines),
            checkout=checkout,
        )
        register_lock(LockRank.PAYABLE, f"{order.pk:020d}")
        for line in preview_lines:
            order_item = OrderItem.objects.create(
                order=order,
                product_id=line.product_id,
                quantity=line.quantity,
                price=line.line_payable_total,
            )
            for attachment in line.attachments.all().order_by("pk"):
                if attachment.attachment_id is not None:
                    OrderItemAttachment.objects.create(
                        order_item=order_item,
                        attachment_id=attachment.attachment_id,
                    )
        payment = Payment.objects.create(
            order=order,
            amount_due=normalized.canonical_amount,
            currency=normalized.canonical_currency,
            collection_status=PaymentCollectionStatus.OPEN,
        )
        register_lock(LockRank.PAYMENT, f"{payment.pk:020d}")

        lines = lock_many(
            queryset=CheckoutLine.objects.prefetch_related("attachments"),
            rank=LockRank.COMMERCIAL_LINE,
            pks=[line.pk for line in preview_lines],
        )
        if _snapshot_hash({**snapshot_payload, "lines": _line_payload(lines)}) != commercial_snapshot_hash:
            raise PlacementNotEligible("Checkout snapshots changed during placement.")

        now = timezone.now()
        if authority == ProductCommerceAuthority.STANDARD_COMMERCE:
            _lock_and_validate_standard(checkout=checkout, order=order, lines=lines, now=now)
        else:
            _lock_and_validate_digital(checkout=checkout, order=order, lines=lines, now=now)

        PaymentObligationSource.objects.create(
            payment=payment,
            source_kind=PaymentObligationSourceKind.CHECKOUT_PLACEMENT,
            source_model="shop.Checkout",
            source_object_id=str(checkout.pk),
            source_field="computed_payable_total",
            source_amount=normalized.source_amount,
            source_unit=normalized.source_unit,
            canonical_amount=normalized.canonical_amount,
            canonical_currency=normalized.canonical_currency,
            bridge_version=normalized.bridge_version,
            evidence_fingerprint=normalized.evidence_fingerprint,
            commercial_snapshot_hash=commercial_snapshot_hash,
            commerce_authority=authority,
            idempotency_key=UUID(str(idempotency_key)),
        )

        checkout.status = CheckoutStatus.PENDING_PAYMENT
        checkout.version += 1
        checkout.save(update_fields=("status", "version", "updated_at"))
        cart.lock_reason = CartLockReason.PAYMENT_IN_PROGRESS
        cart.lock_version += 1
        cart.save(update_fields=("lock_reason", "lock_version", "updated_at"))

        _completed_idempotency(
            scope=scope,
            key=idempotency_key,
            request_hash=request_hash,
            result_type=payment._meta.label_lower,
            result_id=payment.pk,
            safe_response={"order_id": order.pk, "payment_public_id": str(payment.public_id)},
        )
        register_lock(LockRank.EVENT_OUTBOX, f"event:payment:{payment.pk:020d}:0000000001")
        append_financial_event(
            aggregate_type=payment._meta.label_lower,
            aggregate_id=payment.public_id,
            aggregate_version=payment.version,
            event_type="payment.obligation_created",
            actor_type=actor_type,
            actor_id=actor_id,
            idempotency_key=f"placement:{idempotency_key}",
            metadata={
                "new_status": payment.collection_status,
                "amount": payment.amount_due,
                "currency": payment.currency,
            },
        )
        append_commerce_event(
            checkout=checkout,
            order=order,
            event_type=CommerceEventType.ORDER_PLACED,
            actor_type=CommerceActorType.CUSTOMER if actor_id else CommerceActorType.SYSTEM,
            actor_id=actor_id,
            idempotency_reference=str(idempotency_key),
            metadata={"outcome": "placed"},
        )
        append_commerce_event(
            checkout=checkout,
            order=order,
            event_type=CommerceEventType.PAYMENT_OBLIGATION_CREATED,
            actor_type=CommerceActorType.CUSTOMER if actor_id else CommerceActorType.SYSTEM,
            actor_id=actor_id,
            idempotency_reference=str(idempotency_key),
            metadata={"outcome": "created"},
        )
        append_outbox_message(
            topic="payment.obligation.created",
            aggregate_type=payment._meta.label_lower,
            aggregate_id=payment.public_id,
            idempotency_key=f"outbox:placement:{idempotency_key}",
            correlation_id=payment.public_id,
            payload={
                "event_type": "payment.obligation_created",
                "payment_public_id": str(payment.public_id),
                "new_status": payment.collection_status,
            },
        )
        return PlacementResult(order, payment, False)


LEGACY_BLOCKING_STATUSES = (
    LegacyPaymentTransactionStatus.CREATED,
    LegacyPaymentTransactionStatus.PENDING,
    LegacyPaymentTransactionStatus.CALLBACK_RECEIVED,
    LegacyPaymentTransactionStatus.VERIFYING,
    LegacyPaymentTransactionStatus.PAID,
    LegacyPaymentTransactionStatus.REQUIRES_MANUAL_REVIEW,
)


@transaction.atomic
def adopt_legacy_order_payment_obligation(
    *,
    order_id,
    expected_user_id,
    source_unit,
    idempotency_key,
    legacy_owner_inactive,
    ownership_evidence_reference,
    actor_type=FinancialActorType.SYSTEM,
    actor_id=None,
):
    if not legacy_owner_inactive or not ownership_evidence_reference:
        raise LegacyAdoptionRejected("Legacy payment ownership must be explicitly inactive.")
    request_payload = {
        "order_id": int(order_id),
        "expected_user_id": int(expected_user_id),
        "source_unit": str(source_unit).upper() if source_unit else "",
        "ownership_evidence_reference": str(ownership_evidence_reference),
    }
    request_hash = canonical_request_hash(request_payload)
    scope = f"financial_core:adopt_legacy_order:{order_id}"
    with ordered_lock_scope():
        order = lock_one(queryset=Order.objects.all(), rank=LockRank.PAYABLE, pk=order_id)
        if order.user_id != int(expected_user_id):
            raise PermissionDenied("Legacy Order does not belong to the expected customer.")
        existing = Payment.objects.filter(order=order).first()
        if existing:
            record = IdempotencyRecord.objects.filter(scope=scope, key=str(idempotency_key)).first()
            if record and record.request_hash == request_hash:
                return existing
            raise IdempotencyConflict("Legacy Order already has another Payment obligation.")
        if order.payment_status not in (OrderStatus.PENDDING.value, OrderStatus.FAIDED.value):
            raise LegacyAdoptionRejected("Legacy Order is not demonstrably unpaid.")
        normalized = normalize_obligation_money(
            source_amount=order.total_price_discount,
            source_unit=source_unit,
            source_model="shop.Order",
            source_object_id=order.pk,
            source_field="total_price_discount",
        )
        payment = Payment.objects.create(
            order=order,
            amount_due=normalized.canonical_amount,
            currency=normalized.canonical_currency,
        )
        register_lock(LockRank.PAYMENT, f"{payment.pk:020d}")
        legacy_transaction_ids = list(
            LegacyPaymentTransaction.objects.filter(order=order).order_by("pk").values_list("pk", flat=True)
        )
        for legacy_transaction_id in legacy_transaction_ids:
            register_lock(LockRank.PAYMENT_TRANSACTION, f"legacy:{legacy_transaction_id:020d}")
        legacy_transactions = list(
            LegacyPaymentTransaction.objects.select_for_update()
            .filter(pk__in=legacy_transaction_ids)
            .order_by("pk")
        )
        if any(item.status in LEGACY_BLOCKING_STATUSES for item in legacy_transactions):
            raise LegacyAdoptionRejected("Legacy payment evidence is live, paid, unknown, or under review.")
        snapshot_hash = _snapshot_hash(
            {
                "order_id": order.pk,
                "user_id": order.user_id,
                "total_price": order.total_price,
                "total_price_discount": order.total_price_discount,
                "source_unit": normalized.source_unit,
                "ownership_evidence_reference": str(ownership_evidence_reference),
            }
        )
        PaymentObligationSource.objects.create(
            payment=payment,
            source_kind=PaymentObligationSourceKind.LEGACY_ORDER_ADOPTION,
            source_model="shop.Order",
            source_object_id=str(order.pk),
            source_field="total_price_discount",
            source_amount=normalized.source_amount,
            source_unit=normalized.source_unit,
            canonical_amount=normalized.canonical_amount,
            canonical_currency=normalized.canonical_currency,
            bridge_version=normalized.bridge_version,
            evidence_fingerprint=normalized.evidence_fingerprint,
            commercial_snapshot_hash=snapshot_hash,
            commerce_authority="legacy_standard_commerce",
            idempotency_key=UUID(str(idempotency_key)),
        )
        _completed_idempotency(
            scope=scope,
            key=idempotency_key,
            request_hash=request_hash,
            result_type=payment._meta.label_lower,
            result_id=payment.pk,
            safe_response={"order_id": order.pk, "payment_public_id": str(payment.public_id)},
        )
        register_lock(LockRank.EVENT_OUTBOX, f"event:payment:{payment.pk:020d}:0000000001")
        append_financial_event(
            aggregate_type=payment._meta.label_lower,
            aggregate_id=payment.public_id,
            aggregate_version=payment.version,
            event_type="legacy_payment.obligation_adopted",
            actor_type=actor_type,
            actor_id=actor_id,
            idempotency_key=f"legacy-adoption:{idempotency_key}",
            metadata={
                "new_status": payment.collection_status,
                "amount": payment.amount_due,
                "currency": payment.currency,
            },
        )
        return payment
