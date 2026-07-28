from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid5

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from cheatgame.digital_products.models import (
    DigitalInventoryReservation,
    DigitalInventoryReservationState,
    InventoryPool,
)
from cheatgame.digital_products.services.reservations import (
    CURRENT_DIGITAL_RESERVATION_STATES,
    DigitalReservationCardinalityError,
    classify_digital_reservations,
)
from cheatgame.financial_core.models import (
    CANONICAL_CURRENCY,
    ExceptionalRecognitionAuthorization,
    ExceptionalRecognitionAuthorizationStatus,
    FinancialActorType,
    FinancialAllocation,
    LatePaymentAdjudication,
    LatePaymentAdjudicationDecision,
    LatePaymentAdjudicationStatus,
    Payment,
    PaymentAttempt,
    PaymentAttemptStatus,
    PaymentTransaction,
    PaymentTransactionStatus,
    ReviewAction,
    ReviewCase,
    ReviewCaseReason,
    ReviewCaseSeverity,
    ReviewCaseStatus,
    Verification,
)
from cheatgame.financial_core.services.events import append_financial_event
from cheatgame.financial_core.services.idempotency import canonical_request_hash
from cheatgame.financial_core.services.locks import (
    LockRank,
    lock_many,
    lock_one,
    ordered_lock_scope,
    register_lock,
)
from cheatgame.financial_core.services.reviews import open_review_case
from cheatgame.shop.models import Checkout, Order
from cheatgame.users.models import BaseUser, UserTypes


ADJUDICATION_NAMESPACE = UUID("7486b40b-56b2-4a86-98b7-3cece91ec884")
TERMINAL_TRANSACTION_STATUSES = (
    PaymentTransactionStatus.DECLINED,
    PaymentTransactionStatus.CANCELED,
    PaymentTransactionStatus.EXPIRED,
)


class LatePaymentAdjudicationError(ValidationError):
    pass


@dataclass(frozen=True)
class LatePaymentApplicationResult:
    adjudication: LatePaymentAdjudication
    allocation: FinancialAllocation
    inventory_recovered: bool
    inventory_review: ReviewCase


def _uuid(value, label):
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise LatePaymentAdjudicationError(f"{label} must be a UUID.") from exc


def _staff_actor(actor):
    if not isinstance(actor, BaseUser) or not actor.pk:
        raise PermissionDenied("An authenticated finance reviewer is required.")
    persisted = BaseUser.objects.filter(pk=actor.pk).values("is_active", "user_type").first()
    if not persisted or not persisted["is_active"] or persisted["user_type"] not in (
        UserTypes.ADMIN,
        UserTypes.MANAGER,
    ):
        raise PermissionDenied("An active Admin or Manager finance reviewer is required.")
    return actor.pk


def _is_terminal_contradiction(attempt, transaction_obj):
    return (
        attempt.status == PaymentAttemptStatus.DEFINITIVE_FAILED
        or transaction_obj.status in TERMINAL_TRANSACTION_STATUSES
    )


def ensure_terminal_late_payment_adjudication(*, verification, review_case):
    """Create the one adjudication identity for one exact terminal success Verification."""
    if review_case.reason != ReviewCaseReason.LATE_PAYMENT:
        raise LatePaymentAdjudicationError("Terminal adjudication requires a LATE_PAYMENT ReviewCase.")
    transaction_obj = verification.transaction
    attempt = transaction_obj.attempt
    payment = attempt.payment
    if not _is_terminal_contradiction(attempt, transaction_obj):
        raise LatePaymentAdjudicationError("Nominal-expiry success is not terminal adjudication.")
    key = uuid5(ADJUDICATION_NAMESPACE, f"adjudication:{verification.public_id}")
    existing = LatePaymentAdjudication.objects.filter(verification=verification).first()
    if existing:
        return existing, False
    adjudication = LatePaymentAdjudication.objects.create(
        review_case=review_case,
        verification=verification,
        order=payment.order,
        payment=payment,
        attempt=attempt,
        transaction=transaction_obj,
        idempotency_key=key,
    )
    append_financial_event(
        aggregate_type=adjudication._meta.label_lower,
        aggregate_id=adjudication.public_id,
        aggregate_version=adjudication.version,
        event_type="late_payment.detected",
        actor_type=FinancialActorType.SYSTEM,
        actor_id=None,
        idempotency_key=f"late-payment-detected:{adjudication.public_id}",
        correlation_id=verification.correlation_id,
        causation_id=verification.public_id,
        metadata={
            "reason_code": ReviewCaseReason.LATE_PAYMENT,
            "payment_public_id": str(payment.public_id),
            "transaction_public_id": str(transaction_obj.public_id),
        },
    )
    return adjudication, True


