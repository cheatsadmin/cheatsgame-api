from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from cheatgame.api.mixins import ApiAuthMixin
from cheatgame.shop.models import Checkout
from cheatgame.shop.services.checkout import (
    CheckoutServiceError,
    cancel_checkout,
    checkout_totals,
    create_or_reuse_checkout,
    get_active_checkout,
    get_owned_checkout,
    select_checkout_address,
    select_checkout_schedule,
    select_checkout_shipping,
)


ERROR_STATUS = {
    "IDEMPOTENCY_CONFLICT": status.HTTP_409_CONFLICT,
    "CART_LOCKED": status.HTTP_409_CONFLICT,
    "CART_EMPTY": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "CART_INVALID": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "CHECKOUT_NOT_FOUND": status.HTTP_404_NOT_FOUND,
    "ADDRESS_NOT_FOUND": status.HTTP_404_NOT_FOUND,
    "CHECKOUT_NOT_EDITABLE": status.HTTP_409_CONFLICT,
    "CHECKOUT_NOT_CANCELABLE": status.HTTP_409_CONFLICT,
    "ADDRESS_REQUIRED": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "SHIPPING_METHOD_REQUIRED": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "SHIPPING_METHOD_INVALID": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "SCHEDULE_INVALID": status.HTTP_422_UNPROCESSABLE_ENTITY,
}


def _disabled_response():
    return Response(
        {"code": "CHECKOUT_V2_DISABLED", "message": "نسخه جدید فرایند خرید فعال نیست."},
        status=status.HTTP_503_SERVICE_UNAVAILABLE,
    )


def _error_response(error):
    payload = {"code": error.code, "message": error.message}
    if error.details:
        payload["details"] = error.details
    return Response(payload, status=ERROR_STATUS.get(error.code, status.HTTP_400_BAD_REQUEST))


def _money(value):
    return str(value.quantize(1))


def serialize_checkout(checkout):
    checkout = (
        Checkout.objects.filter(pk=checkout.pk)
        .prefetch_related("lines__attachments", "orders", "payment_transactions")
        .select_related("shipping_snapshot")
        .get()
    )
    lines = []
    for line in checkout.lines.all().order_by("id"):
        lines.append(
            {
                "product_id": line.product_id,
                "product_name": line.product_name,
                "product_sku": line.product_sku,
                "product_type": line.product_type,
                "variation_id": line.variation_id,
                "variation_name": line.variation_name,
                "unit_original_price": _money(line.unit_original_price),
                "unit_payable_price": _money(line.unit_payable_price),
                "quantity": line.quantity,
                "line_original_total": _money(line.line_original_total),
                "line_payable_total": _money(line.line_payable_total),
                "attachments": [
                    {
                        "attachment_id": attachment.attachment_id,
                        "attachment_type": attachment.attachment_type,
                        "name": attachment.name,
                        "unit_price": _money(attachment.unit_price),
                        "quantity_basis": attachment.quantity_basis,
                        "total_price": _money(attachment.total_price),
                    }
                    for attachment in line.attachments.all().order_by("id")
                ],
                "display": line.snapshot,
            }
        )

    shipping = None
    try:
        snapshot = checkout.shipping_snapshot
    except ObjectDoesNotExist:
        snapshot = None
    if snapshot is not None:
        shipping = {
            "address_id": snapshot.address_id,
            "recipient_name": snapshot.recipient_name,
            "recipient_phone": snapshot.recipient_phone,
            "province": snapshot.province,
            "city": snapshot.city,
            "full_address": snapshot.full_address,
            "postal_code": snapshot.postal_code,
            "delivery_method_id": snapshot.delivery_method_id,
            "delivery_method_name": snapshot.delivery_method_name,
            "delivery_cost": _money(snapshot.delivery_cost),
            "is_pricing_finalized": snapshot.is_pricing_finalized,
            "schedule_id": snapshot.schedule_id,
            "schedule_start": snapshot.schedule_start,
            "schedule_end": snapshot.schedule_end,
        }
    latest_payment = checkout.payment_transactions.order_by("-created_at").first()
    payment = None
    if latest_payment is not None:
        payment = {
            "status": latest_payment.status,
            "amount": _money(latest_payment.amount),
            "created_at": latest_payment.created_at,
        }
    totals = checkout_totals(checkout)
    order_codes = list(checkout.orders.values_list("public_tracking_code", flat=True))
    return {
        "public_id": str(checkout.public_id),
        "status": checkout.status,
        "expires_at": checkout.expires_at,
        "maximum_expires_at": checkout.maximum_expires_at,
        "version": checkout.version,
        "tracking_codes": order_codes,
        "lines": lines,
        "shipping": shipping,
        "totals": {key: _money(value) if key != "is_pricing_finalized" else value for key, value in totals.items()},
        "payment_eligible": False,
        "payment_ineligible_reason": "SHIPPING_PRICING_NOT_FINALIZED",
        "latest_payment": payment,
        "resume_route": f"/checkout/{checkout.public_id}",
        "can_cancel": checkout.status in ("checkout_draft", "pending_payment")
        and not checkout.payment_transactions.filter(status__in=("pending", "callback_received", "verifying", "paid", "requires_manual_review")).exists(),
        "can_retry_payment": checkout.status == "pending_payment"
        and not checkout.payment_transactions.filter(status__in=("pending", "callback_received", "verifying", "paid", "requires_manual_review")).exists(),
    }


