from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction

from cheatgame.financial_core.models import (
    CANONICAL_CURRENCY,
    FinancialActorType,
    Payment,
    PaymentAttempt,
    PaymentAttemptStatus,
    PaymentCollectionStatus,
    PaymentTransaction,
    PaymentTransactionStatus,
)
from cheatgame.financial_core.services.events import append_financial_event
from cheatgame.financial_core.services.locks import (
    LockRank,
    lock_many,
    lock_one,
    ordered_lock_scope,
    register_lock,
)
from cheatgame.financial_core.services.state_machines import (
    assert_payment_attempt_transition,
    assert_payment_transaction_transition,
    assert_payment_transition,
)
from cheatgame.shop.models import Order


BLOCKING_ATTEMPT_STATES = (
    PaymentAttemptStatus.CREATED,
    PaymentAttemptStatus.REQUIRES_CUSTOMER_ACTION,
    PaymentAttemptStatus.PROCESSING,
    PaymentAttemptStatus.SUCCEEDED,
    PaymentAttemptStatus.OUTCOME_UNKNOWN,
    PaymentAttemptStatus.REVIEW,
)


def _canonical_currency(value, *, field="currency"):
    normalized = str(value).upper()
    if normalized != CANONICAL_CURRENCY:
        raise ValidationError(
            {field: "C1 accepts canonical IRR only; the legacy IRT compatibility bridge is not implemented."}
        )
    return normalized


def _event(*, aggregate, event_type, actor_type, actor_id, command_key, metadata):
    register_lock(LockRank.EVENT_OUTBOX, f"event:{aggregate._meta.label_lower}:{aggregate.pk:020d}")
    return append_financial_event(
        aggregate_type=aggregate._meta.label_lower,
        aggregate_id=aggregate.public_id,
        aggregate_version=aggregate.version,
        event_type=event_type,
        actor_type=actor_type,
        actor_id=actor_id,
        idempotency_key=command_key,
        metadata=metadata,
    )


@transaction.atomic
def create_payment_for_order(
    *,
    order_id,
    amount_due,
    currency,
    command_key,
    actor_type=FinancialActorType.SYSTEM,
    actor_id=None,
):
    amount_due = Decimal(str(amount_due))
    currency = _canonical_currency(currency)
    with ordered_lock_scope():
        order = lock_one(queryset=Order.objects.all(), rank=LockRank.PAYABLE, pk=order_id)
        existing = Payment.objects.filter(order=order).first()
        if existing is not None:
            if existing.amount_due != amount_due or existing.currency != currency:
                raise ValidationError("Order already has a different immutable Payment obligation.")
            return existing
        payment = Payment.objects.create(order=order, amount_due=amount_due, currency=currency)
        _event(
            aggregate=payment,
            event_type="payment.opened",
            actor_type=actor_type,
            actor_id=actor_id,
            command_key=command_key,
            metadata={"new_status": payment.collection_status, "amount": amount_due, "currency": currency},
        )
        return payment


