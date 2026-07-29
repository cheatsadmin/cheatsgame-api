from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid5

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from cheatgame.digital_products.models import (
    DigitalInventoryReservation,
    DigitalInventoryReservationState,
)
from cheatgame.digital_products.services.payment_hold_policy import (
    get_digital_payment_hold_policy,
)
from cheatgame.digital_products.services.reservations import (
    DigitalReservationCardinalityError,
    classify_digital_reservations,
)
from cheatgame.financial_core.models import (
    FinancialActorType,
    FinancialAllocation,
    Payment,
    PaymentAttempt,
    PaymentAttemptStatus,
    PaymentCollectionStatus,
    PaymentTransaction,
    PaymentTransactionStatus,
    ReviewCase,
    ReviewCaseReason,
    ReviewCaseSeverity,
    ReviewCaseStatus,
    Verification,
    VerificationOutcome,
    VerificationApplicationState,
    VerificationWorkItem,
    VerificationWorkStatus,
)
from cheatgame.financial_core.services.events import append_financial_event
from cheatgame.financial_core.services.locks import (
    LockRank,
    lock_many,
    lock_one,
    ordered_lock_scope,
    register_lock,
)
from cheatgame.shop.models import (
    Cart,
    CartLockReason,
    CartState,
    Checkout,
    CheckoutStatus,
    CommerceActorType,
    CommerceEvent,
    CommerceEventType,
    Order,
)
from cheatgame.shop.services.commerce_foundation import append_commerce_event


HOLD_POLICY_NAMESPACE = UUID("91bdc889-952d-4fde-a8df-e662cb86cc3f")
OPEN_REVIEW_STATES = frozenset(
    {
        ReviewCaseStatus.OPEN,
        ReviewCaseStatus.INVESTIGATING,
        ReviewCaseStatus.APPROVAL_PENDING,
    }
)
TERMINAL_UNPAID_TRANSACTION_STATES = frozenset(
    {
        PaymentTransactionStatus.DECLINED,
        PaymentTransactionStatus.CANCELED,
        PaymentTransactionStatus.EXPIRED,
    }
)


class DigitalPaymentHoldConflict(ValidationError):
    pass


@dataclass(frozen=True)
class DigitalPaymentHoldResult:
    reservation_ids: tuple[int, ...]
    state: str
    expires_at: object
    replayed: bool


def _uuid(value):
    return uuid5(HOLD_POLICY_NAMESPACE, str(value))


def _event_once(*, checkout, order, event_type, reference, metadata):
    existing = CommerceEvent.objects.filter(
        checkout=checkout,
        event_type=event_type,
        idempotency_reference=reference,
    ).first()
    if existing is not None:
        return existing
    return append_commerce_event(
        checkout=checkout,
        order=order,
        event_type=event_type,
        actor_type=CommerceActorType.SYSTEM,
        idempotency_reference=reference,
        correlation_id=str(order.public_tracking_code or ""),
        metadata=metadata,
    )


def _lock_current_reservations(*, checkout, order):
    reservations = lock_many(
        queryset=DigitalInventoryReservation.objects.all(),
        rank=LockRank.RESERVATION,
        pks=DigitalInventoryReservation.objects.filter(order=order).values_list("pk", flat=True),
    )
    try:
        lineage = classify_digital_reservations(reservations)
    except DigitalReservationCardinalityError as exc:
        raise DigitalPaymentHoldConflict(str(exc)) from exc
    line_ids = set(checkout.lines.values_list("pk", flat=True))
    if not line_ids or set(lineage.by_line) != line_ids:
        raise DigitalPaymentHoldConflict("Digital reservation coverage is incomplete.")
    return lineage, line_ids