def list_open_terminal_late_payment_reviews():
    return LatePaymentAdjudication.objects.select_related(
        "review_case", "payment", "order", "attempt", "transaction", "verification"
    ).filter(status__in=(LatePaymentAdjudicationStatus.OPEN, LatePaymentAdjudicationStatus.MAKER_APPROVED))


def inspect_terminal_late_payment_review(*, public_id):
    return LatePaymentAdjudication.objects.select_related(
        "review_case", "payment", "order", "attempt", "transaction", "verification",
        "maker", "checker",
    ).get(public_id=public_id)


def _maker_replay(*, action, adjudication, actor_id, decision, rationale):
    expected = f"late_payment.maker_proposal:{adjudication.proposal_version}"
    if (
        action.review_case_id != adjudication.review_case_id
        or action.action_type != expected
        or action.actor_id != actor_id
        or action.reason_code != decision
        or action.note != str(rationale)[:1000]
    ):
        raise LatePaymentAdjudicationError("Maker idempotency key conflicts.")
    return adjudication


@transaction.atomic
def propose_terminal_late_payment_decision(
    *,
    adjudication_public_id,
    decision,
    rationale,
    actor,
    idempotency_key,
):
    actor_id = _staff_actor(actor)
    key = _uuid(idempotency_key, "idempotency_key")
    if decision not in LatePaymentAdjudicationDecision.values:
        raise LatePaymentAdjudicationError("Late-payment proposal decision is invalid.")
    if not str(rationale).strip():
        raise LatePaymentAdjudicationError("Maker rationale is required.")
    replay = ReviewAction.objects.filter(idempotency_key=key).first()
    if replay:
        adjudication = LatePaymentAdjudication.objects.get(public_id=adjudication_public_id)
        return _maker_replay(
            action=replay,
            adjudication=adjudication,
            actor_id=actor_id,
            decision=decision,
            rationale=rationale,
        )

    with ordered_lock_scope():
        ref = LatePaymentAdjudication.objects.select_related("payment").get(public_id=adjudication_public_id)
        lock_one(queryset=Order.objects.all(), rank=LockRank.PAYABLE, pk=ref.order_id)
        lock_one(queryset=Payment.objects.all(), rank=LockRank.PAYMENT, pk=ref.payment_id)
        lock_many(
            queryset=PaymentAttempt.objects.all(),
            rank=LockRank.PAYMENT_ATTEMPT,
            pks=PaymentAttempt.objects.filter(payment_id=ref.payment_id).values_list("pk", flat=True),
        )
        lock_many(
            queryset=PaymentTransaction.objects.all(),
            rank=LockRank.PAYMENT_TRANSACTION,
            pks=PaymentTransaction.objects.filter(attempt__payment_id=ref.payment_id).values_list("pk", flat=True),
        )
        lock_many(
            queryset=Verification.objects.all(),
            rank=LockRank.FINANCIAL_EVIDENCE,
            pks=Verification.objects.filter(transaction__attempt__payment_id=ref.payment_id).values_list("pk", flat=True),
        )
        register_lock(LockRank.REVIEW_CASE, f"adjudication:{ref.pk:020d}")
        adjudication = LatePaymentAdjudication.objects.select_for_update().get(pk=ref.pk)
        review = ReviewCase.objects.select_for_update().get(pk=adjudication.review_case_id)
        replay = ReviewAction.objects.filter(idempotency_key=key).first()
        if replay:
            return _maker_replay(
                action=replay,
                adjudication=adjudication,
                actor_id=actor_id,
                decision=decision,
                rationale=rationale,
            )
        if adjudication.status not in (
            LatePaymentAdjudicationStatus.OPEN,
            LatePaymentAdjudicationStatus.MAKER_APPROVED,
        ):
            raise LatePaymentAdjudicationError("Terminal adjudication no longer accepts proposals.")
        now = timezone.now()
        adjudication.status = LatePaymentAdjudicationStatus.MAKER_APPROVED
        adjudication.proposed_decision = decision
        adjudication.proposal_version += 1
        adjudication.maker_id = actor_id
        adjudication.maker_at = now
        adjudication.maker_rationale = str(rationale)[:1000]
        adjudication.checker = None
        adjudication.checked_at = None
        adjudication.checker_rationale = ""
        adjudication.decision = ""
        adjudication.version += 1
        adjudication.save()
        action_type = f"late_payment.maker_proposal:{adjudication.proposal_version}"
        ReviewAction.objects.create(
            review_case=review,
            action_type=action_type,
            actor_type=FinancialActorType.ADMIN,
            actor_id=actor_id,
            reason_code=decision,
            note=str(rationale)[:1000],
            requires_approval=True,
            idempotency_key=key,
        )
        if review.status == ReviewCaseStatus.OPEN:
            review.status = ReviewCaseStatus.APPROVAL_PENDING
            review.version += 1
            review.save(update_fields=("status", "version", "updated_at"))
        append_financial_event(
            aggregate_type=adjudication._meta.label_lower,
            aggregate_id=adjudication.public_id,
            aggregate_version=adjudication.version,
            event_type="late_payment.maker_proposed",
            actor_type=FinancialActorType.ADMIN,
            actor_id=actor_id,
            idempotency_key=f"late-payment-maker:{key}",
            correlation_id=adjudication.verification.correlation_id,
            causation_id=adjudication.verification.public_id,
            metadata={"decision": decision, "proposal_version": adjudication.proposal_version},
        )
        return adjudication


