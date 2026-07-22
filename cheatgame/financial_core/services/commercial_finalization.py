from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from time import monotonic, sleep
from uuid import UUID, uuid4, uuid5

from django.core.exceptions import ValidationError
from django.db import DatabaseError, transaction
from django.utils import timezone

from cheatgame.digital_products.models import (
    DigitalCheckoutLineSnapshot,
    DigitalInventoryReservation,
    DigitalInventoryReservationState,
    InventoryPool,
)
from cheatgame.financial_core.models import (
    CANONICAL_CURRENCY,
    CommercialAccountingPolicyVersion,
    CommercialFinalization,
    CommercialFinalizationWorkItem,
    DigitalInventoryCommitment,
    DigitalFulfillmentObligation,
    FinancialActorType,
    FinancialAllocation,
    FinalizationWorkStatus,
    IdempotencyRecord,
    IdempotencyStatus,
    JournalEntry,
    JournalPosting,
    Payment,
    PaymentAttempt,
    PaymentAttemptStatus,
    PaymentCollectionStatus,
    PaymentTransaction,
    PaymentTransactionStatus,
    PostingDirection,
    ProviderReferenceAllocation,
    ReceiptAccountingPolicyVersion,
    ReviewCase,
    ReviewAction,
    ReviewCaseReason,
    ReviewCaseStatus,
    StandardInventoryCommitment,
    StandardFulfillmentObligation,
    Verification,
)
from cheatgame.financial_core.services.events import append_financial_event
from cheatgame.financial_core.services.idempotency import IdempotencyConflict, canonical_request_hash
from cheatgame.financial_core.services.journal import post_balanced_journal_entry_under_lock
from cheatgame.financial_core.services.locks import LockRank, lock_many, lock_one, ordered_lock_scope, register_lock
from cheatgame.financial_core.services.money import normalize_obligation_money
from cheatgame.financial_core.services.outbox import append_outbox_message
from cheatgame.financial_core.services.placement import _line_payload, _snapshot_hash
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
WORK_CONTRACT_VERSION = "commercial-finalizer-v1-dormant"
FINALIZER_CONTRACT_VERSION = "commercial-finalizer-api08-v1"
SUPPORTED_WORK_ENGINE_VERSIONS = {WORK_CONTRACT_VERSION: FINALIZER_VERSION}
COMMERCIAL_JOURNAL_SOURCE = "commercial_reclassification"
RECEIPT_JOURNAL_SOURCE = "provider_receipt"
FULFILLMENT_OUTBOX_TOPIC = "commercial.fulfillment.requested"
FINALIZATION_NAMESPACE = UUID("5c71ef26-9ace-4b8e-81fd-25e01a285cf9")
CLAIM_LEASE = timedelta(minutes=5)


class CommercialFinalizationBlocked(ValidationError):
    pass


@dataclass(frozen=True)
class CommercialFinalizationResult:
    finalization: CommercialFinalization
    replayed: bool


@dataclass(frozen=True)
class CommercialWorkFinalizationResult:
    finalization: CommercialFinalization
    work_item: CommercialFinalizationWorkItem
    replayed: bool


def _deterministic_uuid(value):
    return uuid5(FINALIZATION_NAMESPACE, str(value))


def _validate_finalizer_actor(*, actor_type, actor_id, actor_reason):
    if actor_type not in (
        FinancialActorType.SYSTEM,
        FinancialActorType.RECONCILIATION,
        FinancialActorType.COMMERCIAL_RECOVERY,
    ):
        raise CommercialFinalizationBlocked("Only controlled system actors may finalize commerce.")
    if actor_type == FinancialActorType.SYSTEM and actor_id is not None:
        raise CommercialFinalizationBlocked("SYSTEM finalization cannot carry a user actor.")
    if actor_type in (FinancialActorType.RECONCILIATION, FinancialActorType.COMMERCIAL_RECOVERY):
        if actor_id is None or not str(actor_reason).strip():
            raise CommercialFinalizationBlocked("Recovery finalization requires an accountable actor and reason.")


def _lock_ranked_rows(*, queryset, rank, prefix, pks):
    ordered_ids = sorted({int(pk) for pk in pks})
    for pk in ordered_ids:
        register_lock(rank, f"{prefix}:{pk:020d}")
    rows = list(queryset.select_for_update().filter(pk__in=ordered_ids).order_by("pk"))
    if [row.pk for row in rows] != ordered_ids:
        raise CommercialFinalizationBlocked("The locked finalization graph is incomplete.")
    return rows