def _extend_locked_hold(
    *,
    cart,
    checkout,
    order,
    target_state,
    extension_seconds,
    reason_code,
    idempotency_identity,
    now=None,
):
    if checkout.status not in (
        CheckoutStatus.PENDING_PAYMENT,
        CheckoutStatus.REQUIRES_MANUAL_REVIEW,
    ):
        raise DigitalPaymentHoldConflict("Checkout cannot retain a payment hold in its current state.")
    if cart.state != CartState.LOCKED or cart.active_checkout_id != checkout.pk:
        raise DigitalPaymentHoldConflict("Cart is not owned by the unresolved Checkout.")
    lineage, line_ids = _lock_current_reservations(checkout=checkout, order=order)
    if set(lineage.current_by_line) != line_ids:
        raise DigitalPaymentHoldConflict("Every Checkout line requires one current reservation.")
    current = tuple(lineage.current_by_line.values())
    if any(
        reservation.state
        not in (
            DigitalInventoryReservationState.PAYMENT_HOLD,
            DigitalInventoryReservationState.HELD_FOR_REVIEW,
        )
        for reservation in current
    ):
        raise DigitalPaymentHoldConflict("Unresolved payment authority requires a live payment hold.")

    now = now or timezone.now()
    reference = f"payment-hold:{idempotency_identity}:{target_state}"
    existing_event = CommerceEvent.objects.filter(
        checkout=checkout,
        event_type=(
            CommerceEventType.MANUAL_REVIEW_REQUIRED
            if target_state == DigitalInventoryReservationState.HELD_FOR_REVIEW
            else CommerceEventType.PAYMENT_VERIFICATION_STARTED
        ),
        idempotency_reference=reference,
    ).first()
    if (
        existing_event is not None
        and checkout.expires_at > now
        and cart.lock_reason
        == (
            CartLockReason.MANUAL_REVIEW
            if target_state == DigitalInventoryReservationState.HELD_FOR_REVIEW
            else CartLockReason.PAYMENT_IN_PROGRESS
        )
        and all(
            reservation.state == target_state
            and reservation.expires_at == checkout.expires_at
            and reservation.expires_at > now
            for reservation in current
        )
    ):
        return DigitalPaymentHoldResult(
            tuple(sorted(item.pk for item in current)),
            target_state,
            checkout.expires_at,
            True,
        )
    expires_at = max(checkout.expires_at, now + timedelta(seconds=int(extension_seconds)))
    desired_lock_reason = (
        CartLockReason.MANUAL_REVIEW
        if target_state == DigitalInventoryReservationState.HELD_FOR_REVIEW
        else CartLockReason.PAYMENT_IN_PROGRESS
    )
    replayed = (
        checkout.expires_at >= expires_at
        and cart.lock_reason == desired_lock_reason
        and all(
            reservation.state == target_state and reservation.expires_at >= expires_at
            for reservation in current
        )
    )
    checkout.expires_at = expires_at
    checkout.maximum_expires_at = max(checkout.maximum_expires_at, expires_at)
    checkout.save(update_fields=("expires_at", "maximum_expires_at", "updated_at"))
    if cart.lock_reason != desired_lock_reason:
        cart.lock_reason = desired_lock_reason
        cart.lock_version += 1
        cart.save(update_fields=("lock_reason", "lock_version", "updated_at"))
    for reservation in current:
        reservation.checkout = checkout
        reservation.state = target_state
        reservation.expires_at = expires_at
        reservation.state_changed_at = now
        reservation.resolution_reason = str(reason_code)[:64]
        reservation.save(
            update_fields=(
                "state",
                "expires_at",
                "state_changed_at",
                "resolution_reason",
                "updated_at",
            )
        )
    _event_once(
        checkout=checkout,
        order=order,
        event_type=(
            CommerceEventType.MANUAL_REVIEW_REQUIRED
            if target_state == DigitalInventoryReservationState.HELD_FOR_REVIEW
            else CommerceEventType.PAYMENT_VERIFICATION_STARTED
        ),
        reference=reference,
        metadata={
            "reason_code": str(reason_code)[:64],
            "reservation_state": target_state,
            "expires_at": expires_at.isoformat(),
        },
    )
    return DigitalPaymentHoldResult(
        tuple(sorted(item.pk for item in current)),
        target_state,
        expires_at,
        replayed,
    )


def retain_locked_digital_payment_hold(
    *,
    cart,
    checkout,
    order,
    phase,
    idempotency_identity,
    now=None,
):
    policy = get_digital_payment_hold_policy()
    phase_policy = {
        "provider_pending": (
            DigitalInventoryReservationState.PAYMENT_HOLD,
            policy.provider_pending_seconds,
        ),
        "verification_pending": (
            DigitalInventoryReservationState.PAYMENT_HOLD,
            policy.verification_pending_seconds,
        ),
        "review": (
            DigitalInventoryReservationState.HELD_FOR_REVIEW,
            policy.review_hold_seconds,
        ),
        "nominal_expiry_success": (
            DigitalInventoryReservationState.PAYMENT_HOLD,
            policy.nominal_expiry_renewal_seconds,
        ),
        "paid_finalization": (
            DigitalInventoryReservationState.PAYMENT_HOLD,
            policy.nominal_expiry_renewal_seconds,
        ),
        "paid_finalization_review": (
            DigitalInventoryReservationState.HELD_FOR_REVIEW,
            policy.review_hold_seconds,
        ),
    }
    try:
        target_state, seconds = phase_policy[phase]
    except KeyError as exc:
        raise DigitalPaymentHoldConflict("Unsupported Digital payment-hold phase.") from exc
    return _extend_locked_hold(
        cart=cart,
        checkout=checkout,
        order=order,
        target_state=target_state,
        extension_seconds=seconds,
        reason_code=phase,
        idempotency_identity=idempotency_identity,
        now=now,
    )