def _authorization_fingerprint(adjudication):
    verification = adjudication.verification
    return canonical_request_hash(
        {
            "contract": "terminal-late-payment-adjudication-v1",
            "adjudication_public_id": str(adjudication.public_id),
            "verification_public_id": str(verification.public_id),
            "payment_public_id": str(adjudication.payment.public_id),
            "transaction_public_id": str(adjudication.transaction.public_id),
            "merchant_account_version_id": verification.merchant_account_version_id,
            "provider_reference": verification.provider_reference,
            "amount": str(verification.canonical_allocation_amount),
            "currency": verification.canonical_currency,
            "evidence_hash": verification.evidence_hash,
        }
    )


def _checker_replay(
    *,
    action,
    adjudication,
    actor_id,
    approve_proposal,
    rationale,
    expected_proposal_version,
):
    expected_decision = (
        LatePaymentAdjudicationDecision.ACCEPT
        if bool(approve_proposal)
        and adjudication.proposed_decision == LatePaymentAdjudicationDecision.ACCEPT
        else LatePaymentAdjudicationDecision.REJECT
    )
    if (
        action.review_case_id != adjudication.review_case_id
        or action.action_type != f"transition:{ReviewCaseStatus.RESOLVED}"
        or action.actor_id != actor_id
        or action.reason_code != expected_decision
        or action.note != str(rationale)[:1000]
        or adjudication.proposal_version != int(expected_proposal_version)
    ):
        raise LatePaymentAdjudicationError("Checker idempotency key conflicts.")
    return adjudication


