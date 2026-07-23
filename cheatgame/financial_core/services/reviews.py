from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from cheatgame.financial_core.models import (
    FinancialActorType,
    Payment,
    PaymentAttempt,
    PaymentTransaction,
    ReviewCase,
    ReviewAction,
    ReviewCaseStatus,
)
from cheatgame.financial_core.services.events import append_financial_event
from cheatgame.financial_core.services.locks import LockRank, lock_many, lock_one, ordered_lock_scope, register_lock
from cheatgame.financial_core.services.state_machines import assert_review_case_transition
from cheatgame.shop.models import Order


@transaction.atomic
def open_review_case(
    *,
    reason,
    severity,
    summary,
    idempotency_key,
    command_key,
    order_id=None,
    payment_id=None,
    attempt_id=None,
    transaction_id=None,
    opened_by_type=FinancialActorType.SYSTEM,
    opened_by_id=None,
):
    with ordered_lock_scope():
        existing = ReviewCase.objects.filter(idempotency_key=idempotency_key).first()
        if existing is not None:
            return existing

        if transaction_id is not None:
            tx_ref = PaymentTransaction.objects.select_related("attempt__payment").get(pk=transaction_id)
            attempt_id = attempt_id or tx_ref.attempt_id
            payment_id = payment_id or tx_ref.attempt.payment_id
            order_id = order_id or tx_ref.attempt.payment.order_id
        elif attempt_id is not None:
            attempt_ref = PaymentAttempt.objects.select_related("payment").get(pk=attempt_id)
            payment_id = payment_id or attempt_ref.payment_id
            order_id = order_id or attempt_ref.payment.order_id
        elif payment_id is not None:
            payment_ref = Payment.objects.get(pk=payment_id)
            order_id = order_id or payment_ref.order_id

        order = lock_one(queryset=Order.objects.all(), rank=LockRank.PAYABLE, pk=order_id) if order_id else None
        payment = (
            lock_one(queryset=Payment.objects.all(), rank=LockRank.PAYMENT, pk=payment_id)
            if payment_id
            else None
        )
        attempts = (
            lock_many(
                queryset=PaymentAttempt.objects.all(),
                rank=LockRank.PAYMENT_ATTEMPT,
                pks=PaymentAttempt.objects.filter(payment=payment).values_list("pk", flat=True),
            )
            if payment
            else []
        )
        attempt = next((item for item in attempts if item.pk == attempt_id), None)
        transactions = (
            lock_many(
                queryset=PaymentTransaction.objects.all(),
                rank=LockRank.PAYMENT_TRANSACTION,
                pks=PaymentTransaction.objects.filter(attempt__payment=payment).values_list("pk", flat=True),
            )
            if payment
            else []
        )
        transaction_obj = next((item for item in transactions if item.pk == transaction_id), None)
        register_lock(LockRank.REVIEW_CASE, f"review:new:{idempotency_key}")
        review = ReviewCase.objects.create(
            reason=reason,
            severity=severity,
            summary=str(summary)[:1000],
            idempotency_key=idempotency_key,
            order=order,
            payment=payment,
            attempt=attempt,
            transaction=transaction_obj,
            opened_by_type=opened_by_type,
            opened_by_id=opened_by_id,
        )
        register_lock(LockRank.EVENT_OUTBOX, f"event:review:{review.pk:020d}")
        append_financial_event(
            aggregate_type=review._meta.label_lower,
            aggregate_id=review.public_id,
            aggregate_version=review.version,
            event_type="review_case.opened",
            actor_type=opened_by_type,
            actor_id=opened_by_id,
            idempotency_key=command_key,
            metadata={"reason_code": reason, "severity": severity, "new_status": review.status},
        )
        return review


@transaction.atomic
def transition_review_case(
    *,
    review_case_id,
    target_status,
    actor_id,
    reason_code,
    idempotency_key,
    command_key,
    note="",
    resolution_code="",
    requires_approval=False,
    approved_by_id=None,
):
    if not actor_id:
        raise ValidationError("A named staff actor is required for ReviewCase transitions.")
    if target_status == ReviewCaseStatus.RESOLVED and not resolution_code:
        raise ValidationError("ReviewCase resolution requires a resolution code.")
    if requires_approval and approved_by_id is None:
        raise ValidationError("This ReviewCase action requires an approving staff actor.")
    if approved_by_id is not None and approved_by_id == actor_id:
        raise ValidationError("Maker and checker must be different users.")

    with ordered_lock_scope():
        review_ref = ReviewCase.objects.only(
            "order_id", "payment_id", "attempt_id", "transaction_id"
        ).get(pk=review_case_id)
        if review_ref.order_id:
            lock_one(queryset=Order.objects.all(), rank=LockRank.PAYABLE, pk=review_ref.order_id)
        payment = None
        if review_ref.payment_id:
            payment = lock_one(
                queryset=Payment.objects.all(),
                rank=LockRank.PAYMENT,
                pk=review_ref.payment_id,
            )
        if payment:
            lock_many(
                queryset=PaymentAttempt.objects.all(),
                rank=LockRank.PAYMENT_ATTEMPT,
                pks=PaymentAttempt.objects.filter(payment=payment).values_list("pk", flat=True),
            )
            lock_many(
                queryset=PaymentTransaction.objects.all(),
                rank=LockRank.PAYMENT_TRANSACTION,
                pks=PaymentTransaction.objects.filter(attempt__payment=payment).values_list("pk", flat=True),
            )
        review = lock_one(
            queryset=ReviewCase.objects.all(),
            rank=LockRank.REVIEW_CASE,
            pk=review_case_id,
        )
        replay = ReviewAction.objects.filter(idempotency_key=idempotency_key).first()
        if replay is not None:
            if replay.review_case_id != review.pk or replay.action_type != f"transition:{target_status}":
                raise ValidationError("ReviewAction idempotency key conflicts with another command.")
            return review

        assert_review_case_transition(review.status, target_status)
        previous_status = review.status
        ReviewAction.objects.create(
            review_case=review,
            action_type=f"transition:{target_status}",
            actor_id=actor_id,
            reason_code=reason_code,
            note=str(note)[:1000],
            requires_approval=requires_approval,
            approved_by_id=approved_by_id,
            idempotency_key=idempotency_key,
        )
        review.status = target_status
        review.version += 1
        update_fields = ["status", "version", "updated_at"]
        if target_status == ReviewCaseStatus.RESOLVED:
            review.resolution_code = resolution_code
            review.resolved_at = timezone.now()
            update_fields.extend(("resolution_code", "resolved_at"))
        review.save(update_fields=update_fields)
        register_lock(LockRank.EVENT_OUTBOX, f"event:review:{review.pk:020d}:{review.version:020d}")
        append_financial_event(
            aggregate_type=review._meta.label_lower,
            aggregate_id=review.public_id,
            aggregate_version=review.version,
            event_type="review_case.status_changed",
            actor_type=FinancialActorType.ADMIN,
            actor_id=actor_id,
            idempotency_key=command_key,
            metadata={
                "previous_status": previous_status,
                "new_status": target_status,
                "reason_code": reason_code,
            },
        )
        return review