def _graph_identity(payment_id):
    return Payment.objects.select_related("order__checkout").values(
        "order_id",
        "order__checkout_id",
        "order__checkout__cart_id",
    ).get(pk=payment_id)


def _lock_payment_graph(payment_id):
    identity = _graph_identity(payment_id)
    cart = lock_one(
        queryset=Cart.objects.all(),
        rank=LockRank.CART,
        pk=identity["order__checkout__cart_id"],
    )
    checkout = lock_one(
        queryset=Checkout.objects.all(),
        rank=LockRank.CHECKOUT,
        pk=identity["order__checkout_id"],
    )
    order = lock_one(
        queryset=Order.objects.all(),
        rank=LockRank.PAYABLE,
        pk=identity["order_id"],
    )
    payment = lock_one(
        queryset=Payment.objects.all(),
        rank=LockRank.PAYMENT,
        pk=payment_id,
    )
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
    return cart, checkout, order, payment, attempts, transactions


def _ensure_system_review_locked(
    *,
    order,
    payment,
    attempt,
    transaction_obj,
    reason,
    severity,
    summary,
    identity,
):
    key = _uuid(f"review:{payment.public_id}:{reason}:{identity}")
    register_lock(LockRank.REVIEW_CASE, f"review:{key}")
    review = ReviewCase.objects.select_for_update().filter(idempotency_key=key).first()
    if review is not None:
        return review, False
    review = ReviewCase.objects.create(
        reason=reason,
        severity=severity,
        order=order,
        payment=payment,
        attempt=attempt,
        transaction=transaction_obj,
        opened_by_type=FinancialActorType.SYSTEM,
        summary=str(summary)[:1000],
        idempotency_key=key,
    )
    register_lock(LockRank.EVENT_OUTBOX, f"review-event:{review.pk:020d}")
    append_financial_event(
        aggregate_type=review._meta.label_lower,
        aggregate_id=review.public_id,
        aggregate_version=review.version,
        event_type="review_case.opened",
        actor_type=FinancialActorType.SYSTEM,
        idempotency_key=f"payment-hold-review:{key}",
        correlation_id=payment.public_id,
        metadata={"reason_code": reason, "severity": severity},
    )
    return review, True


@transaction.atomic
def apply_verification_hold_policy(*, verification_id, now=None):
    verification_ref = Verification.objects.select_related(
        "transaction__attempt__payment"
    ).get(pk=verification_id)
    payment_id = verification_ref.transaction.attempt.payment_id
    with ordered_lock_scope():
        cart, checkout, order, payment, attempts, transactions = _lock_payment_graph(
            payment_id
        )
        verification = lock_one(
            queryset=Verification.objects.all(),
            rank=LockRank.FINANCIAL_EVIDENCE,
            pk=verification_id,
        )
        attempt = next(
            item for item in attempts if item.pk == verification.transaction.attempt_id
        )
        transaction_obj = next(
            item for item in transactions if item.pk == verification.transaction_id
        )
        terminal_contradiction = (
            attempt.status == PaymentAttemptStatus.DEFINITIVE_FAILED
            or transaction_obj.status in TERMINAL_UNPAID_TRANSACTION_STATES
        )
        if (
            verification.normalized_outcome == VerificationOutcome.CONFIRMED_SUCCESS
            and terminal_contradiction
        ):
            return None
        if verification.normalized_outcome == VerificationOutcome.CONFIRMED_SUCCESS:
            phase = "nominal_expiry_success"
        elif (
            verification.application_state == VerificationApplicationState.UNAPPLIED
            and verification.normalized_outcome
            in (
                VerificationOutcome.PENDING,
                VerificationOutcome.NO_EFFECT_RETRYABLE,
            )
        ):
            phase = "verification_pending"
        elif verification.application_state == VerificationApplicationState.REVIEW_REQUIRED:
            phase = "review"
        else:
            return None
        result = retain_locked_digital_payment_hold(
            cart=cart,
            checkout=checkout,
            order=order,
            phase=phase,
            idempotency_identity=verification.result_idempotency_key,
            now=now,
        )
        review = None
        created = False
        if phase == "review":
            review, created = _ensure_system_review_locked(
                order=order,
                payment=payment,
                attempt=attempt,
                transaction_obj=transaction_obj,
                reason=ReviewCaseReason.PROVIDER_STATE_UNCLEAR,
                severity=ReviewCaseSeverity.HIGH,
                summary="Verification could not establish a definitive provider outcome.",
                identity=f"verification:{verification.public_id}",
            )
        return result, review, created


