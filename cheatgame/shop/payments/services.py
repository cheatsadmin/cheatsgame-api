from decimal import Decimal
from uuid import uuid4

from django.db import transaction
from django.utils import timezone

from cheatgame.shop.models import (
    Cart,
    CartLockReason,
    CartState,
    Checkout,
    CheckoutStatus,
    ManualReviewReason,
    Order,
    OrderStatus,
    PaymentTransaction,
    PaymentTransactionStatus,
)
from cheatgame.shop.payments.providers import PaymentProvider, PaymentProviderError, get_payment_provider
from cheatgame.shop.services.order import (
    DeliverySlotUnavailableError,
    DiscountUnavailableError,
    ShippingUnavailableError,
    StockUnavailableError,
    commit_order_delivery_slot,
    commit_order_discount_usage,
    commit_order_stock,
    ensure_order_delivery_slot_available,
    ensure_order_discount_available,
    ensure_order_shipping_ready,
    ensure_order_stock_available,
)


class PaymentError(Exception):
    pass


ACTIVE_PAYMENT_STATUSES = (
    PaymentTransactionStatus.CREATED,
    PaymentTransactionStatus.PENDING,
    PaymentTransactionStatus.CALLBACK_RECEIVED,
    PaymentTransactionStatus.VERIFYING,
    PaymentTransactionStatus.REQUIRES_MANUAL_REVIEW,
)


def _legacy_identity_for_order(order_id):
    return Order.objects.values("id", "checkout_id", "checkout__cart_id").get(pk=order_id)


def _lock_legacy_order_graph(identity):
    cart = None
    checkout = None
    if identity["checkout__cart_id"] is not None:
        cart = Cart.objects.select_for_update().get(pk=identity["checkout__cart_id"])
    if identity["checkout_id"] is not None:
        checkout = Checkout.objects.select_for_update().get(pk=identity["checkout_id"])
    order = Order.objects.select_for_update().get(pk=identity["id"])
    return cart, checkout, order


def _mark_legacy_review(*, transaction_id, claim_token, reason, message, paid_evidence=False, verify_result=None):
    identity = PaymentTransaction.objects.values(
        "order_id", "order__checkout_id", "order__checkout__cart_id"
    ).get(pk=transaction_id)
    order_identity = {
        "id": identity["order_id"],
        "checkout_id": identity["order__checkout_id"],
        "checkout__cart_id": identity["order__checkout__cart_id"],
    }
    with transaction.atomic():
        cart, checkout, order = _lock_legacy_order_graph(order_identity)
        transaction_obj = PaymentTransaction.objects.select_for_update().get(pk=transaction_id)
        if transaction_obj.verification_claim_token != claim_token:
            return transaction_obj
        now = timezone.now()
        transaction_obj.status = PaymentTransactionStatus.REQUIRES_MANUAL_REVIEW
        transaction_obj.manual_review_reason = reason
        transaction_obj.manual_review_message = str(message)[:2000]
        transaction_obj.error_code = "provider_paid_local_finalization_failed" if paid_evidence else "provider_outcome_unclear"
        transaction_obj.error_message = str(message)
        transaction_obj.verification_claim_token = None
        transaction_obj.verification_claimed_at = None
        update_fields = [
            "status", "manual_review_reason", "manual_review_message", "error_code", "error_message",
            "verification_claim_token", "verification_claimed_at", "updated_at",
        ]
        if verify_result is not None:
            transaction_obj.verify_payload = verify_result.payload
            transaction_obj.verified_at = now
            transaction_obj.gateway_ref_id = verify_result.ref_id
            transaction_obj.gateway_trace_no = verify_result.trace_no
            update_fields.extend(("verify_payload", "verified_at", "gateway_ref_id", "gateway_trace_no"))
            if paid_evidence:
                transaction_obj.paid_at = now
                update_fields.append("paid_at")
        transaction_obj.save(update_fields=update_fields)
        if checkout is not None:
            checkout.status = CheckoutStatus.REQUIRES_MANUAL_REVIEW
            checkout.manual_review_reason = reason
            checkout.manual_review_message = str(message)[:2000]
            checkout.version += 1
            checkout.save(update_fields=(
                "status", "manual_review_reason", "manual_review_message", "version", "updated_at"
            ))
        if cart is not None and cart.active_checkout_id in (None, getattr(checkout, "pk", None)):
            cart.state = CartState.LOCKED
            cart.lock_reason = CartLockReason.MANUAL_REVIEW
            cart.active_checkout = checkout
            cart.locked_at = cart.locked_at or now
            cart.lock_version += 1
            cart.save(update_fields=(
                "state", "lock_reason", "active_checkout", "locked_at", "lock_version", "updated_at"
            ))
        if order.payment_status != OrderStatus.PAID.value:
            order.payment_status = OrderStatus.PENDDING.value
            order.save(update_fields=("payment_status", "updated_at"))
        return transaction_obj