@transaction.atomic
def check_terminal_late_payment_decision(
    *,
    adjudication_public_id,
    approve_proposal,
    rationale,
    actor,
    expected_proposal_version,
    idempotency_key,
):
    actor_id = _staff_actor(actor)
    key = _uuid(idempotency_key, "idempotency_key")
    if not str(rationale).strip():
        raise LatePaymentAdjudicationError("Checker rationale is required.")
    replay = ReviewAction.objects.filter(idempotency_key=key).first()
    if replay:
        adjudication = LatePaymentAdjudication.objects.get(public_id=adjudication_public_id)
        return _checker_replay(
            action=replay,
            adjudication=adjudication,
            actor_id=actor_id,
            approve_proposal=approve_proposal,
            rationale=rationale,
            expected_proposal_version=expected_proposal_version,
        )

    with ordered_lock_scope():
        ref = LatePaymentAdjudication.objects.select_related("verification").get(public_id=adjudication_public_id)
        lock_one(queryset=Order.objects.all(), rank=LockRank.PAYABLE, pk=ref.order_id)
        lock_one(queryset=Payment.objects.all(), rank=LockRank.PAYMENT, pk=ref.payment_id)
        lock_many(
            queryset=PaymentAttempt.objects.all(),
            rank=LockRank.PAYMENT_ATTEMPT,
            pks=PaymentAttempt.objects.filter(payment_id=ref.payment_id).values_list("pk", flat=True),
        )
        lock_many(
            queryset=PaymentTransaction.objects.all(),
            rank=LockRank.PAYMENT_TRANSACTION,
            pks=PaymentTransaction.objects.filter(attempt__payment_id=ref.payment_id).values_list("pk", flat=True),
        )
        lock_many(
            queryset=Verification.objects.all(),
            rank=LockRank.FINANCIAL_EVIDENCE,
            pks=Verification.objects.filter(transaction__attempt__payment_id=ref.payment_id).values_list("pk", flat=True),
        )
        register_lock(LockRank.REVIEW_CASE, f"adjudication:{ref.pk:020d}")
        adjudication = LatePaymentAdjudication.objects.select_for_update().select_related(
            "verification", "transaction", "attempt", "payment", "order"
        ).get(pk=ref.pk)
        review = ReviewCase.objects.select_for_update().get(pk=adjudication.review_case_id)
        replay = ReviewAction.objects.filter(idempotency_key=key).first()
        if replay:
            return _checker_replay(
                action=replay,
                adjudication=adjudication,
                actor_id=actor_id,
                approve_proposal=approve_proposal,
                rationale=rationale,
                expected_proposal_version=expected_proposal_version,
            )
        if adjudication.status != LatePaymentAdjudicationStatus.MAKER_APPROVED:
            raise LatePaymentAdjudicationError("Adjudication is not awaiting checker action.")
        if adjudication.proposal_version != int(expected_proposal_version):
            raise LatePaymentAdjudicationError("Maker proposal changed before checker action.")
        if adjudication.maker_id == actor_id:
            raise PermissionDenied("Maker cannot approve their own late-payment proposal.")
        now = timezone.now()
        accepted = bool(approve_proposal) and adjudication.proposed_decision == LatePaymentAdjudicationDecision.ACCEPT
        adjudication.status = (
            LatePaymentAdjudicationStatus.APPROVED if accepted else LatePaymentAdjudicationStatus.REJECTED
        )
        adjudication.decision = (
            LatePaymentAdjudicationDecision.ACCEPT if accepted else LatePaymentAdjudicationDecision.REJECT
        )
        adjudication.checker_id = actor_id
        adjudication.checked_at = now
        adjudication.checker_rationale = str(rationale)[:1000]
        adjudication.version += 1
        adjudication.save()
        ReviewAction.objects.create(
            review_case=review,
            action_type=f"transition:{ReviewCaseStatus.RESOLVED}",
            actor_type=FinancialActorType.ADMIN,
            actor_id=actor_id,
            reason_code=adjudication.decision,
            note=str(rationale)[:1000],
            idempotency_key=key,
        )
        review.status = ReviewCaseStatus.RESOLVED
        review.resolution_code = (
            "late_payment_accepted" if accepted else "late_payment_rejected"
        )
        review.resolved_at = now
        review.version += 1
        review.save(update_fields=("status", "resolution_code", "resolved_at", "version", "updated_at"))
        if accepted:
            verification = adjudication.verification
            ExceptionalRecognitionAuthorization.objects.create(
                adjudication=adjudication,
                verification=verification,
                order=adjudication.order,
                payment=adjudication.payment,
                attempt=adjudication.attempt,
                transaction=adjudication.transaction,
                merchant_account_version=verification.merchant_account_version,
                provider_reference=verification.provider_reference,
                amount=verification.canonical_allocation_amount,
                currency=verification.canonical_currency,
                expected_payment_version=adjudication.payment.version,
                evidence_hash=verification.evidence_hash,
                authorization_fingerprint=_authorization_fingerprint(adjudication),
                authorized_by_id=actor_id,
                authorized_at=now,
                idempotency_key=uuid5(ADJUDICATION_NAMESPACE, f"authorization:{adjudication.public_id}"),
            )
        append_financial_event(
            aggregate_type=adjudication._meta.label_lower,
            aggregate_id=adjudication.public_id,
            aggregate_version=adjudication.version,
            event_type="late_payment.adjudication_approved" if accepted else "late_payment.adjudication_rejected",
            actor_type=FinancialActorType.ADMIN,
            actor_id=actor_id,
            idempotency_key=f"late-payment-checker:{key}",
            correlation_id=adjudication.verification.correlation_id,
            causation_id=adjudication.verification.public_id,
            metadata={"decision": adjudication.decision, "proposal_version": adjudication.proposal_version},
        )
        return adjudication