@transaction.atomic
def escalate_overdue_uncertain_payment(*, payment_id, now=None):
    now = now or timezone.now()
    policy = get_digital_payment_hold_policy()
    with ordered_lock_scope():
        cart, checkout, order, payment, attempts, transactions = _lock_payment_graph(payment_id)
        if payment.collection_status not in (
            PaymentCollectionStatus.PROCESSING,
            PaymentCollectionStatus.REVIEW,
        ):
            raise DigitalPaymentHoldConflict("Payment is not unresolved.")
        if FinancialAllocation.objects.filter(payment=payment).exists():
            raise DigitalPaymentHoldConflict("Recognized funds cannot use uncertainty escalation.")
        if not attempts or not transactions:
            raise DigitalPaymentHoldConflict("Provider operation evidence is missing.")
        attempt = attempts[-1]
        transaction_obj = transactions[-1]
        age_seconds = (
            policy.review_hold_seconds
            if transaction_obj.status
            in (PaymentTransactionStatus.OUTCOME_UNKNOWN, PaymentTransactionStatus.REVIEW)
            else policy.provider_pending_seconds
        )
        if transaction_obj.updated_at + timedelta(seconds=age_seconds) > now:
            raise DigitalPaymentHoldConflict("Payment uncertainty is not overdue.")
        result = retain_locked_digital_payment_hold(
            cart=cart,
            checkout=checkout,
            order=order,
            phase="review",
            idempotency_identity=f"overdue:{payment.public_id}",
            now=now,
        )
        review, created = _ensure_system_review_locked(
            order=order,
            payment=payment,
            attempt=attempt,
            transaction_obj=transaction_obj,
            reason=ReviewCaseReason.PROVIDER_STATE_UNCLEAR,
            severity=ReviewCaseSeverity.HIGH,
            summary="Provider outcome remains unresolved beyond the configured policy window.",
            identity="overdue-uncertainty",
        )
        return result, review, created


@transaction.atomic
def renew_paid_finalization_hold(*, payment_id, identity, now=None, for_review=False):
    with ordered_lock_scope():
        cart, checkout, order, payment, attempts, transactions = _lock_payment_graph(payment_id)
        if payment.collection_status != PaymentCollectionStatus.PAID_PENDING_FINALIZATION:
            raise DigitalPaymentHoldConflict("Payment is not paid pending finalization.")
        if not FinancialAllocation.objects.filter(payment=payment).exists():
            raise DigitalPaymentHoldConflict("Paid hold renewal requires recognized funds.")
        return retain_locked_digital_payment_hold(
            cart=cart,
            checkout=checkout,
            order=order,
            phase="paid_finalization_review" if for_review else "paid_finalization",
            idempotency_identity=identity,
            now=now,
        )


@transaction.atomic
def escalate_terminal_finalization_failure(*, payment_id, classification, identity, now=None):
    now = now or timezone.now()
    with ordered_lock_scope():
        cart, checkout, order, payment, attempts, transactions = _lock_payment_graph(payment_id)
        if payment.collection_status != PaymentCollectionStatus.PAID_PENDING_FINALIZATION:
            raise DigitalPaymentHoldConflict("Terminal finalization review requires recognized funds.")
        try:
            result = retain_locked_digital_payment_hold(
                cart=cart,
                checkout=checkout,
                order=order,
                phase="paid_finalization_review",
                idempotency_identity=identity,
                now=now,
            )
        except DigitalPaymentHoldConflict:
            # A missing/released claim is itself a paid inventory conflict. Do
            # not manufacture inventory here; persist review ownership below.
            result = DigitalPaymentHoldResult((), "", checkout.expires_at, False)
        attempt = attempts[-1] if attempts else None
        transaction_obj = transactions[-1] if transactions else None
        review, created = _ensure_system_review_locked(
            order=order,
            payment=payment,
            attempt=attempt,
            transaction_obj=transaction_obj,
            reason=ReviewCaseReason.COMMERCIAL_FINALIZATION_FAILED,
            severity=ReviewCaseSeverity.CRITICAL,
            summary=f"Commercial finalization requires manual review: {classification}",
            identity="terminal-finalization",
        )
        return result, review, created