def get_user_payment_transaction(*, transaction_id: int, user) -> PaymentTransaction:
    transaction_obj = PaymentTransaction.objects.filter(id=transaction_id, user=user).select_related("order").first()
    if transaction_obj is None:
        raise PaymentError("تراکنش پرداخت یافت نشد.")
    return transaction_obj


def serialize_payment_transaction(transaction_obj: PaymentTransaction) -> dict:
    return {
        "id": transaction_obj.id,
        "order": transaction_obj.order_id,
        "order_public_tracking_code": transaction_obj.order.public_tracking_code,
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


def create_payment_request(
    *, order_id: int, user, callback_url: str, success_redirect_url: str = ""
) -> PaymentTransaction:
    try:
        provider = get_payment_provider()
    except PaymentProviderError as error:
        raise PaymentError(str(error)) from error
    try:
        identity = _legacy_identity_for_order(order_id)
    except Order.DoesNotExist as exc:
        raise PaymentError("سفارش یافت نشد.") from exc
    claim_token = uuid4()
    with transaction.atomic():
        _, _, order = _lock_legacy_order_graph(identity)
        if order.user_id != user.id:
            raise PaymentError("سفارش یافت نشد.")
        if hasattr(order, "financial_payment"):
            raise PaymentError("این سفارش تحت مالکیت Financial Core است.")
        if order.payment_status == OrderStatus.PAID.value:
            raise PaymentError("این سفارش قبلا پرداخت شده است.")
        try:
            ensure_order_stock_available(order=order, lock=True)
            ensure_order_shipping_ready(order=order, lock=True)
            ensure_order_delivery_slot_available(order=order, lock=True)
            ensure_order_discount_available(order=order, lock=True)
        except (
            StockUnavailableError,
            ShippingUnavailableError,
            DeliverySlotUnavailableError,
            DiscountUnavailableError,
        ) as error:
            raise PaymentError(str(error)) from error

        amount = order.total_price_discount if order.total_price_discount is not None else order.total_price
        if amount <= Decimal("0"):
            raise PaymentError("مبلغ سفارش برای پرداخت معتبر نیست.")
        transaction_obj = PaymentTransaction.objects.select_for_update().filter(
            order=order, user=user, provider=provider.name, status__in=ACTIVE_PAYMENT_STATUSES
        ).order_by("-created_at").first()
        if transaction_obj is not None and transaction_obj.status == PaymentTransactionStatus.REQUIRES_MANUAL_REVIEW:
            raise PaymentError("وضعیت پرداخت نیازمند بررسی است و درخواست مجدد مجاز نیست.")
        if transaction_obj is not None and transaction_obj.gateway_payment_url:
            if (
                success_redirect_url
                and transaction_obj.request_payload.get("success_redirect_url") != success_redirect_url
            ):
                transaction_obj.request_payload = {
                    **transaction_obj.request_payload,
                    "success_redirect_url": success_redirect_url,
                }
                transaction_obj.save(update_fields=("request_payload", "updated_at"))
            return transaction_obj
        if transaction_obj is None:
            transaction_obj = PaymentTransaction.objects.create(
                order=order,
                user=user,
                provider=provider.name,
                amount=amount,
                status=PaymentTransactionStatus.CREATED,
                idempotency_key=f"{provider.name}:{order.id}:{uuid4()}",
            )
        if transaction_obj.verification_claim_token is not None:
            raise PaymentError("درخواست پرداخت در حال پردازش است.")
        transaction_obj.verification_claim_token = claim_token
        transaction_obj.verification_claimed_at = timezone.now()
        transaction_obj.save(update_fields=("verification_claim_token", "verification_claimed_at", "updated_at"))

    try:
        request_result = provider.create_payment_request(transaction=transaction_obj, callback_url=callback_url)
    except PaymentProviderError as error:
        _mark_legacy_review(
            transaction_id=transaction_obj.pk,
            claim_token=claim_token,
            reason=ManualReviewReason.PROVIDER_STATE_UNCLEAR,
            message=str(error),
        )
        raise PaymentError(str(error)) from error

    with transaction.atomic():
        _, _, order = _lock_legacy_order_graph(identity)
        transaction_obj = PaymentTransaction.objects.select_for_update().get(pk=transaction_obj.pk)
        if transaction_obj.verification_claim_token != claim_token:
            raise PaymentError("نتیجه درخواست پرداخت منقضی یا جایگزین شده است.")
        request_payload = dict(request_result.payload)
        if success_redirect_url:
            request_payload["success_redirect_url"] = success_redirect_url
        transaction_obj.gateway_authority = request_result.authority
        transaction_obj.gateway_payment_url = request_result.payment_url
        transaction_obj.request_payload = request_payload
        transaction_obj.status = PaymentTransactionStatus.PENDING
        transaction_obj.verification_claim_token = None
        transaction_obj.verification_claimed_at = None
        transaction_obj.save(update_fields=(
            "gateway_authority", "gateway_payment_url", "request_payload", "status",
            "verification_claim_token", "verification_claimed_at", "updated_at",
        ))
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
    if transaction_obj.status in (PaymentTransactionStatus.CREATED, PaymentTransactionStatus.PENDING):
        transaction_obj.status = PaymentTransactionStatus.CALLBACK_RECEIVED
        transaction_obj.save(update_fields=["callback_payload", "status", "updated_at"])
    else:
        transaction_obj.save(update_fields=["callback_payload", "updated_at"])
    return transaction_obj


def verify_payment(*, transaction_id: int, user) -> PaymentTransaction:
    identity = PaymentTransaction.objects.filter(pk=transaction_id, user=user).values(
        "order_id", "order__checkout_id", "order__checkout__cart_id"
    ).first()
    if identity is None:
        raise PaymentError("تراکنش پرداخت یافت نشد.")
    order_identity = {
        "id": identity["order_id"],
        "checkout_id": identity["order__checkout_id"],
        "checkout__cart_id": identity["order__checkout__cart_id"],
    }
    claim_token = uuid4()
    with transaction.atomic():
        _, _, order = _lock_legacy_order_graph(order_identity)
        transaction_obj = PaymentTransaction.objects.select_for_update().get(pk=transaction_id, user=user)
        if transaction_obj.status in (
            PaymentTransactionStatus.PAID,
            PaymentTransactionStatus.FAILED,
            PaymentTransactionStatus.REQUIRES_MANUAL_REVIEW,
        ):
            return transaction_obj
        if transaction_obj.verification_claim_token is not None:
            raise PaymentError("تأیید پرداخت در حال پردازش است.")
        transaction_obj.status = PaymentTransactionStatus.VERIFYING
        transaction_obj.verification_claim_token = claim_token
        transaction_obj.verification_claimed_at = timezone.now()
        transaction_obj.save(update_fields=(
            "status", "verification_claim_token", "verification_claimed_at", "updated_at"
        ))

    try:
        provider = get_payment_provider(provider=transaction_obj.provider)
        verify_result = provider.verify(transaction=transaction_obj)
    except PaymentProviderError as error:
        _mark_legacy_review(
            transaction_id=transaction_id,
            claim_token=claim_token,
            reason=ManualReviewReason.PROVIDER_STATE_UNCLEAR,
            message=str(error),
        )
        raise PaymentError(str(error)) from error

    with transaction.atomic():
        _, _, order = _lock_legacy_order_graph(order_identity)
        transaction_obj = PaymentTransaction.objects.select_for_update().get(pk=transaction_id, user=user)
        if transaction_obj.verification_claim_token != claim_token:
            if transaction_obj.status in (
                PaymentTransactionStatus.PAID,
                PaymentTransactionStatus.FAILED,
                PaymentTransactionStatus.REQUIRES_MANUAL_REVIEW,
            ):
                return transaction_obj
            raise PaymentError("نتیجه تأیید پرداخت منقضی یا جایگزین شده است.")

        now = timezone.now()
        transaction_obj.verify_payload = verify_result.payload
        transaction_obj.verified_at = now
        transaction_obj.error_code = verify_result.error_code
        transaction_obj.error_message = verify_result.error_message
        transaction_obj.verification_claim_token = None
        transaction_obj.verification_claimed_at = None
        if verify_result.is_paid:
            try:
                ensure_order_stock_available(order=order, lock=True)
                ensure_order_shipping_ready(order=order, lock=True)
                ensure_order_delivery_slot_available(order=order, lock=True)
                ensure_order_discount_available(order=order, lock=True)
                commit_order_delivery_slot(order=order)
                commit_order_stock(order=order)
                commit_order_discount_usage(order=order)
            except (
                StockUnavailableError,
                ShippingUnavailableError,
                DeliverySlotUnavailableError,
                DiscountUnavailableError,
            ) as error:
                transaction.set_rollback(True)
                local_error = error
            else:
                transaction_obj.status = PaymentTransactionStatus.PAID
                transaction_obj.gateway_ref_id = verify_result.ref_id
                transaction_obj.gateway_trace_no = verify_result.trace_no
                transaction_obj.paid_at = now
                transaction_obj.save(update_fields=(
                    "verify_payload", "verified_at", "error_code", "error_message", "status",
                    "gateway_ref_id", "gateway_trace_no", "paid_at", "verification_claim_token",
                    "verification_claimed_at", "updated_at",
                ))
                order.payment_status = OrderStatus.PAID.value
                order.save(update_fields=("payment_status", "updated_at"))
                return transaction_obj
        else:
            transaction_obj.status = PaymentTransactionStatus.FAILED
            transaction_obj.save(update_fields=(
                "verify_payload", "verified_at", "error_code", "error_message", "status",
                "verification_claim_token", "verification_claimed_at", "updated_at",
            ))
            if order.payment_status != OrderStatus.PAID.value:
                order.payment_status = OrderStatus.FAIDED.value
                order.save(update_fields=("payment_status", "updated_at"))
            return transaction_obj

    return _mark_legacy_review(
        transaction_id=transaction_id,
        claim_token=claim_token,
        reason=ManualReviewReason.FINALIZATION_ERROR,
        message=str(local_error),
        paid_evidence=True,
        verify_result=verify_result,
    )