@transaction.atomic
def cancel_terminal_late_payment_adjudication(
    *, adjudication_public_id, reason, actor, idempotency_key
):
    actor_id = _staff_actor(actor)
    key = _uuid(idempotency_key, "idempotency_key")
    if not str(reason).strip():
        raise LatePaymentAdjudicationError("Cancellation reason is required.")
    adjudication = LatePaymentAdjudication.objects.select_for_update().get(public_id=adjudication_public_id)
    if hasattr(adjudication, "recognition_authorization"):
        raise LatePaymentAdjudicationError("Approved adjudication cannot be canceled.")
    if adjudication.status == LatePaymentAdjudicationStatus.CANCELED:
        return adjudication
    if adjudication.status not in (
        LatePaymentAdjudicationStatus.OPEN,
        LatePaymentAdjudicationStatus.MAKER_APPROVED,
    ):
        raise LatePaymentAdjudicationError("Terminal adjudication cannot be canceled.")
    review = ReviewCase.objects.select_for_update().get(pk=adjudication.review_case_id)
    ReviewAction.objects.create(
        review_case=review,
        action_type="late_payment.canceled",
        actor_type=FinancialActorType.ADMIN,
        actor_id=actor_id,
        reason_code="canceled",
        note=str(reason)[:1000],
        idempotency_key=key,
    )
    now = timezone.now()
    adjudication.status = LatePaymentAdjudicationStatus.CANCELED
    adjudication.checked_at = now
    adjudication.checker_id = actor_id if adjudication.maker_id != actor_id else None
    adjudication.checker_rationale = str(reason)[:1000]
    adjudication.version += 1
    adjudication.save()
    review.status = ReviewCaseStatus.CANCELED
    review.version += 1
    review.save(update_fields=("status", "version", "updated_at"))
    return adjudication


def _inventory_review_key(authorization):
    return uuid5(ADJUDICATION_NAMESPACE, f"inventory-review:{authorization.public_id}")


def _record_application_activity(*, review, authorization, action_type, reason_code, note):
    key = uuid5(
        ADJUDICATION_NAMESPACE,
        f"activity:{authorization.public_id}:{action_type}",
    )
    action, _ = ReviewAction.objects.get_or_create(
        idempotency_key=key,
        defaults={
            "review_case": review,
            "action_type": action_type,
            "actor_type": FinancialActorType.SYSTEM,
            "reason_code": reason_code,
            "note": note,
        },
    )
    if action.review_case_id != review.pk or action.action_type != action_type:
        raise LatePaymentAdjudicationError("Application activity idempotency conflict.")
    return action


def _ensure_inventory_review(authorization):
    return open_review_case(
        reason=ReviewCaseReason.INVENTORY_CONFLICT,
        severity=ReviewCaseSeverity.CRITICAL,
        summary="Accepted late funds require controlled inventory recovery before finalization.",
        idempotency_key=_inventory_review_key(authorization),
        command_key=f"late-payment-inventory-review:{authorization.public_id}",
        order_id=authorization.order_id,
        payment_id=authorization.payment_id,
        attempt_id=authorization.attempt_id,
        transaction_id=authorization.transaction_id,
    )


