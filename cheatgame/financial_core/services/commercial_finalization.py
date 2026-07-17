from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID, uuid4, uuid5

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from cheatgame.digital_products.models import (
    DigitalInventoryReservation,
    DigitalInventoryReservationState,
    InventoryPool,
)
from cheatgame.financial_core.models import (
    CANONICAL_CURRENCY,
    CommercialAccountingPolicyVersion,
    CommercialFinalization,
    CommercialFinalizationWorkItem,
    DigitalFulfillmentObligation,
    FinancialActorType,
    FinancialAllocation,
    FinalizationWorkStatus,
    IdempotencyRecord,
    IdempotencyStatus,
    Payment,
    PaymentCollectionStatus,
    PostingDirection,
    ReviewCase,
    ReviewCaseReason,
    ReviewCaseStatus,
    StandardFulfillmentObligation,
)
from cheatgame.financial_core.services.events import append_financial_event
from cheatgame.financial_core.services.idempotency import IdempotencyConflict, canonical_request_hash
from cheatgame.financial_core.services.journal import post_balanced_journal_entry_under_lock
from cheatgame.financial_core.services.locks import LockRank, lock_many, lock_one, ordered_lock_scope, register_lock
from cheatgame.financial_core.services.money import normalize_obligation_money
from cheatgame.financial_core.services.state_machines import assert_payment_transition
from cheatgame.product.models import Product, ProductCommerceAuthority
from cheatgame.shop.models import (
    Cart,
    CartState,
    Checkout,
    CheckoutLine,
    CheckoutStatus,
    CommerceActorType,
    CommerceEventType,
    FulfillmentStatus,
    Order,
    OrderItem,
    OrderStatus,
    StockReservation,
    StockReservationState,
)
from cheatgame.shop.services.commerce_foundation import append_commerce_event


FINALIZER_VERSION = "commercial-finalizer-v1"
COMMERCIAL_JOURNAL_SOURCE = "commercial_reclassification"
FINALIZATION_NAMESPACE = UUID("5c71ef26-9ace-4b8e-81fd-25e01a285cf9")


class CommercialFinalizationBlocked(ValidationError):
    pass


@dataclass(frozen=True)
class CommercialFinalizationResult:
    finalization: CommercialFinalization
    replayed: bool


def _deterministic_uuid(value):
    return uuid5(FINALIZATION_NAMESPACE, str(value))


def _canonical_commercial_component(*, source, amount, checkout_id, source_field):
    if Decimal(amount) == 0:
        return Decimal("0")
    normalized = normalize_obligation_money(
        source_amount=amount,
        source_unit=source.source_unit,
        source_model="shop.Checkout",
        source_object_id=checkout_id,
        source_field=source_field,
    )
    if normalized.canonical_currency != CANONICAL_CURRENCY:
        raise CommercialFinalizationBlocked("Commercial component did not normalize to canonical IRR.")
    return normalized.canonical_amount


def _active_policy(authority):
    candidates = list(
        CommercialAccountingPolicyVersion.objects.filter(
            commerce_authority=authority, active_for_new_finalizations=True
        ).order_by("pk")
    )
    if len(candidates) != 1:
        raise CommercialFinalizationBlocked("Exactly one active commercial accounting policy is required.")
    return lock_one(
        queryset=CommercialAccountingPolicyVersion.objects.all(),
        rank=LockRank.ACCOUNTING_POLICY,
        pk=candidates[0].pk,
    )


