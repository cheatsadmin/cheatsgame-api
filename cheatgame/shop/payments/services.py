from decimal import Decimal
from uuid import uuid4

from django.db import transaction
from django.utils import timezone

from cheatgame.shop.models import Order, OrderStatus, PaymentTransaction, PaymentTransactionStatus
from cheatgame.shop.payments.providers import PaymentProvider, PaymentProviderError, get_payment_provider


class PaymentError(Exception):
    pass


ACTIVE_PAYMENT_STATUSES = (
    PaymentTransactionStatus.CREATED,
    PaymentTransactionStatus.PENDING,
    PaymentTransactionStatus.CALLBACK_RECEIVED,
)


def get_user_payment_transaction(*, transaction_id: int, user) -> PaymentTransaction:
    transaction_obj = PaymentTransaction.objects.filter(id=transaction_id, user=user).select_related("order").first()
    if transaction_obj is None:
        raise PaymentError("تراکنش پرداخت یافت نشد.")
    return transaction_obj


def serialize_payment_transaction(transaction_obj: PaymentTransaction) -> dict:
    return {
        "id": transaction_obj.id,
        "order": transaction_obj.order_id,
        "provider": transaction_obj.provider,
        "amount": transaction_obj.amount,
        "status": transaction_obj.status,
        "gateway_authority": transaction_obj.gateway_authority,
        "gateway_ref_id": transaction_obj.gateway_ref_id,
        "gateway_trace_no": transaction_obj.gateway_trace_no,
        "gateway_payment_url": transaction_obj.gateway_payment_url,
        "error_code": transaction_obj.error_code,
        "error_message": transaction_obj.error_message,
        "paid_at": transaction_obj.paid_at,
        "verified_at": transaction_obj.verified_at,
        "created_at": transaction_obj.created_at,
        "updated_at": transaction_obj.updated_at,
    }


def get_latest_order_transaction_summary(*, order: Order) -> dict:
    transaction_obj = order.payment_transactions.order_by("-created_at").first()
    if transaction_obj is None:
        return None
    return serialize_payment_transaction(transaction_obj)


@transaction.atomic
def create_payment_request(
    *, order_id: int, user, callback_url: str, success_redirect_url: str = ""
) -> PaymentTransaction:
    try:
        provider = get_payment_provider()
    except PaymentProviderError as error:
        raise PaymentError(str(error)) from error
    order = Order.objects.select_for_update().filter(id=order_id, user=user).first()
    if order is None:
        raise PaymentError("سفارش یافت نشد.")
    if order.payment_status == OrderStatus.PAID.value:
        raise PaymentError("این سفارش قبلا پرداخت شده است.")

    amount = order.total_price_discount
    if amount is None:
        amount = order.total_price
    if amount <= Decimal("0"):
        raise PaymentError("مبلغ سفارش برای پرداخت معتبر نیست.")

    transaction_obj = PaymentTransaction.objects.filter(
        order=order,
        user=user,
        provider=provider.name,
        status__in=ACTIVE_PAYMENT_STATUSES,
    ).order_by("-created_at").first()

    if transaction_obj is None:
        transaction_obj = PaymentTransaction.objects.create(
            order=order,
            user=user,
            provider=provider.name,
            amount=amount,
            status=PaymentTransactionStatus.CREATED,
            idempotency_key=f"{provider.name}:{order.id}:{uuid4()}",
        )

    if transaction_obj.status == PaymentTransactionStatus.CREATED or not transaction_obj.gateway_payment_url:
        try:
            request_result = provider.create_payment_request(
                transaction=transaction_obj,
                callback_url=callback_url,
            )
        except PaymentProviderError as error:
            raise PaymentError(str(error)) from error
        request_payload = dict(request_result.payload)
        if success_redirect_url:
            request_payload["success_redirect_url"] = success_redirect_url
        transaction_obj.gateway_authority = request_result.authority
        transaction_obj.gateway_payment_url = request_result.payment_url
        transaction_obj.request_payload = request_payload
        transaction_obj.status = PaymentTransactionStatus.PENDING
        transaction_obj.save(
            update_fields=[
                "gateway_authority",
                "gateway_payment_url",
                "request_payload",
                "status",
                "updated_at",
            ]
        )
    elif (
        success_redirect_url
        and transaction_obj.request_payload.get("success_redirect_url") != success_redirect_url
    ):
        transaction_obj.request_payload = {
            **transaction_obj.request_payload,
            "success_redirect_url": success_redirect_url,
        }
        transaction_obj.save(update_fields=["request_payload", "updated_at"])

    return transaction_obj


