from decimal import Decimal

from cheatgame.financial_core.models import (
    IdempotencyRecord,
    IdempotencyStatus,
    PaymentAttemptStatus,
    PaymentCollectionStatus,
    PaymentTransactionStatus,
)


BLOCKING_ATTEMPT_STATES = frozenset(
    {
        PaymentAttemptStatus.CREATED,
        PaymentAttemptStatus.PROCESSING,
        PaymentAttemptStatus.REQUIRES_CUSTOMER_ACTION,
        PaymentAttemptStatus.OUTCOME_UNKNOWN,
        PaymentAttemptStatus.REVIEW,
    }
)


def payment_request_scope(checkout_id):
    return f"digital_api:payment_request:{int(checkout_id)}"


def _current_attempt(payment):
    attempts = list(payment.attempts.all())
    return max(attempts, key=lambda item: item.sequence) if attempts else None


def _current_transaction(attempt):
    if attempt is None:
        return None
    transactions = list(attempt.transactions.all())
    return max(transactions, key=lambda item: item.sequence) if transactions else None


def _handoff(checkout, transaction_obj):
    if transaction_obj is None or transaction_obj.status != PaymentTransactionStatus.PENDING_CUSTOMER:
        return ""
    records = IdempotencyRecord.objects.filter(
        scope=payment_request_scope(checkout.pk),
        status=IdempotencyStatus.COMPLETED,
        result_type=transaction_obj._meta.label_lower,
        result_id=str(transaction_obj.pk),
    ).order_by("-completed_at", "-pk")
    for record in records:
        url = record.safe_response.get("customer_action_url", "")
        if isinstance(url, str) and url:
            return url
    return ""


def digital_payment_projection(checkout, *, replayed=False, customer_action_url=None):
    orders = list(checkout.orders.all())
    order = orders[0] if len(orders) == 1 else None
    payment = getattr(order, "financial_payment", None) if order is not None else None
    attempt = _current_attempt(payment) if payment is not None else None
    transaction_obj = _current_transaction(attempt)
    if customer_action_url is None:
        customer_action_url = _handoff(checkout, transaction_obj)
    if transaction_obj is None or transaction_obj.status != PaymentTransactionStatus.PENDING_CUSTOMER:
        customer_action_url = ""

    blocking = bool(attempt and attempt.status in BLOCKING_ATTEMPT_STATES)
    can_retry = bool(
        payment
        and payment.collection_status == PaymentCollectionStatus.OPEN
        and not blocking
        and (attempt is None or attempt.status == PaymentAttemptStatus.DEFINITIVE_FAILED)
    )
    do_not_pay_again = bool(
        payment
        and (
            payment.collection_status == PaymentCollectionStatus.REVIEW
            or (attempt and attempt.status in (PaymentAttemptStatus.OUTCOME_UNKNOWN, PaymentAttemptStatus.REVIEW))
            or (
                attempt
                and attempt.status == PaymentAttemptStatus.PROCESSING
                and transaction_obj
                and transaction_obj.status == PaymentTransactionStatus.REQUESTING
            )
        )
    )
    payment_received = bool(
        payment
        and payment.collection_status
        in (PaymentCollectionStatus.PAID_PENDING_FINALIZATION, PaymentCollectionStatus.PAID)
    )
    return {
        "checkout_id": str(checkout.public_id),
        "checkout_status": checkout.status,
        "order_reference": order.public_tracking_code if order else "",
        "payment_id": str(payment.public_id) if payment else None,
        "payment_status": payment.collection_status if payment else None,
        "amount_due": str(Decimal(payment.amount_due).quantize(Decimal("1"))) if payment else None,
        "currency": payment.currency if payment else None,
        "attempt_id": str(attempt.public_id) if attempt else None,
        "attempt_status": attempt.status if attempt else None,
        "transaction_id": str(transaction_obj.public_id) if transaction_obj else None,
        "transaction_status": transaction_obj.status if transaction_obj else None,
        "provider": attempt.provider if attempt else None,
        "customer_action_url": customer_action_url or None,
        "can_retry": can_retry,
        "do_not_pay_again": do_not_pay_again,
        "payment_received": payment_received,
        "replayed": bool(replayed),
    }