@transaction.atomic
def recover_terminal_late_payment_inventory(*, authorization_id):
    """Recover a reservation claim without decrementing inventory."""
    with ordered_lock_scope():
        auth_ref = ExceptionalRecognitionAuthorization.objects.select_related(
            "payment__order__checkout"
        ).get(pk=authorization_id)
        checkout = lock_one(
            queryset=Checkout.objects.all(),
            rank=LockRank.CHECKOUT,
            pk=auth_ref.payment.order.checkout_id,
        )
        order = lock_one(queryset=Order.objects.all(), rank=LockRank.PAYABLE, pk=auth_ref.order_id)
        payment = lock_one(queryset=Payment.objects.all(), rank=LockRank.PAYMENT, pk=auth_ref.payment_id)
        register_lock(LockRank.FINANCIAL_EVIDENCE, f"authorization:{auth_ref.pk:020d}")
        authorization = ExceptionalRecognitionAuthorization.objects.select_for_update().get(pk=auth_ref.pk)
        reservations = list(
            DigitalInventoryReservation.objects.filter(order=order)
            .select_related("checkout_line__digital_snapshot")
            .order_by("checkout_line_id", "-created_at")
        )
        line_ids = list(checkout.lines.order_by("pk").values_list("pk", flat=True))
        try:
            lineage = classify_digital_reservations(reservations)
        except DigitalReservationCardinalityError as exc:
            raise LatePaymentAdjudicationError(str(exc)) from exc
        current_by_line = lineage.current_by_line
        historical_by_line = lineage.original_by_line
        pool_ids = sorted(
            {
                historical_by_line[line_id].inventory_pool_id
                for line_id in line_ids
                if line_id in historical_by_line
            }
        )
        pools = lock_many(
            queryset=InventoryPool.objects.all(),
            rank=LockRank.COMMERCIAL_RESOURCE,
            pks=pool_ids,
        )
        pool_map = {pool.pk: pool for pool in pools}
        lock_many(
            queryset=DigitalInventoryReservation.objects.all(),
            rank=LockRank.RESERVATION,
            pks=DigitalInventoryReservation.objects.filter(order=order).values_list("pk", flat=True),
        )
        if set(historical_by_line) != set(line_ids):
            raise LatePaymentAdjudicationError("Original Digital reservation lineage is incomplete.")
        needed = {}
        for line_id in line_ids:
            if line_id not in current_by_line:
                pool_id = historical_by_line[line_id].inventory_pool_id
                needed[pool_id] = needed.get(pool_id, 0) + 1
        for pool_id, quantity in needed.items():
            held = (
                DigitalInventoryReservation.objects.filter(
                    inventory_pool_id=pool_id,
                    state__in=CURRENT_DIGITAL_RESERVATION_STATES,
                ).aggregate(total=Sum("quantity"))["total"]
                or 0
            )
            if pool_map[pool_id].sellable_quantity - held < quantity:
                return False
        now = timezone.now()
        extension = timedelta(
            seconds=int(getattr(settings, "FINANCIAL_LATE_PAYMENT_RECOVERY_HOLD_SECONDS", 1800))
        )
        recovery_expiry = max(checkout.expires_at, now + extension)
        checkout.expires_at = recovery_expiry
        checkout.maximum_expires_at = max(checkout.maximum_expires_at, recovery_expiry)
        checkout.save(update_fields=("expires_at", "maximum_expires_at", "updated_at"))
        for reservation in current_by_line.values():
            if reservation.state != DigitalInventoryReservationState.PAYMENT_HOLD:
                raise LatePaymentAdjudicationError(
                    "Late-payment recovery requires PAYMENT_HOLD or no current reservation."
                )
            reservation.state = DigitalInventoryReservationState.PAYMENT_HOLD
            reservation.expires_at = recovery_expiry
            reservation.state_changed_at = now
            reservation.resolution_reason = "late_payment_adjudication_retained"
            reservation.save(
                update_fields=(
                    "state", "expires_at", "state_changed_at", "resolution_reason", "updated_at"
                )
            )
        for line_id in line_ids:
            if line_id in current_by_line:
                continue
            original = historical_by_line[line_id]
            DigitalInventoryReservation.objects.create(
                checkout=checkout,
                checkout_line_id=line_id,
                order=order,
                inventory_pool_id=original.inventory_pool_id,
                quantity=1,
                state=DigitalInventoryReservationState.PAYMENT_HOLD,
                expires_at=recovery_expiry,
                state_changed_at=now,
                idempotency_key=uuid5(
                    ADJUDICATION_NAMESPACE,
                    f"replacement:{authorization.public_id}:{line_id}",
                ),
                resolution_reason="late_payment_adjudication_replacement",
                recovery_authorization=authorization,
            )
        return True


