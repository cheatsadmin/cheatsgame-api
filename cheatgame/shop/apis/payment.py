from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.conf import settings
from django.http import HttpResponseRedirect
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from cheatgame.api.mixins import ApiAuthMixin
from cheatgame.product.permissions import CustomerPermission
from cheatgame.shop.models import PaymentTransaction
from cheatgame.shop.payments.providers import PaymentProviderError, get_payment_provider
from cheatgame.shop.payments.services import (
    PaymentError,
    create_payment_request,
    get_user_payment_transaction,
    record_payment_callback,
    serialize_payment_transaction,
    verify_payment,
)


def wants_json_response(request) -> bool:
    accept_header = request.headers.get("Accept", "")
    return request.query_params.get("response") == "json" or "application/json" in accept_header


def is_same_origin_url(url: str, origin: str) -> bool:
    if not origin:
        return True

    split_url = urlsplit(url)
    split_origin = urlsplit(origin)
    return (
        split_url.scheme in ("http", "https")
        and split_url.scheme == split_origin.scheme
        and split_url.netloc == split_origin.netloc
    )


def build_payment_success_redirect_url(transaction_obj: PaymentTransaction) -> str:
    redirect_url = (
        transaction_obj.request_payload.get("success_redirect_url")
        or settings.PAYMENT_SUCCESS_REDIRECT_URL
    )
    if not redirect_url:
        raise PaymentError("آدرس بازگشت موفق پرداخت تنظیم نشده است.")

    split_url = urlsplit(redirect_url)
    query = dict(parse_qsl(split_url.query, keep_blank_values=True))
    query.update(
        {
            "transaction_id": transaction_obj.id,
            "order_id": transaction_obj.order_id,
        }
    )
    return urlunsplit(
        (
            split_url.scheme,
            split_url.netloc,
            split_url.path,
            urlencode(query),
            split_url.fragment,
        )
    )


class PaymentTransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = PaymentTransaction
        fields = (
            "id",
            "order",
            "provider",
            "amount",
            "status",
            "gateway_authority",
            "gateway_ref_id",
            "gateway_trace_no",
            "gateway_payment_url",
            "error_code",
            "error_message",
            "paid_at",
            "verified_at",
            "created_at",
            "updated_at",
        )


class CreatePaymentRequestApi(ApiAuthMixin, APIView):
    permission_classes = (CustomerPermission,)
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = "payment_write"

    class CreatePaymentRequestInPutSerializer(serializers.Serializer):
        success_redirect_url = serializers.URLField(required=False)

    @extend_schema(request=CreatePaymentRequestInPutSerializer, responses=PaymentTransactionSerializer)
    def post(self, request, order_id: int):
        serializer = self.CreatePaymentRequestInPutSerializer(data=request.data or {})
        serializer.is_valid(raise_exception=True)
        success_redirect_url = serializer.validated_data.get("success_redirect_url", "")
        if success_redirect_url and not is_same_origin_url(
            success_redirect_url,
            request.headers.get("Origin", ""),
        ):
            return Response(
                {"error": "آدرس بازگشت پرداخت معتبر نیست."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            provider = get_payment_provider()
        except PaymentProviderError as error:
            return Response({"error": str(error)}, status=status.HTTP_400_BAD_REQUEST)

        callback_url = request.build_absolute_uri(f"/api/payment/callback/{provider.name}/")
        try:
            transaction_obj = create_payment_request(
                order_id=order_id,
                user=request.user,
                callback_url=callback_url,
                success_redirect_url=success_redirect_url,
            )
        except PaymentError as error:
            return Response({"error": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(PaymentTransactionSerializer(transaction_obj).data, status=status.HTTP_200_OK)


class PaymentCallbackApi(APIView):
    provider_name = ""
    authentication_classes = ()
    permission_classes = ()

    class PaymentCallbackOutPutSerializer(serializers.Serializer):
        transaction_id = serializers.IntegerField()
        status = serializers.CharField()

    @extend_schema(responses=PaymentCallbackOutPutSerializer)
    def get(self, request):
        try:
            provider = get_payment_provider(provider=self.provider_name)
        except PaymentProviderError as error:
            return Response({"error": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        try:
            transaction_obj = record_payment_callback(
                provider=provider,
                query_params=request.query_params,
            )
        except PaymentError as error:
            return Response({"error": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        if not wants_json_response(request):
            try:
                redirect_url = build_payment_success_redirect_url(transaction_obj)
            except PaymentError as error:
                return Response({"error": str(error)}, status=status.HTTP_400_BAD_REQUEST)
            return HttpResponseRedirect(redirect_url)
        return Response(
            {
                "transaction_id": transaction_obj.id,
                "order_id": transaction_obj.order_id,
                "status": transaction_obj.status,
            },
            status=status.HTTP_200_OK,
        )


class FakePaymentCallbackApi(PaymentCallbackApi):
    provider_name = "fake"


class ZarinpalPaymentCallbackApi(PaymentCallbackApi):
    provider_name = "zarinpal"


class VerifyPaymentApi(ApiAuthMixin, APIView):
    permission_classes = (CustomerPermission,)
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = "payment_write"

    @extend_schema(request=None, responses=PaymentTransactionSerializer)
    def post(self, request, transaction_id: int):
        try:
            transaction_obj = verify_payment(transaction_id=transaction_id, user=request.user)
        except PaymentError as error:
            return Response({"error": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(PaymentTransactionSerializer(transaction_obj).data, status=status.HTTP_200_OK)


class PaymentTransactionDetailApi(ApiAuthMixin, APIView):
    permission_classes = (CustomerPermission,)

    @extend_schema(responses=PaymentTransactionSerializer)
    def get(self, request, transaction_id: int):
        try:
            transaction_obj = get_user_payment_transaction(transaction_id=transaction_id, user=request.user)
        except PaymentError as error:
            return Response({"error": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(serialize_payment_transaction(transaction_obj), status=status.HTTP_200_OK)