@transaction.atomic
def record_payment_callback(*, provider: PaymentProvider, query_params) -> PaymentTransaction:
    callback_result = provider.parse_callback(query_params=query_params)
    if not callback_result.authority:
        raise PaymentError("شناسه پرداخت در callback ارسال نشده است.")

    transaction_obj = PaymentTransaction.objects.select_for_update().filter(
        provider=provider.name,
        gateway_authority=callback_result.authority,
    ).first()
    if transaction_obj is None:
        raise PaymentError("تراکنش پرداخت یافت نشد.")

    transaction_obj.callback_payload = callback_result.payload
    if transaction_obj.status not in (PaymentTransactionStatus.PAID, PaymentTransactionStatus.FAILED):
        transaction_obj.status = PaymentTransactionStatus.CALLBACK_RECEIVED
        transaction_obj.save(update_fields=["callback_payload", "status", "updated_at"])
    else:
        transaction_obj.save(update_fields=["callback_payload", "updated_at"])
    return transaction_obj


@transaction.atomic
def verify_payment(*, transaction_id: int, user) -> PaymentTransaction:
    transaction_obj = PaymentTransaction.objects.select_for_update().select_related("order").filter(
        id=transaction_id,
        user=user,
    ).first()
    if transaction_obj is None:
        raise PaymentError("تراکنش پرداخت یافت نشد.")

    order = Order.objects.select_for_update().get(id=transaction_obj.order_id)
    if transaction_obj.status == PaymentTransactionStatus.PAID:
        if order.payment_status != OrderStatus.PAID.value:
            order.payment_status = OrderStatus.PAID.value
            order.save(update_fields=["payment_status", "updated_at"])
        return transaction_obj
    if transaction_obj.status == PaymentTransactionStatus.FAILED:
        return transaction_obj

    try:
        provider = get_payment_provider(provider=transaction_obj.provider)
        verify_result = provider.verify(transaction=transaction_obj)
    except PaymentProviderError as error:
        raise PaymentError(str(error)) from error
    now = timezone.now()

    transaction_obj.verify_payload = verify_result.payload
    transaction_obj.verified_at = now
    transaction_obj.error_code = verify_result.error_code
    transaction_obj.error_message = verify_result.error_message

    if verify_result.is_paid:
        transaction_obj.status = PaymentTransactionStatus.PAID
        transaction_obj.gateway_ref_id = verify_result.ref_id
        transaction_obj.gateway_trace_no = verify_result.trace_no
        transaction_obj.paid_at = now
        transaction_obj.save(
            update_fields=[
                "verify_payload",
                "verified_at",
                "error_code",
                "error_message",
                "status",
                "gateway_ref_id",
                "gateway_trace_no",
                "paid_at",
                "updated_at",
            ]
        )
        order.payment_status = OrderStatus.PAID.value
        order.save(update_fields=["payment_status", "updated_at"])
        return transaction_obj

    transaction_obj.status = PaymentTransactionStatus.FAILED
    transaction_obj.save(
        update_fields=[
            "verify_payload",
            "verified_at",
            "error_code",
            "error_message",
            "status",
            "updated_at",
        ]
    )
    if order.payment_status != OrderStatus.PAID.value:
        order.payment_status = OrderStatus.FAIDED.value
        order.save(update_fields=["payment_status", "updated_at"])
    return transaction_obj