class CheckoutCreateApi(ApiAuthMixin, APIView):
    class InputSerializer(serializers.Serializer):
        checkout_uuid = serializers.UUIDField()

    def post(self, request):
        if not settings.COMMERCE_CHECKOUT_V2_ENABLED:
            return _disabled_response()
        serializer = self.InputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            result = create_or_reuse_checkout(
                user=request.user,
                client_checkout_uuid=serializer.validated_data["checkout_uuid"],
                request_context={
                    "request_id": request.headers.get("X-Request-ID"),
                    "correlation_id": request.headers.get("X-Correlation-ID"),
                    "client_ip": request.META.get("REMOTE_ADDR"),
                    "user_agent": request.headers.get("User-Agent"),
                },
            )
        except CheckoutServiceError as error:
            return _error_response(error)
        return Response(serialize_checkout(result.checkout), status=status.HTTP_201_CREATED if result.created else status.HTTP_200_OK)


class ActiveCheckoutApi(ApiAuthMixin, APIView):
    def get(self, request):
        if not settings.COMMERCE_CHECKOUT_V2_ENABLED:
            return _disabled_response()
        checkout = get_active_checkout(user=request.user)
        if checkout is None:
            return Response({"code": "NO_ACTIVE_CHECKOUT", "message": "فرایند خرید فعالی وجود ندارد."}, status=404)
        return Response(serialize_checkout(checkout))


class CheckoutDetailApi(ApiAuthMixin, APIView):
    def get(self, request, public_id):
        if not settings.COMMERCE_CHECKOUT_V2_ENABLED:
            return _disabled_response()
        try:
            checkout = get_owned_checkout(user=request.user, public_id=public_id)
        except CheckoutServiceError as error:
            return _error_response(error)
        return Response(serialize_checkout(checkout))


class CheckoutAddressApi(ApiAuthMixin, APIView):
    class InputSerializer(serializers.Serializer):
        address_id = serializers.IntegerField(min_value=1)

    def patch(self, request, public_id):
        return _selection_response(request, public_id, self.InputSerializer, select_checkout_address, "address_id")


class CheckoutShippingApi(ApiAuthMixin, APIView):
    class InputSerializer(serializers.Serializer):
        delivery_method_id = serializers.IntegerField(min_value=1)

    def patch(self, request, public_id):
        return _selection_response(request, public_id, self.InputSerializer, select_checkout_shipping, "delivery_method_id")


class CheckoutScheduleApi(ApiAuthMixin, APIView):
    class InputSerializer(serializers.Serializer):
        schedule_id = serializers.IntegerField(min_value=1)

    def patch(self, request, public_id):
        return _selection_response(request, public_id, self.InputSerializer, select_checkout_schedule, "schedule_id")


def _selection_response(request, public_id, serializer_class, service, field):
    if not settings.COMMERCE_CHECKOUT_V2_ENABLED:
        return _disabled_response()
    serializer = serializer_class(data=request.data)
    serializer.is_valid(raise_exception=True)
    try:
        checkout, _ = service(user=request.user, public_id=public_id, **{field: serializer.validated_data[field]})
    except CheckoutServiceError as error:
        return _error_response(error)
    return Response(serialize_checkout(checkout))


class CheckoutCancelApi(ApiAuthMixin, APIView):
    def post(self, request, public_id):
        if not settings.COMMERCE_CHECKOUT_V2_ENABLED:
            return _disabled_response()
        try:
            checkout = cancel_checkout(user=request.user, public_id=public_id)
        except CheckoutServiceError as error:
            return _error_response(error)
        return Response(serialize_checkout(checkout))