@transaction.atomic
def create_payment_attempt(
    *,
    payment_id,
    requested_amount,
    currency,
    tender_type,
    provider,
    merchant_account_ref,
    idempotency_key,
    request_hash,
    command_key,
    actor_type=FinancialActorType.CUSTOMER,
    actor_id=None,
):
    currency = _canonical_currency(currency)
    with ordered_lock_scope():
        order_id = Payment.objects.only("order_id").get(pk=payment_id).order_id
        lock_one(queryset=Order.objects.all(), rank=LockRank.PAYABLE, pk=order_id)
        payment = lock_one(queryset=Payment.objects.all(), rank=LockRank.PAYMENT, pk=payment_id)
        attempts = lock_many(
            queryset=PaymentAttempt.objects.all(),
            rank=LockRank.PAYMENT_ATTEMPT,
            pks=PaymentAttempt.objects.filter(payment=payment).values_list("pk", flat=True),
        )
        for attempt in attempts:
            if attempt.idempotency_key == idempotency_key:
                if attempt.request_hash != request_hash:
                    raise ValidationError("PaymentAttempt idempotency key conflicts with another request.")
                return attempt
            if attempt.status in BLOCKING_ATTEMPT_STATES:
                raise ValidationError("Payment has a live, successful, unknown, or review attempt.")
        if payment.collection_status not in (
            PaymentCollectionStatus.OPEN,
            PaymentCollectionStatus.PROCESSING,
            PaymentCollectionStatus.PARTIALLY_PAID,
        ):
            raise ValidationError("Payment cannot accept a new attempt in its current state.")
        sequence = attempts[-1].sequence + 1 if attempts else 1
        attempt = PaymentAttempt.objects.create(
            payment=payment,
            sequence=sequence,
            requested_amount=Decimal(str(requested_amount)),
            currency=currency,
            tender_type=tender_type,
            provider=str(provider),
            merchant_account_ref=str(merchant_account_ref),
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        _event(
            aggregate=attempt,
            event_type="payment_attempt.created",
            actor_type=actor_type,
            actor_id=actor_id,
            command_key=command_key,
            metadata={
                "new_status": attempt.status,
                "provider": attempt.provider,
                "amount": attempt.requested_amount,
                "currency": attempt.currency,
                "sequence": attempt.sequence,
            },
        )
        return attempt


@transaction.atomic
def create_payment_transaction(
    *,
    attempt_id,
    operation_type,
    provider,
    merchant_account_ref,
    merchant_reference,
    amount,
    currency,
    provider_amount,
    provider_unit,
    idempotency_key,
    command_key,
    parent_id=None,
    actor_type=FinancialActorType.SYSTEM,
    actor_id=None,
):
    amount = Decimal(str(amount))
    currency = _canonical_currency(currency)
    provider_amount = Decimal(str(provider_amount))
    provider_unit = _canonical_currency(provider_unit, field="provider_unit")
    with ordered_lock_scope():
        attempt_ref = PaymentAttempt.objects.select_related("payment").only("payment_id", "payment__order_id").get(
            pk=attempt_id
        )
        lock_one(queryset=Order.objects.all(), rank=LockRank.PAYABLE, pk=attempt_ref.payment.order_id)
        payment = lock_one(
            queryset=Payment.objects.all(), rank=LockRank.PAYMENT, pk=attempt_ref.payment_id
        )
        attempts = lock_many(
            queryset=PaymentAttempt.objects.all(),
            rank=LockRank.PAYMENT_ATTEMPT,
            pks=PaymentAttempt.objects.filter(payment=payment).values_list("pk", flat=True),
        )
        attempt = next(item for item in attempts if item.pk == attempt_id)
        transactions = lock_many(
            queryset=PaymentTransaction.objects.all(),
            rank=LockRank.PAYMENT_TRANSACTION,
            pks=PaymentTransaction.objects.filter(attempt=attempt).values_list("pk", flat=True),
        )
        for transaction_obj in transactions:
            if transaction_obj.idempotency_key == idempotency_key:
                replay_identity = {
                    "operation_type": operation_type,
                    "parent_id": parent_id,
                    "provider": provider,
                    "merchant_account_ref": merchant_account_ref,
                    "merchant_reference": merchant_reference,
                    "amount": amount,
                    "currency": currency,
                    "provider_amount": provider_amount,
                    "provider_unit": provider_unit,
                }
                if any(
                    getattr(transaction_obj, field) != value
                    for field, value in replay_identity.items()
                ):
                    raise ValidationError(
                        "PaymentTransaction idempotency key conflicts with another request."
                    )
                return transaction_obj
        sequence = transactions[-1].sequence + 1 if transactions else 1
        parent = None
        if parent_id is not None:
            parent = next((item for item in transactions if item.pk == parent_id), None)
            if parent is None:
                raise ValidationError("Parent transaction is not locked under this PaymentAttempt.")
        transaction_obj = PaymentTransaction.objects.create(
            attempt=attempt,
            sequence=sequence,
            operation_type=operation_type,
            parent=parent,
            provider=provider,
            merchant_account_ref=merchant_account_ref,
            merchant_reference=merchant_reference,
            amount=amount,
            currency=currency,
            provider_amount=provider_amount,
            provider_unit=provider_unit,
            idempotency_key=idempotency_key,
        )
        _event(
            aggregate=transaction_obj,
            event_type="payment_transaction.created",
            actor_type=actor_type,
            actor_id=actor_id,
            command_key=command_key,
            metadata={
                "new_status": transaction_obj.status,
                "operation_type": transaction_obj.operation_type,
                "provider": transaction_obj.provider,
                "amount": transaction_obj.amount,
                "currency": transaction_obj.currency,
                "sequence": transaction_obj.sequence,
            },
        )
        return transaction_obj


@transaction.atomic
def transition_payment(
    *,
    payment_id,
    target_status,
    command_key,
    confirmed_amount=None,
    actor_type=FinancialActorType.SYSTEM,
    actor_id=None,
    reason_code="",
):
    if confirmed_amount is not None:
        raise ValidationError(
            "Confirmed funds may only be written by the verified journaled finalizer and are dormant in C1."
        )
    if target_status in (
        PaymentCollectionStatus.PAID_PENDING_FINALIZATION,
        PaymentCollectionStatus.PAID,
    ):
        raise ValidationError(
            "Paid Payment transitions are reserved for the verified journaled finalizer and are dormant in C1."
        )
    with ordered_lock_scope():
        order_id = Payment.objects.only("order_id").get(pk=payment_id).order_id
        lock_one(queryset=Order.objects.all(), rank=LockRank.PAYABLE, pk=order_id)
        payment = lock_one(queryset=Payment.objects.all(), rank=LockRank.PAYMENT, pk=payment_id)
        previous = payment.collection_status
        assert_payment_transition(previous, target_status)
        if previous == target_status:
            return payment
        payment.collection_status = target_status
        payment.version += 1
        payment.save(update_fields=("collection_status", "version", "updated_at"))
        _event(
            aggregate=payment,
            event_type="payment.status_changed",
            actor_type=actor_type,
            actor_id=actor_id,
            command_key=command_key,
            metadata={"previous_status": previous, "new_status": target_status, "reason_code": reason_code},
        )
        return payment


@transaction.atomic
def transition_payment_attempt(
    *,
    attempt_id,
    target_status,
    command_key,
    actor_type=FinancialActorType.SYSTEM,
    actor_id=None,
    reason_code="",
):
    if target_status == PaymentAttemptStatus.SUCCEEDED:
        raise ValidationError(
            "Successful PaymentAttempt transitions are reserved for verified payment finalization."
        )
    with ordered_lock_scope():
        attempt_ref = PaymentAttempt.objects.select_related("payment").only("payment_id", "payment__order_id").get(
            pk=attempt_id
        )
        lock_one(queryset=Order.objects.all(), rank=LockRank.PAYABLE, pk=attempt_ref.payment.order_id)
        payment = lock_one(
            queryset=Payment.objects.all(), rank=LockRank.PAYMENT, pk=attempt_ref.payment_id
        )
        attempts = lock_many(
            queryset=PaymentAttempt.objects.all(),
            rank=LockRank.PAYMENT_ATTEMPT,
            pks=PaymentAttempt.objects.filter(payment=payment).values_list("pk", flat=True),
        )
        attempt = next(item for item in attempts if item.pk == attempt_id)
        previous = attempt.status
        assert_payment_attempt_transition(previous, target_status)
        if previous == target_status:
            return attempt
        attempt.status = target_status
        attempt.version += 1
        attempt.save(update_fields=("status", "version", "updated_at"))
        _event(
            aggregate=attempt,
            event_type="payment_attempt.status_changed",
            actor_type=actor_type,
            actor_id=actor_id,
            command_key=command_key,
            metadata={"previous_status": previous, "new_status": target_status, "reason_code": reason_code},
        )
        return attempt


@transaction.atomic
def transition_payment_transaction(
    *,
    transaction_id,
    target_status,
    command_key,
    actor_type=FinancialActorType.SYSTEM,
    actor_id=None,
    reason_code="",
):
    if target_status == PaymentTransactionStatus.SUCCEEDED:
        raise ValidationError(
            "Successful PaymentTransaction transitions require provider verification and are dormant in C1."
        )
    with ordered_lock_scope():
        transaction_ref = PaymentTransaction.objects.select_related("attempt__payment").only(
            "attempt_id", "attempt__payment_id", "attempt__payment__order_id"
        ).get(pk=transaction_id)
        lock_one(
            queryset=Order.objects.all(),
            rank=LockRank.PAYABLE,
            pk=transaction_ref.attempt.payment.order_id,
        )
        payment = lock_one(
            queryset=Payment.objects.all(),
            rank=LockRank.PAYMENT,
            pk=transaction_ref.attempt.payment_id,
        )
        lock_many(
            queryset=PaymentAttempt.objects.all(),
            rank=LockRank.PAYMENT_ATTEMPT,
            pks=PaymentAttempt.objects.filter(payment=payment).values_list("pk", flat=True),
        )
        transaction_ids = PaymentTransaction.objects.filter(attempt__payment=payment).values_list("pk", flat=True)
        transactions = lock_many(
            queryset=PaymentTransaction.objects.all(),
            rank=LockRank.PAYMENT_TRANSACTION,
            pks=transaction_ids,
        )
        transaction_obj = next(item for item in transactions if item.pk == transaction_id)
        previous = transaction_obj.status
        assert_payment_transaction_transition(previous, target_status)
        if previous == target_status:
            return transaction_obj
        transaction_obj.status = target_status
        if target_status in (
            PaymentTransactionStatus.DECLINED,
            PaymentTransactionStatus.CANCELED,
            PaymentTransactionStatus.EXPIRED,
        ):
            from django.utils import timezone

            transaction_obj.completed_at = timezone.now()
        transaction_obj.version += 1
        transaction_obj.save(update_fields=("status", "completed_at", "version", "updated_at"))
        _event(
            aggregate=transaction_obj,
            event_type="payment_transaction.status_changed",
            actor_type=actor_type,
            actor_id=actor_id,
            command_key=command_key,
            metadata={"previous_status": previous, "new_status": target_status, "reason_code": reason_code},
        )
        return transaction_obj