def _reservation_digest(rows):
    return canonical_request_hash(
        [
            {
                "id": row.pk,
                "checkout_id": row.checkout_id,
                "order_id": row.order_id,
                "quantity": row.quantity,
            }
            for row in sorted(rows, key=lambda item: item.pk)
        ]
    )


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


def _lock_and_validate_recognized_funds(payment):
    attempts = lock_many(
        queryset=PaymentAttempt.objects.all(),
        rank=LockRank.PAYMENT_ATTEMPT,
        pks=PaymentAttempt.objects.filter(payment=payment).values_list("pk", flat=True),
    )
    transactions = lock_many(
        queryset=PaymentTransaction.objects.all(),
        rank=LockRank.PAYMENT_TRANSACTION,
        pks=PaymentTransaction.objects.filter(attempt__payment=payment).values_list("pk", flat=True),
    )
    transaction_ids = [item.pk for item in transactions]
    verifications = _lock_ranked_rows(
        queryset=Verification.objects.all(),
        rank=LockRank.FINANCIAL_EVIDENCE,
        prefix="10-verification",
        pks=Verification.objects.filter(transaction_id__in=transaction_ids).values_list("pk", flat=True),
    )
    references = _lock_ranked_rows(
        queryset=ProviderReferenceAllocation.objects.all(),
        rank=LockRank.FINANCIAL_EVIDENCE,
        prefix="20-provider-reference",
        pks=ProviderReferenceAllocation.objects.filter(transaction_id__in=transaction_ids).values_list("pk", flat=True),
    )
    allocations = _lock_ranked_rows(
        queryset=FinancialAllocation.objects.all(),
        rank=LockRank.FINANCIAL_EVIDENCE,
        prefix="30-allocation",
        pks=FinancialAllocation.objects.filter(payment=payment).values_list("pk", flat=True),
    )
    policies = _lock_ranked_rows(
        queryset=ReceiptAccountingPolicyVersion.objects.all(),
        rank=LockRank.FINANCIAL_EVIDENCE,
        prefix="40-receipt-policy",
        pks=[item.accounting_policy_version_id for item in allocations],
    )
    journals = _lock_ranked_rows(
        queryset=JournalEntry.objects.all(),
        rank=LockRank.FINANCIAL_EVIDENCE,
        prefix="50-receipt-journal",
        pks=[item.journal_entry_id for item in allocations],
    )
    attempt_by_id = {item.pk: item for item in attempts}
    transaction_by_id = {item.pk: item for item in transactions}
    verification_by_id = {item.pk: item for item in verifications}
    reference_by_transaction = {item.transaction_id: item for item in references}
    policy_by_id = {item.pk: item for item in policies}
    journal_by_id = {item.pk: item for item in journals}
    if not allocations or sum((item.amount for item in allocations), Decimal("0")) != payment.confirmed_amount:
        raise CommercialFinalizationBlocked("Confirmed Payment does not reconcile to immutable allocations.")
    for allocation in allocations:
        transaction_obj = transaction_by_id.get(allocation.transaction_id)
        attempt = attempt_by_id.get(allocation.attempt_id)
        verification = verification_by_id.get(allocation.verification_id)
        reference = reference_by_transaction.get(allocation.transaction_id)
        policy = policy_by_id.get(allocation.accounting_policy_version_id)
        journal = journal_by_id.get(allocation.journal_entry_id)
        if (
            transaction_obj is None
            or attempt is None
            or verification is None
            or reference is None
            or policy is None
            or journal is None
            or transaction_obj.attempt_id != attempt.pk
            or attempt.payment_id != payment.pk
            or transaction_obj.status != PaymentTransactionStatus.SUCCEEDED
            or attempt.status != PaymentAttemptStatus.SUCCEEDED
            or verification.transaction_id != transaction_obj.pk
            or reference.verification_id != verification.pk
            or reference.transaction_id != transaction_obj.pk
            or reference.merchant_account_version_id != allocation.merchant_account_version_id
            or reference.provider_reference != allocation.provider_reference
            or allocation.amount != transaction_obj.amount
            or allocation.currency != CANONICAL_CURRENCY
            or policy.merchant_account_version_id != allocation.merchant_account_version_id
            or journal.source_type != RECEIPT_JOURNAL_SOURCE
            or journal.source_id != str(allocation.public_id)
        ):
            raise CommercialFinalizationBlocked("Recognized financial evidence is incoherent.")
        postings = list(JournalPosting.objects.filter(entry=journal).order_by("line_number"))
        debit = sum(
            (
                item.amount
                for item in postings
                if item.account_id == policy.provider_clearing_account_id
                and item.direction == PostingDirection.DEBIT
                and item.currency == CANONICAL_CURRENCY
            ),
            Decimal("0"),
        )
        credit = sum(
            (
                item.amount
                for item in postings
                if item.account_id == policy.customer_unapplied_funds_account_id
                and item.direction == PostingDirection.CREDIT
                and item.currency == CANONICAL_CURRENCY
            ),
            Decimal("0"),
        )
        if debit != allocation.amount or credit != allocation.amount or len(postings) != 2:
            raise CommercialFinalizationBlocked("Provider-receipt Journal is incoherent.")
    return attempts, transactions, verifications, references, allocations, journals


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
    actor_reason="",
    work_item_id=None,
    expected_work_version=None,
    work_claim_token=None,
    application_fingerprint=None,
    expected_policy_id=None,
):
    """Atomically execute commercial obligations already backed by recognized provider funds."""
    _validate_finalizer_actor(actor_type=actor_type, actor_id=actor_id, actor_reason=actor_reason)
    if (
        work_item_id is None
        or expected_work_version is None
        or work_claim_token is None
        or application_fingerprint is None
        or expected_policy_id is None
    ):
        raise CommercialFinalizationBlocked(
            "Commercial finalization requires an authoritative claimed work item."
        )
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
        _lock_and_validate_recognized_funds(payment)

        payload = {
            "payment_public_id": str(payment.public_id),
            "expected_payment_version": int(expected_payment_version),
            "finalizer_version": FINALIZER_VERSION,
            "actor_type": actor_type,
        }
        fingerprint = application_fingerprint or canonical_request_hash(payload)
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
        if expected_policy_id is not None and policy.pk != int(expected_policy_id):
            raise CommercialFinalizationBlocked("Commercial accounting policy changed before finalization.")

        order_items = _lock_ranked_rows(
            queryset=OrderItem.objects.all(),
            rank=LockRank.COMMERCIAL_LINE,
            prefix="10-order-item",
            pks=OrderItem.objects.filter(order=order).values_list("pk", flat=True),
        )
        lines = _lock_ranked_rows(
            queryset=CheckoutLine.objects.prefetch_related("attachments"),
            rank=LockRank.COMMERCIAL_LINE,
            prefix="20-checkout-line",
            pks=CheckoutLine.objects.filter(checkout=checkout).values_list("pk", flat=True),
        )
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

        shipping_source = Decimal("0")
        shipping = getattr(checkout, "shipping_snapshot", None)
        if authority == ProductCommerceAuthority.STANDARD_COMMERCE:
            if shipping is None or not shipping.is_pricing_finalized:
                raise CommercialFinalizationBlocked("Frozen shipping evidence is missing or incomplete.")
            shipping_source = shipping.delivery_cost
        snapshot_payload = {
            "checkout_id": checkout.pk,
            "checkout_version": checkout.version - 1,
            "cart_fingerprint": checkout.cart_fingerprint,
            "authority": authority,
            "lines": _line_payload(lines),
            "items_original": sum((line.line_original_total for line in lines), Decimal("0")),
            "items_payable": sum((line.line_payable_total for line in lines), Decimal("0")),
            "shipping": shipping_source,
            "source_total": sum((line.line_payable_total for line in lines), Decimal("0")) + shipping_source,
            "source_unit": source.source_unit,
        }
        if _snapshot_hash(snapshot_payload) != source.commercial_snapshot_hash:
            raise CommercialFinalizationBlocked("Frozen placement snapshot hash is incoherent.")

        standard_specs = []
        digital_specs = []
        standard_commitment_specs = []
        digital_commitment_specs = []
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
            if len(reservations) != len(required) or set(required) != set(reservation_by_product):
                raise CommercialFinalizationBlocked("Standard reservation set is incomplete.")
            for product_id, quantity in required.items():
                reservation = reservation_by_product[product_id]
                product = product_by_id[product_id]
                if reservation.state == StockReservationState.RELEASED:
                    raise CommercialFinalizationBlocked("Standard reservation was released.")
                if reservation.expires_at <= timezone.now():
                    raise CommercialFinalizationBlocked("Standard reservation expired.")
                if (
                    reservation.state != StockReservationState.PAYMENT_HOLD
                    or reservation.quantity != quantity
                    or reservation.checkout_id != checkout.pk
                    or reservation.order_id != order.pk
                ):
                    raise CommercialFinalizationBlocked("Standard reservation is not consumable.")
                if product.commerce_authority != authority or product.quantity < quantity:
                    raise CommercialFinalizationBlocked("Authoritative Standard inventory is insufficient.")
                pre_quantity = product.quantity
                standard_commitment_specs.append(
                    (product, [reservation], pre_quantity, quantity, pre_quantity - quantity)
                )
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
            if len(reservations) != len(lines) or len(snapshots) != len(lines) or set(by_line) != set(snapshots):
                raise CommercialFinalizationBlocked("Digital reservation set is incomplete.")
            order_by_line = {line.pk: item for line, item in zip(lines, order_items)}
            pool_required = {}
            for line_id, line in snapshots.items():
                snapshot = line.digital_snapshot
                reservation = by_line[line_id]
                if reservation.state == DigitalInventoryReservationState.RELEASED:
                    raise CommercialFinalizationBlocked("Digital reservation was released.")
                if reservation.expires_at <= timezone.now():
                    raise CommercialFinalizationBlocked("Digital reservation expired.")
                if (
                    reservation.state != DigitalInventoryReservationState.PAYMENT_HOLD
                    or reservation.inventory_pool_id != snapshot.inventory_pool_id
                    or reservation.quantity != 1
                    or reservation.checkout_id != checkout.pk
                    or reservation.order_id != order.pk
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
                pre_quantity = pool.sellable_quantity
                digital_commitment_specs.append(
                    (
                        pool,
                        [item for item in reservations if item.inventory_pool_id == pool_id],
                        pre_quantity,
                        quantity,
                        pre_quantity - quantity,
                    )
                )

        register_lock(LockRank.FULFILLMENT, f"order:{order.pk:020d}")
        shipping_amount = _canonical_commercial_component(
            source=source,
            amount=shipping_source,
            checkout_id=checkout.pk,
            source_field="shipping_snapshot.delivery_cost",
        )
        merchandise_amount = payment.amount_due - shipping_amount
        if merchandise_amount < 0:
            raise CommercialFinalizationBlocked("Commercial amount components are invalid.")
        source_merchandise = sum((line.line_payable_total for line in lines), Decimal("0"))
        frozen_merchandise_amount = _canonical_commercial_component(
            source=source,
            amount=source_merchandise,
            checkout_id=checkout.pk,
            source_field="lines.line_payable_total",
        )
        if frozen_merchandise_amount != merchandise_amount:
            raise CommercialFinalizationBlocked("Frozen line totals do not reconcile to the Payment obligation.")

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
        for product, commitment_reservations, pre_quantity, quantity, post_quantity in standard_commitment_specs:
            StandardInventoryCommitment.objects.create(
                finalization=finalization,
                order=order,
                product=product,
                reservation_set_digest=_reservation_digest(commitment_reservations),
                pre_quantity=pre_quantity,
                committed_quantity=quantity,
                post_quantity=post_quantity,
                correlation_id=correlation_id,
                causation_id=causation_id,
            )
        for pool, commitment_reservations, pre_quantity, quantity, post_quantity in digital_commitment_specs:
            DigitalInventoryCommitment.objects.create(
                finalization=finalization,
                order=order,
                inventory_pool=pool,
                reservation_set_digest=_reservation_digest(commitment_reservations),
                pre_quantity=pre_quantity,
                committed_quantity=quantity,
                post_quantity=post_quantity,
                correlation_id=correlation_id,
                causation_id=causation_id,
            )

        for product, commitment_reservations, _, _, post_quantity in standard_commitment_specs:
            product.quantity = post_quantity
            product.save(update_fields=("quantity", "updated_at"))
            for reservation in commitment_reservations:
                reservation.state = StockReservationState.CONSUMED
                reservation.save(update_fields=("state", "updated_at"))

        digital_consumed_at = timezone.now()
        for pool, commitment_reservations, _, _, post_quantity in digital_commitment_specs:
            pool.sellable_quantity = post_quantity
            pool.save(update_fields=("sellable_quantity", "updated_at"))
            for reservation in commitment_reservations:
                reservation.state = DigitalInventoryReservationState.CONSUMED
                reservation.state_changed_at = digital_consumed_at
                reservation.resolution_reason = "commercial_finalized"
                reservation.save(
                    update_fields=("state", "state_changed_at", "resolution_reason", "updated_at")
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

        reviews = lock_many(
            queryset=ReviewCase.objects.all(),
            rank=LockRank.REVIEW_CASE,
            pks=ReviewCase.objects.filter(payment=payment).values_list("pk", flat=True),
        )
        blocking = [
            item
            for item in reviews
            if item.status in (ReviewCaseStatus.OPEN, ReviewCaseStatus.INVESTIGATING, ReviewCaseStatus.APPROVAL_PENDING)
            and item.reason != ReviewCaseReason.PAID_PENDING_FINALIZATION
        ]
        if blocking:
            raise CommercialFinalizationBlocked("An unresolved financial ReviewCase blocks finalization.")

        marker = next(
            (
                item
                for item in reviews
                if item.reason == ReviewCaseReason.PAID_PENDING_FINALIZATION
                and item.opened_by_type == FinancialActorType.SYSTEM
                and item.opened_by_id is None
                and item.status in (
                    ReviewCaseStatus.OPEN,
                    ReviewCaseStatus.INVESTIGATING,
                    ReviewCaseStatus.APPROVAL_PENDING,
                )
            ),
            None,
        )
        if marker is not None:
            ReviewAction.objects.create(
                review_case=marker,
                action_type=f"transition:{ReviewCaseStatus.RESOLVED}",
                actor_type=FinancialActorType.SYSTEM,
                actor=None,
                reason_code="commercial_finalization_completed",
                note="Resolved atomically by the API-08 commercial finalizer.",
                idempotency_key=_deterministic_uuid(f"review-resolution:{finalization.public_id}:{marker.public_id}"),
            )
            marker.status = ReviewCaseStatus.RESOLVED
            marker.resolution_code = "commercial_finalization_completed"
            marker.resolved_at = timezone.now()
            marker.version += 1
            marker.save(update_fields=("status", "resolution_code", "resolved_at", "version", "updated_at"))

        register_lock(LockRank.EVENT_OUTBOX, f"finalization:{finalization.pk:020d}")
        work = CommercialFinalizationWorkItem.objects.select_for_update().filter(
            payment=payment,
            pk=work_item_id,
        ).first()
        if (
            work is None
            or work.finalizer_version not in SUPPORTED_WORK_ENGINE_VERSIONS
            or SUPPORTED_WORK_ENGINE_VERSIONS[work.finalizer_version] != FINALIZER_VERSION
            or work.version != int(expected_work_version) + 1
            or work.status != FinalizationWorkStatus.CLAIMED
            or work.claim_token != work_claim_token
            or work.claim_expires_at is None
            or work.claim_expires_at <= timezone.now()
        ):
            raise CommercialFinalizationBlocked("Finalization work claim is stale or incoherent.")
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
        if marker is not None:
            append_financial_event(
                aggregate_type=marker._meta.label_lower,
                aggregate_id=marker.public_id,
                aggregate_version=marker.version,
                event_type="review_case.status_changed",
                actor_type=FinancialActorType.SYSTEM,
                actor_id=None,
                idempotency_key=f"commercial-finalization:{idempotency_key}:review-resolution",
                correlation_id=correlation_id,
                causation_id=causation_id,
                metadata={"new_status": marker.status, "reason_code": marker.resolution_code},
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
        append_outbox_message(
            topic=FULFILLMENT_OUTBOX_TOPIC,
            aggregate_type=finalization._meta.label_lower,
            aggregate_id=finalization.public_id,
            idempotency_key=f"outbox:commercial-fulfillment:{finalization.public_id}:{FINALIZER_CONTRACT_VERSION}",
            correlation_id=correlation_id,
            causation_id=causation_id or finalization.public_id,
            payload={
                "event_type": FULFILLMENT_OUTBOX_TOPIC,
                "commercial_finalization_public_id": str(finalization.public_id),
                "order_public_id": str(order.public_tracking_code),
                "commerce_authority": authority,
                "finalizer_contract_version": FINALIZER_CONTRACT_VERSION,
                "correlation_id": str(correlation_id),
                "causation_id": str(causation_id or finalization.public_id),
            },
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


def _work_application_identity(
    *,
    work,
    expected_work_version,
    expected_payment_version,
    actor_type,
    frozen_policy_id=None,
):
    payment = work.payment
    order = payment.order
    checkout = order.checkout
    if checkout is None:
        raise CommercialFinalizationBlocked("Finalization work requires a Checkout-owned Order.")
    try:
        source = payment.obligation_source
    except Exception as exc:
        raise CommercialFinalizationBlocked("Payment obligation evidence is missing.") from exc
    lines = list(
        CheckoutLine.objects.filter(checkout=checkout)
        .order_by("pk")
        .values(
            "pk",
            "product_id",
            "quantity",
            "line_payable_total",
            "commerce_authority",
            "snapshot",
        )
    )
    items = list(
        OrderItem.objects.filter(order=order)
        .order_by("pk")
        .values("pk", "product_id", "quantity", "price")
    )
    if source.commerce_authority == ProductCommerceAuthority.STANDARD_COMMERCE:
        reservations = list(
            StockReservation.objects.filter(order=order)
            .order_by("pk")
            .values("pk", "checkout_id", "order_id", "product_id", "quantity")
        )
    elif source.commerce_authority == ProductCommerceAuthority.DIGITAL_PRODUCTS:
        reservations = list(
            DigitalInventoryReservation.objects.filter(order=order)
            .order_by("pk")
            .values("pk", "checkout_id", "order_id", "checkout_line_id", "inventory_pool_id", "quantity")
        )
    else:
        raise CommercialFinalizationBlocked("Commercial authority is unsupported.")
    allocations = list(
        FinancialAllocation.objects.filter(payment=payment)
        .order_by("pk")
        .values(
            "pk",
            "public_id",
            "attempt_id",
            "transaction_id",
            "verification_id",
            "journal_entry_id",
            "accounting_policy_version_id",
            "amount",
            "currency",
        )
    )
    receipt_journal_ids = [item["journal_entry_id"] for item in allocations]
    receipt_journals = list(
        JournalEntry.objects.filter(pk__in=receipt_journal_ids)
        .order_by("pk")
        .values("pk", "public_id", "source_type", "source_id", "idempotency_key", "correlation_id")
    )
    receipt_postings = list(
        JournalPosting.objects.filter(entry_id__in=receipt_journal_ids)
        .order_by("entry_id", "line_number", "pk")
        .values("pk", "entry_id", "line_number", "account_id", "direction", "amount", "currency")
    )
    digital_snapshots = list(
        DigitalCheckoutLineSnapshot.objects.filter(checkout_line__checkout=checkout)
        .order_by("checkout_line_id")
        .values(
            "pk",
            "checkout_line_id",
            "offer_id",
            "inventory_pool_id",
            "delivered_version_id",
            "product_id",
            "commerce_authority",
            "customer_console",
            "capacity",
            "fulfillment_method",
            "version_label",
            "native_console",
            "compatibility_disclosure",
            "capacity_disclosure",
            "unit_price",
            "quantity",
            "line_total",
        )
    )
    if frozen_policy_id is None:
        policies = list(
            CommercialAccountingPolicyVersion.objects.filter(
                commerce_authority=source.commerce_authority,
                active_for_new_finalizations=True,
            ).values_list("pk", flat=True)
        )
        if len(policies) != 1:
            raise CommercialFinalizationBlocked("Exactly one active commercial accounting policy is required.")
        policy_id = policies[0]
    else:
        policy_id = int(frozen_policy_id)
        if not CommercialAccountingPolicyVersion.objects.filter(
            pk=policy_id,
            commerce_authority=source.commerce_authority,
        ).exists():
            raise CommercialFinalizationBlocked("Frozen commercial accounting policy is unavailable.")
    revisions = sorted(
        {
            str((line.get("snapshot") or {}).get("commercial_revision", ""))
            for line in lines
        }
    )
    payload = {
        "contract_version": FINALIZER_CONTRACT_VERSION,
        "work_public_id": str(work.public_id),
        "work_id": work.pk,
        "work_contract_version": work.finalizer_version,
        "engine_version": SUPPORTED_WORK_ENGINE_VERSIONS.get(work.finalizer_version),
        "payment_public_id": str(payment.public_id),
        "payment_id": payment.pk,
        "order_id": order.pk,
        "checkout_public_id": str(checkout.public_id),
        "checkout_id": checkout.pk,
        "obligation_source_id": source.pk,
        "snapshot_hash": source.commercial_snapshot_hash,
        "commercial_revisions": revisions,
        "commerce_authority": source.commerce_authority,
        "lines": lines,
        "order_items": items,
        "reservations": reservations,
        "recognized_financial_graph": allocations,
        "receipt_journals": receipt_journals,
        "receipt_postings": receipt_postings,
        "digital_snapshots": digital_snapshots,
        "commercial_policy_id": policy_id,
        "actor_type": actor_type,
        "expected_work_version": int(expected_work_version),
        "expected_payment_version": int(expected_payment_version),
    }
    return canonical_request_hash(payload), policy_id


@transaction.atomic
def _claim_commercial_work_item(*, work_public_id, idempotency_key, expected_work_version):
    work = CommercialFinalizationWorkItem.objects.select_for_update().select_related("payment").get(
        public_id=work_public_id
    )
    if work.finalizer_version not in SUPPORTED_WORK_ENGINE_VERSIONS:
        raise CommercialFinalizationBlocked("Commercial finalization work contract is unsupported.")
    if work.status == FinalizationWorkStatus.COMPLETED:
        finalization = CommercialFinalization.objects.filter(payment=work.payment).first()
        if finalization is None:
            raise CommercialFinalizationBlocked("Completed work has no coherent finalization.")
        return work, None, finalization
    if work.status == FinalizationWorkStatus.CANCELED:
        raise CommercialFinalizationBlocked("Canceled finalization work cannot be claimed.")
    token = _deterministic_uuid(f"claim:{work.public_id}:{idempotency_key}")
    now = timezone.now()
    reclaiming_expired_claim = False
    if work.status == FinalizationWorkStatus.CLAIMED:
        if work.claim_token == token:
            if work.claim_expires_at is None or work.claim_expires_at <= now:
                raise CommercialFinalizationBlocked("The replayed finalization claim expired.")
            return work, token, None
        if work.claim_expires_at and work.claim_expires_at > now:
            raise IdempotencyConflict("Commercial finalization work already has an active claim.")
        if work.version != int(expected_work_version):
            raise CommercialFinalizationBlocked("Commercial finalization work version changed.")
        work.status = FinalizationWorkStatus.PENDING
        work.claim_token = None
        work.claimed_at = None
        work.claim_expires_at = None
        work.version += 1
        work.save(
            update_fields=("status", "claim_token", "claimed_at", "claim_expires_at", "version", "updated_at")
        )
        reclaiming_expired_claim = True
    if not reclaiming_expired_claim and work.version != int(expected_work_version):
        raise CommercialFinalizationBlocked("Commercial finalization work version changed.")
    if work.attempt_count >= work.max_attempts or work.next_attempt_at > now:
        raise CommercialFinalizationBlocked("Commercial finalization work is not due or is exhausted.")
    work.status = FinalizationWorkStatus.CLAIMED
    work.attempt_count += 1
    work.claim_token = token
    work.claimed_at = now
    work.claim_expires_at = now + CLAIM_LEASE
    work.version += 1
    work.save(
        update_fields=(
            "status",
            "attempt_count",
            "claim_token",
            "claimed_at",
            "claim_expires_at",
            "version",
            "updated_at",
        )
    )
    claim_hash = canonical_request_hash(
        {"work_public_id": str(work.public_id), "expected_work_version": int(expected_work_version)}
    )
    claim_record, created = IdempotencyRecord.objects.get_or_create(
        scope="financial_core:commercial_finalization_claim",
        key=str(idempotency_key),
        defaults={
            "request_hash": claim_hash,
            "status": IdempotencyStatus.COMPLETED,
            "result_type": work._meta.label_lower,
            "result_id": str(work.pk),
            "safe_response": {"work_public_id": str(work.public_id), "claim_token": str(token)},
            "completed_at": now,
        },
    )
    if not created and (
        claim_record.request_hash != claim_hash
        or claim_record.result_type != work._meta.label_lower
        or claim_record.result_id != str(work.pk)
    ):
        raise IdempotencyConflict("Commercial finalization claim idempotency key conflicts.")
    return work, token, None


@transaction.atomic
def _record_finalization_failure(*, work_id, claim_token, classification, terminal):
    work = CommercialFinalizationWorkItem.objects.select_for_update().get(pk=work_id)
    if work.status != FinalizationWorkStatus.CLAIMED or work.claim_token != claim_token:
        return
    now = timezone.now()
    work.status = FinalizationWorkStatus.CANCELED if terminal else FinalizationWorkStatus.PENDING
    work.claim_token = None
    work.claimed_at = None
    work.claim_expires_at = None
    work.last_error_classification = str(classification)[:64]
    work.next_attempt_at = now
    work.completed_at = now if terminal else None
    work.version += 1
    work.save(
        update_fields=(
            "status",
            "claim_token",
            "claimed_at",
            "claim_expires_at",
            "last_error_classification",
            "next_attempt_at",
            "completed_at",
            "version",
            "updated_at",
        )
    )


def finalize_commercial_work_item(
    *,
    work_item_public_id,
    idempotency_key,
    expected_work_item_version,
    expected_payment_version,
    correlation_id,
    causation_id=None,
    actor_type=FinancialActorType.SYSTEM,
    actor_id=None,
    actor_reason="",
):
    """Dormant internal API-08 boundary. No URL, task, or signal invokes it."""
    work_public_id = UUID(str(work_item_public_id))
    key = UUID(str(idempotency_key))
    correlation = UUID(str(correlation_id))
    causation = UUID(str(causation_id)) if causation_id else None
    _validate_finalizer_actor(actor_type=actor_type, actor_id=actor_id, actor_reason=actor_reason)
    work_ref = CommercialFinalizationWorkItem.objects.select_related("payment__order__checkout").get(
        public_id=work_public_id
    )
    existing = CommercialFinalization.objects.filter(payment=work_ref.payment).first()
    if existing is not None:
        fingerprint, _ = _work_application_identity(
            work=work_ref,
            expected_work_version=expected_work_item_version,
            expected_payment_version=expected_payment_version,
            actor_type=actor_type,
            frozen_policy_id=existing.accounting_policy_version_id,
        )
        if existing.application_fingerprint != fingerprint:
            raise IdempotencyConflict("Existing commercial finalization has different immutable command evidence.")
        return CommercialWorkFinalizationResult(existing, work_ref, True)
    fingerprint, policy_id = _work_application_identity(
        work=work_ref,
        expected_work_version=expected_work_item_version,
        expected_payment_version=expected_payment_version,
        actor_type=actor_type,
    )
    try:
        work, claim_token, completed = _claim_commercial_work_item(
            work_public_id=work_public_id,
            idempotency_key=key,
            expected_work_version=expected_work_item_version,
        )
    except IdempotencyConflict as exc:
        if "active claim" not in str(exc):
            raise
        deadline = monotonic() + 5
        while monotonic() < deadline:
            concurrent = CommercialFinalization.objects.filter(payment=work_ref.payment).first()
            if concurrent is not None:
                if concurrent.application_fingerprint != fingerprint:
                    raise IdempotencyConflict(
                        "Concurrent commercial finalization has different immutable command evidence."
                    )
                completed_work = CommercialFinalizationWorkItem.objects.get(pk=work_ref.pk)
                return CommercialWorkFinalizationResult(concurrent, completed_work, True)
            sleep(0.01)
        raise IdempotencyConflict("Commercial finalization work remains in progress.") from exc
    if completed is not None:
        if completed.application_fingerprint != fingerprint:
            raise IdempotencyConflict("Completed finalization work has different immutable command evidence.")
        return CommercialWorkFinalizationResult(completed, work, True)
    try:
        result = finalize_paid_commerce(
            payment_id=work.payment_id,
            idempotency_key=key,
            expected_payment_version=expected_payment_version,
            correlation_id=correlation,
            causation_id=causation,
            actor_type=actor_type,
            actor_id=actor_id,
            actor_reason=actor_reason,
            work_item_id=work.pk,
            expected_work_version=work.version - 1,
            work_claim_token=claim_token,
            application_fingerprint=fingerprint,
            expected_policy_id=policy_id,
        )
    except Exception as exc:
        terminal = isinstance(exc, CommercialFinalizationBlocked) and any(
            fragment in str(exc).lower()
            for fragment in ("unsupported", "canceled", "released", "mixed", "expired")
        )
        try:
            _record_finalization_failure(
                work_id=work.pk,
                claim_token=claim_token,
                classification=type(exc).__name__,
                terminal=terminal,
            )
        except DatabaseError:
            pass
        raise
    work.refresh_from_db()
    return CommercialWorkFinalizationResult(result.finalization, work, result.replayed)