@transaction.atomic
def _resolve_inventory_review(*, review_id, authorization):
    review = ReviewCase.objects.select_for_update().get(pk=review_id)
    if review.status == ReviewCaseStatus.RESOLVED:
        return review
    key = uuid5(ADJUDICATION_NAMESPACE, f"inventory-review-resolved:{authorization.public_id}")
    ReviewAction.objects.get_or_create(
        idempotency_key=key,
        defaults={
            "review_case": review,
            "action_type": f"transition:{ReviewCaseStatus.RESOLVED}",
            "actor_type": FinancialActorType.SYSTEM,
            "reason_code": "inventory_recovered",
            "note": "Inventory claim recovered without decrement; finalization may proceed.",
        },
    )
    review.status = ReviewCaseStatus.RESOLVED
    review.resolution_code = "late_payment_inventory_recovered"
    review.resolved_at = timezone.now()
    review.version += 1
    review.save(update_fields=("status", "resolution_code", "resolved_at", "version", "updated_at"))
    return review


def apply_approved_terminal_late_payment(
    *,
    adjudication_public_id,
    idempotency_key,
    correlation_id,
    actor,
):
    """Recognize accepted funds into liability, then recover inventory; safe to retry."""
    actor_id = _staff_actor(actor)
    adjudication = LatePaymentAdjudication.objects.select_related(
        "recognition_authorization", "payment", "verification"
    ).get(public_id=adjudication_public_id)
    if adjudication.status != LatePaymentAdjudicationStatus.APPROVED:
        raise LatePaymentAdjudicationError("Only approved late-payment adjudication can be applied.")
    authorization = adjudication.recognition_authorization
    inventory_review = _ensure_inventory_review(authorization)
    _record_application_activity(
        review=inventory_review,
        authorization=authorization,
        action_type="late_payment.application_started",
        reason_code="approved_adjudication",
        note="Approved terminal late-payment application started.",
    )
    from cheatgame.financial_core.services.funds_application import recognize_verified_funds

    _uuid(idempotency_key, "idempotency_key")
    try:
        recognition = recognize_verified_funds(
            verification_id=adjudication.verification_id,
            idempotency_key=uuid5(
                ADJUDICATION_NAMESPACE,
                f"recognition:{authorization.public_id}",
            ),
            expected_payment_version=authorization.expected_payment_version,
            correlation_id=_uuid(correlation_id, "correlation_id"),
            causation_id=authorization.public_id,
            actor_type=FinancialActorType.RECONCILIATION,
            actor_id=actor_id,
        )
    except Exception:
        _record_application_activity(
            review=inventory_review,
            authorization=authorization,
            action_type="late_payment.application_failed",
            reason_code="recognition_failed",
            note="Exceptional recognition application failed; retry remains controlled.",
        )
        raise
    _record_application_activity(
        review=inventory_review,
        authorization=authorization,
        action_type="late_payment.recognition_resumed",
        reason_code="exceptional_authorization_applied",
        note="Exact adjudicated funds were recognized into customer liability.",
    )
    _record_application_activity(
        review=inventory_review,
        authorization=authorization,
        action_type="late_payment.inventory_attempted",
        reason_code="controlled_recovery",
        note="Controlled Digital inventory recovery attempted.",
    )
    recovered = recover_terminal_late_payment_inventory(authorization_id=authorization.pk)
    if recovered:
        _record_application_activity(
            review=inventory_review,
            authorization=authorization,
            action_type="late_payment.inventory_recovered",
            reason_code="inventory_recovered",
            note="Digital inventory authority recovered without decrement.",
        )
        inventory_review = _resolve_inventory_review(
            review_id=inventory_review.pk,
            authorization=authorization,
        )
        _record_application_activity(
            review=inventory_review,
            authorization=authorization,
            action_type="late_payment.application_completed",
            reason_code="ready_for_finalization",
            note="Adjudicated funds and inventory authority are ready for finalization.",
        )
    else:
        _record_application_activity(
            review=inventory_review,
            authorization=authorization,
            action_type="late_payment.inventory_unavailable",
            reason_code="paid_unfulfillable",
            note="Equivalent Digital inventory is unavailable; liability remains under review.",
        )
    return LatePaymentApplicationResult(
        adjudication=adjudication,
        allocation=recognition.allocation,
        inventory_recovered=recovered,
        inventory_review=inventory_review,
    )