def abandonment_candidate_payment_ids(*, now=None, limit=100):
    now = now or timezone.now()
    cutoff = now - timedelta(
        seconds=get_digital_payment_hold_policy().abandonment_seconds
    )
    return list(
        Payment.objects.filter(
            collection_status=PaymentCollectionStatus.OPEN,
            attempts__status=PaymentAttemptStatus.DEFINITIVE_FAILED,
            attempts__transactions__status__in=TERMINAL_UNPAID_TRANSACTION_STATES,
            attempts__transactions__completed_at__lte=cutoff,
            order__digital_inventory_reservations__state=(
                DigitalInventoryReservationState.PAYMENT_HOLD
            ),
        )
        .exclude(
            review_cases__status__in=OPEN_REVIEW_STATES,
        )
        .exclude(
            attempts__transactions__verification_work_items__status__in=(
                VerificationWorkStatus.PENDING,
                VerificationWorkStatus.WAITING,
                VerificationWorkStatus.CLAIMED,
            )
        )
        .order_by("attempts__transactions__completed_at", "pk")
        .values_list("pk", flat=True)
        .distinct()[: int(limit)]
    )


@transaction.atomic
def expire_abandoned_payment_hold(*, payment_id, now=None):
    now = now or timezone.now()
    policy = get_digital_payment_hold_policy()
    with ordered_lock_scope():
        cart, checkout, order, payment, attempts, transactions = _lock_payment_graph(payment_id)
        if FinancialAllocation.objects.filter(payment=payment).exists() or payment.confirmed_amount:
            raise DigitalPaymentHoldConflict("Recognized funds block abandonment.")
        lineage, _ = _lock_current_reservations(checkout=checkout, order=order)
        locked_reservations = [
            reservation
            for rows in lineage.by_line.values()
            for reservation in rows
        ]
        open_reviews = lock_many(
            queryset=ReviewCase.objects.all(),
            rank=LockRank.REVIEW_CASE,
            pks=ReviewCase.objects.filter(
                payment=payment,
                status__in=OPEN_REVIEW_STATES,
            ).values_list("pk", flat=True),
        )
        if open_reviews:
            raise DigitalPaymentHoldConflict("An unresolved review blocks abandonment.")
        pending_work = VerificationWorkItem.objects.filter(
            transaction__attempt__payment=payment,
            status__in=(
                VerificationWorkStatus.PENDING,
                VerificationWorkStatus.WAITING,
                VerificationWorkStatus.CLAIMED,
            ),
        ).exists()
        if pending_work:
            raise DigitalPaymentHoldConflict("Pending verification work blocks abandonment.")
        terminal_pairs = [
            (attempt, transaction_obj)
            for attempt in attempts
            for transaction_obj in transactions
            if transaction_obj.attempt_id == attempt.pk
            and attempt.status == PaymentAttemptStatus.DEFINITIVE_FAILED
            and transaction_obj.status in TERMINAL_UNPAID_TRANSACTION_STATES
            and transaction_obj.completed_at is not None
            and transaction_obj.completed_at
            + timedelta(seconds=policy.abandonment_seconds)
            <= now
        ]
        if not terminal_pairs:
            raise DigitalPaymentHoldConflict("No authoritative abandoned terminal operation exists.")
        attempt, transaction_obj = terminal_pairs[-1]
        from cheatgame.digital_products.services.payment_failures import (
            terminate_locked_definitive_unpaid_digital_graph,
        )

        return terminate_locked_definitive_unpaid_digital_graph(
            cart=cart,
            checkout=checkout,
            order=order,
            payment=payment,
            attempt=attempt,
            transaction_obj=transaction_obj,
            reason_code="authoritative_payment_abandonment",
            idempotency_identity=_uuid(f"abandonment:{payment.public_id}"),
            locked_reservations=locked_reservations,
        )