@transaction.atomic
def finalize_paid_commerce(
    *,
    payment_id,
    idempotency_key,
    expected_payment_version,
    correlation_id,
    causation_id=None,
    actor_type=FinancialActorType.SYSTEM,
    actor_id=None,
):
    """Atomically execute commercial obligations already backed by recognized provider funds."""
    if actor_type not in (FinancialActorType.SYSTEM, FinancialActorType.RECONCILIATION):
        raise CommercialFinalizationBlocked("Only controlled system actors may finalize commerce.")
    identity = Payment.objects.values("order_id", "order__checkout_id", "order__checkout__cart_id").get(pk=payment_id)
    if identity["order__checkout_id"] is None:
        raise CommercialFinalizationBlocked("Financial Core finalization requires a placed Checkout Order.")
    if identity["order__checkout__cart_id"] is None:
        raise CommercialFinalizationBlocked("Financial Core finalization requires a Checkout-owned Cart.")

    with ordered_lock_scope():
        cart = lock_one(
            queryset=Cart.objects.all(), rank=LockRank.CART, pk=identity["order__checkout__cart_id"]
        )
        checkout = lock_one(
            queryset=Checkout.objects.all(), rank=LockRank.CHECKOUT, pk=identity["order__checkout_id"]
        )
        order = lock_one(queryset=Order.objects.all(), rank=LockRank.PAYABLE, pk=identity["order_id"])
        payment = lock_one(queryset=Payment.objects.all(), rank=LockRank.PAYMENT, pk=payment_id)
        register_lock(LockRank.FINANCIAL_EVIDENCE, f"payment:{payment.pk:020d}")

        payload = {
            "payment_public_id": str(payment.public_id),
            "expected_payment_version": int(expected_payment_version),
            "finalizer_version": FINALIZER_VERSION,
            "actor_type": actor_type,
        }
        fingerprint = canonical_request_hash(payload)
        existing_key = CommercialFinalization.objects.filter(
            application_idempotency_key=idempotency_key
        ).first()
        if existing_key:
            if existing_key.application_fingerprint != fingerprint or existing_key.payment_id != payment.pk:
                raise IdempotencyConflict("Commercial-finalization idempotency key conflicts.")
            return CommercialFinalizationResult(existing_key, True)
        existing = CommercialFinalization.objects.filter(payment=payment).first()
        if existing:
            if existing.application_fingerprint != fingerprint:
                raise IdempotencyConflict("Payment was finalized by different command evidence.")
            return CommercialFinalizationResult(existing, True)

        if cart.state != CartState.LOCKED or cart.active_checkout_id != checkout.pk:
            raise CommercialFinalizationBlocked("Cart is not owned by the Checkout awaiting finalization.")

        if payment.version != int(expected_payment_version):
            raise CommercialFinalizationBlocked("Payment version changed before commercial finalization.")
        if payment.collection_status != PaymentCollectionStatus.PAID_PENDING_FINALIZATION:
            raise CommercialFinalizationBlocked("Payment is not paid pending commercial finalization.")
        if payment.currency != CANONICAL_CURRENCY or payment.confirmed_amount != payment.amount_due:
            raise CommercialFinalizationBlocked("Payment is not exactly funded in canonical IRR.")
        allocated = FinancialAllocation.objects.filter(payment=payment).aggregate(total=Sum("amount"))["total"] or 0
        if Decimal(allocated) != payment.confirmed_amount:
            raise CommercialFinalizationBlocked("Confirmed Payment does not reconcile to immutable allocations.")
        if order.checkout_id != checkout.pk or payment.order_id != order.pk:
            raise CommercialFinalizationBlocked("Commercial ownership is inconsistent.")
        if order.payment_status != OrderStatus.PENDDING.value or order.fulfillment_status != FulfillmentStatus.NOT_STARTED:
            raise CommercialFinalizationBlocked("Order is not in the placement-finalization state.")
        if checkout.status != CheckoutStatus.PENDING_PAYMENT:
            raise CommercialFinalizationBlocked("Checkout is not awaiting commercial finalization.")

        try:
            source = payment.obligation_source
        except Exception as exc:
            raise CommercialFinalizationBlocked("Payment obligation evidence is missing.") from exc
        authority = source.commerce_authority
        if authority not in (
            ProductCommerceAuthority.STANDARD_COMMERCE,
            ProductCommerceAuthority.DIGITAL_PRODUCTS,
        ):
            raise CommercialFinalizationBlocked("Commercial authority is unsupported.")
        if source.canonical_amount != payment.amount_due or source.canonical_currency != CANONICAL_CURRENCY:
            raise CommercialFinalizationBlocked("Payment obligation evidence does not match Payment.")
        policy = _active_policy(authority)

        order_items = lock_many(
            queryset=OrderItem.objects.all(),
            rank=LockRank.COMMERCIAL_LINE,
            pks=OrderItem.objects.filter(order=order).values_list("pk", flat=True),
        )
        lines = list(CheckoutLine.objects.filter(checkout=checkout).order_by("pk"))
        if not order_items or len(order_items) != len(lines):
            raise CommercialFinalizationBlocked("Order lines are incomplete.")
        if {line.commerce_authority for line in lines} != {authority}:
            raise CommercialFinalizationBlocked("Order contains incoherent commercial authority.")
        if any(
            item.product_id != line.product_id
            or item.quantity != line.quantity
            or item.price != line.line_payable_total
            for line, item in zip(lines, order_items)
        ):
            raise CommercialFinalizationBlocked("Frozen Order values do not match Checkout snapshots.")

        standard_specs = []
        digital_specs = []
        if authority == ProductCommerceAuthority.STANDARD_COMMERCE:
            product_ids = sorted({item.product_id for item in order_items})
            products = lock_many(queryset=Product.objects.all(), rank=LockRank.COMMERCIAL_RESOURCE, pks=product_ids)
            product_by_id = {item.pk: item for item in products}
            reservations = lock_many(
                queryset=StockReservation.objects.all(),
                rank=LockRank.RESERVATION,
                pks=StockReservation.objects.filter(order=order).values_list("pk", flat=True),
            )
            reservation_by_product = {item.product_id: item for item in reservations}
            required = {}
            for item in order_items:
                required[item.product_id] = required.get(item.product_id, 0) + item.quantity
            if set(required) != set(reservation_by_product):
                raise CommercialFinalizationBlocked("Standard reservation set is incomplete.")
            for product_id, quantity in required.items():
                reservation = reservation_by_product[product_id]
                product = product_by_id[product_id]
                if reservation.state != StockReservationState.PAYMENT_HOLD or reservation.quantity != quantity:
                    raise CommercialFinalizationBlocked("Standard reservation is not consumable.")
                if product.commerce_authority != authority or product.quantity < quantity:
                    raise CommercialFinalizationBlocked("Authoritative Standard inventory is insufficient.")
                product.quantity -= quantity
                product.save(update_fields=("quantity", "updated_at"))
                reservation.state = StockReservationState.CONSUMED
                reservation.save(update_fields=("state", "updated_at"))
            for item in order_items:
                standard_specs.append((item, reservation_by_product[item.product_id]))
        else:
            snapshots = {
                line.pk: line
                for line in checkout.lines.select_related("digital_snapshot").all()
                if hasattr(line, "digital_snapshot")
            }
            pool_ids = sorted({snapshot.digital_snapshot.inventory_pool_id for snapshot in snapshots.values()})
            pools = lock_many(queryset=InventoryPool.objects.all(), rank=LockRank.COMMERCIAL_RESOURCE, pks=pool_ids)
            pool_by_id = {item.pk: item for item in pools}
            reservations = lock_many(
                queryset=DigitalInventoryReservation.objects.all(),
                rank=LockRank.RESERVATION,
                pks=DigitalInventoryReservation.objects.filter(order=order).values_list("pk", flat=True),
            )
            by_line = {item.checkout_line_id: item for item in reservations}
            if len(snapshots) != len(lines) or set(by_line) != set(snapshots):
                raise CommercialFinalizationBlocked("Digital reservation set is incomplete.")
            order_by_line = {line.pk: item for line, item in zip(lines, order_items)}
            pool_required = {}
            for line_id, line in snapshots.items():
                snapshot = line.digital_snapshot
                reservation = by_line[line_id]
                if (
                    reservation.state != DigitalInventoryReservationState.PAYMENT_HOLD
                    or reservation.inventory_pool_id != snapshot.inventory_pool_id
                    or reservation.quantity != 1
                ):
                    raise CommercialFinalizationBlocked("Digital reservation is not consumable.")
                order_item = order_by_line.get(line_id)
                if (
                    order_item is None
                    or order_item.product_id != line.product_id
                    or order_item.quantity != 1
                    or order_item.price != line.line_payable_total
                ):
                    raise CommercialFinalizationBlocked("Digital Order line is inconsistent.")
                pool_required[snapshot.inventory_pool_id] = pool_required.get(snapshot.inventory_pool_id, 0) + 1
                digital_specs.append((order_item, reservation, snapshot))
            for pool_id, quantity in pool_required.items():
                pool = pool_by_id[pool_id]
                if pool.sellable_quantity < quantity:
                    raise CommercialFinalizationBlocked("Authoritative Digital inventory is insufficient.")
                pool.sellable_quantity -= quantity
                pool.save(update_fields=("sellable_quantity", "updated_at"))
            now = timezone.now()
            for _, reservation, _ in digital_specs:
                reservation.state = DigitalInventoryReservationState.CONSUMED
                reservation.state_changed_at = now
                reservation.resolution_reason = "commercial_finalized"
                reservation.save(update_fields=("state", "state_changed_at", "resolution_reason", "updated_at"))

        register_lock(LockRank.FULFILLMENT, f"order:{order.pk:020d}")
        shipping_source = Decimal("0")
        if authority == ProductCommerceAuthority.STANDARD_COMMERCE:
            try:
                shipping = checkout.shipping_snapshot
            except Exception as exc:
                raise CommercialFinalizationBlocked("Frozen shipping evidence is missing.") from exc
            if not shipping.is_pricing_finalized:
                raise CommercialFinalizationBlocked("Shipping price is not finalized.")
            shipping_source = shipping.delivery_cost
        shipping_amount = _canonical_commercial_component(
            source=source,
            amount=shipping_source,
            checkout_id=checkout.pk,
            source_field="shipping_snapshot.delivery_cost",
        )
        merchandise_amount = payment.amount_due - shipping_amount
        if merchandise_amount < 0:
            raise CommercialFinalizationBlocked("Commercial amount components are invalid.")

        liability_accounts = {
            allocation.accounting_policy_version.customer_unapplied_funds_account_id
            for allocation in FinancialAllocation.objects.filter(payment=payment).select_related("accounting_policy_version")
        }
        if liability_accounts != {policy.customer_unapplied_funds_account_id}:
            raise CommercialFinalizationBlocked("Commercial policy does not reclassify the recognized liability.")

        finalization_public_id = uuid4()
        postings = [{
            "account_id": policy.customer_unapplied_funds_account_id,
            "direction": PostingDirection.DEBIT,
            "amount": payment.amount_due,
            "currency": CANONICAL_CURRENCY,
            "memo": "Release deferred customer funds",
        }]
        if merchandise_amount:
            postings.append({
                "account_id": policy.merchandise_revenue_account_id,
                "direction": PostingDirection.CREDIT,
                "amount": merchandise_amount,
                "currency": CANONICAL_CURRENCY,
                "memo": "Commercial merchandise revenue",
            })
        if shipping_amount:
            postings.append({
                "account_id": policy.shipping_revenue_account_id,
                "direction": PostingDirection.CREDIT,
                "amount": shipping_amount,
                "currency": CANONICAL_CURRENCY,
                "memo": "Commercial shipping revenue",
            })
        journal = post_balanced_journal_entry_under_lock(
            source_type=COMMERCIAL_JOURNAL_SOURCE,
            source_id=finalization_public_id,
            idempotency_key=_deterministic_uuid(f"journal:{finalization_public_id}"),
            correlation_id=correlation_id,
            occurred_at=timezone.now(),
            description="Deferred customer funds reclassified on commercial acceptance.",
            postings=postings,
        )
        finalization = CommercialFinalization.objects.create(
            public_id=finalization_public_id,
            payment=payment,
            order=order,
            accounting_policy_version=policy,
            journal_entry=journal,
            amount=payment.amount_due,
            merchandise_amount=merchandise_amount,
            shipping_amount=shipping_amount,
            currency=CANONICAL_CURRENCY,
            commerce_authority=authority,
            finalizer_version=FINALIZER_VERSION,
            application_idempotency_key=idempotency_key,
            application_fingerprint=fingerprint,
            correlation_id=correlation_id,
            causation_id=causation_id,
        )
        for item, reservation in standard_specs:
            StandardFulfillmentObligation.objects.create(
                finalization=finalization,
                order=order,
                order_item=item,
                reservation=reservation,
                product_id=item.product_id,
                quantity=item.quantity,
            )
        for item, reservation, snapshot in digital_specs:
            DigitalFulfillmentObligation.objects.create(
                finalization=finalization,
                order=order,
                order_item=item,
                reservation=reservation,
                inventory_pool_id=reservation.inventory_pool_id,
                checkout_line_id=reservation.checkout_line_id,
                quantity=1,
                fulfillment_method=snapshot.fulfillment_method,
            )

        assert_payment_transition(payment.collection_status, PaymentCollectionStatus.PAID)
        payment.collection_status = PaymentCollectionStatus.PAID
        payment.version += 1
        payment.save(update_fields=("collection_status", "version", "updated_at"))
        order.payment_status = OrderStatus.PAID.value
        order.fulfillment_status = FulfillmentStatus.PROCESSING
        order.save(update_fields=("payment_status", "fulfillment_status", "updated_at"))
        checkout.status = CheckoutStatus.PAID
        checkout.paid_at = timezone.now()
        checkout.version += 1
        checkout.save(update_fields=("status", "paid_at", "version", "updated_at"))
        cart.state = CartState.OPEN
        cart.lock_reason = None
        cart.active_checkout = None
        cart.locked_at = None
        cart.lock_version += 1
        cart.save(
            update_fields=(
                "state",
                "lock_reason",
                "active_checkout",
                "locked_at",
                "lock_version",
                "updated_at",
            )
        )

        register_lock(LockRank.REVIEW_CASE, f"payment:{payment.pk:020d}")
        blocking = ReviewCase.objects.select_for_update().filter(
            payment=payment,
            status__in=(ReviewCaseStatus.OPEN, ReviewCaseStatus.INVESTIGATING, ReviewCaseStatus.APPROVAL_PENDING),
        ).exclude(reason=ReviewCaseReason.PAID_PENDING_FINALIZATION)
        if blocking.exists():
            raise CommercialFinalizationBlocked("An unresolved financial ReviewCase blocks finalization.")

        register_lock(LockRank.EVENT_OUTBOX, f"finalization:{finalization.pk:020d}")
        work = CommercialFinalizationWorkItem.objects.select_for_update().filter(
            payment=payment, finalizer_version__in=("commercial-finalizer-v1-dormant", FINALIZER_VERSION)
        ).order_by("pk").first()
        if work:
            work.status = FinalizationWorkStatus.COMPLETED
            work.completed_at = timezone.now()
            work.claim_token = None
            work.claimed_at = None
            work.claim_expires_at = None
            work.version += 1
            work.save(update_fields=("status", "completed_at", "claim_token", "claimed_at", "claim_expires_at", "version", "updated_at"))
        append_financial_event(
            aggregate_type=payment._meta.label_lower,
            aggregate_id=payment.public_id,
            aggregate_version=payment.version,
            event_type="payment.paid",
            actor_type=actor_type,
            actor_id=actor_id,
            idempotency_key=f"commercial-finalization:{idempotency_key}:payment",
            correlation_id=correlation_id,
            causation_id=causation_id,
            metadata={"new_status": payment.collection_status, "amount": payment.amount_due, "currency": CANONICAL_CURRENCY},
        )
        append_financial_event(
            aggregate_type=finalization._meta.label_lower,
            aggregate_id=finalization.public_id,
            aggregate_version=1,
            event_type="commercial.finalized",
            actor_type=actor_type,
            actor_id=actor_id,
            idempotency_key=f"commercial-finalization:{idempotency_key}:finalization",
            correlation_id=correlation_id,
            causation_id=causation_id,
            metadata={"new_status": "finalized", "amount": finalization.amount, "currency": CANONICAL_CURRENCY},
        )
        append_commerce_event(
            checkout=checkout,
            order=order,
            event_type=CommerceEventType.CART_UNLOCKED,
            actor_type=CommerceActorType.SYSTEM,
            idempotency_reference=f"{idempotency_key}:cart-unlocked",
            correlation_id=str(correlation_id),
            metadata={"outcome": "unlocked"},
        )
        append_commerce_event(
            checkout=checkout,
            order=order,
            event_type=CommerceEventType.STOCK_RESERVATION_CONSUMED,
            actor_type=CommerceActorType.SYSTEM,
            idempotency_reference=f"{idempotency_key}:inventory",
            correlation_id=str(correlation_id),
            metadata={"outcome": "consumed"},
        )
        append_commerce_event(
            checkout=checkout,
            order=order,
            event_type=CommerceEventType.FULFILLMENT_FINALIZATION_SUCCEEDED,
            actor_type=CommerceActorType.SYSTEM,
            idempotency_reference=f"{idempotency_key}:finalized",
            correlation_id=str(correlation_id),
            metadata={"outcome": "finalized"},
        )
        IdempotencyRecord.objects.create(
            scope="financial_core:commercial_finalization",
            key=str(idempotency_key),
            request_hash=fingerprint,
            status=IdempotencyStatus.COMPLETED,
            result_type=finalization._meta.label_lower,
            result_id=str(finalization.pk),
            safe_response={"finalization_public_id": str(finalization.public_id), "payment_status": payment.collection_status},
            completed_at=timezone.now(),
        )
        return CommercialFinalizationResult(finalization, False)
