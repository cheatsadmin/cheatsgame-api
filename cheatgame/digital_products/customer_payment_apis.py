from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils.decorators import method_decorator
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.exceptions import AuthenticationFailed, MethodNotAllowed, NotAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from cheatgame.api.mixins import ApiAuthMixin
from cheatgame.digital_products.customer_checkout_apis import ActiveVerifiedCheckoutCustomerPermission
from cheatgame.digital_products.customer_payment import digital_payment_projection
from cheatgame.digital_products.customer_payment_selectors import owned_customer_digital_payment_checkout
from cheatgame.digital_products.customer_payment_serializers import (
    DigitalPaymentOutputSerializer,
    DigitalPaymentRequestInputSerializer,
)
from cheatgame.digital_products.public_catalog_apis import DigitalApiErrorSerializer, digital_api_error
from cheatgame.digital_products.services.payment_adapter import (
    DigitalPaymentAdapterError,
    DigitalPaymentNotFound,
    DigitalPaymentNotReady,
    DigitalPaymentProviderUnavailable,
    DigitalPaymentRequestConflict,
    DigitalPaymentRequestInProgress,
    request_digital_checkout_payment,
)
from cheatgame.financial_core.services.idempotency import IdempotencyConflict
from cheatgame.financial_core.services.adapters import PRODUCTION_ADAPTER_REGISTRY
from cheatgame.financial_core.services.placement import PlacementNotEligible
from cheatgame.financial_core.services.provider_requests import (
    CollectionBlocked,
    RequestClaimConflict,
    StaleRequestClaim,
)


def _error(*, code, detail, http_status, fields=None):
    return digital_api_error(code=code, detail=detail, fields=fields, http_status=http_status)


def _domain_error(exc):
    if isinstance(exc, DigitalPaymentNotFound):
        return _error(code="digital_checkout_not_found", detail="Digital Checkout was not found.", http_status=404)
    if isinstance(exc, DigitalPaymentProviderUnavailable):
        return _error(
            code="payment_provider_unavailable",
            detail="The selected payment provider is unavailable.",
            http_status=503,
        )
    if isinstance(exc, DigitalPaymentRequestInProgress):
        return _error(
            code="payment_request_in_progress",
            detail="The payment request is still being processed.",
            http_status=409,
        )
    if isinstance(exc, (DigitalPaymentRequestConflict, IdempotencyConflict)):
        return _error(
            code="payment_request_conflict",
            detail="The payment request conflicts with existing payment state.",
            http_status=409,
        )
    if isinstance(exc, (DigitalPaymentNotReady, PlacementNotEligible, CollectionBlocked, RequestClaimConflict)):
        return _error(
            code="digital_checkout_not_payment_ready",
            detail="The Digital Checkout is not ready for a new payment request.",
            http_status=409,
        )
    if isinstance(exc, (DigitalPaymentAdapterError, ValidationError, StaleRequestClaim)):
        return _error(code="invalid_payment_request", detail="The payment request is invalid.", http_status=400)
    if isinstance(exc, PermissionDenied):
        return _error(code="payment_permission_denied", detail="This customer action is not permitted.", http_status=403)
    return _error(
        code="payment_request_failed_safely",
        detail="The payment request could not be completed safely.",
        http_status=409,
    )


class CustomerDigitalPaymentApi(ApiAuthMixin, APIView):
    permission_classes = (ActiveVerifiedCheckoutCustomerPermission,)

    def handle_exception(self, exc):
        if isinstance(exc, (NotAuthenticated, AuthenticationFailed)):
            return _error(code="authentication_required", detail="Authentication is required.", http_status=401)
        if isinstance(exc, PermissionDenied):
            return _error(code="payment_permission_denied", detail="This customer action is not permitted.", http_status=403)
        if isinstance(exc, MethodNotAllowed):
            return _error(code="method_not_allowed", detail="This payment method is not supported.", http_status=405)
        return super().handle_exception(exc)


@method_decorator(transaction.non_atomic_requests, name="dispatch")
class CustomerDigitalPaymentRequestApi(CustomerDigitalPaymentApi):
    http_method_names = ("post", "options")
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = "payment_write"
    adapter_registry = PRODUCTION_ADAPTER_REGISTRY

    @extend_schema(
        operation_id="digital_customer_payment_request",
        parameters=[
            OpenApiParameter(
                name="Idempotency-Key",
                type=str,
                location=OpenApiParameter.HEADER,
                required=True,
                description="Stable UUID for one customer payment-request intent.",
            )
        ],
        request=DigitalPaymentRequestInputSerializer,
        responses={
            200: DigitalPaymentOutputSerializer,
            201: DigitalPaymentOutputSerializer,
            400: DigitalApiErrorSerializer,
            401: DigitalApiErrorSerializer,
            403: DigitalApiErrorSerializer,
            404: DigitalApiErrorSerializer,
            409: DigitalApiErrorSerializer,
            503: DigitalApiErrorSerializer,
        },
        description="Place a READY Digital Checkout and request one Financial Core provider operation.",
    )
    def post(self, request, checkout_id):
        serializer = DigitalPaymentRequestInputSerializer(data=request.data)
        if not serializer.is_valid():
            return _error(
                code="invalid_request",
                detail="The payment request is invalid.",
                fields=serializer.errors,
                http_status=400,
            )
        try:
            result = request_digital_checkout_payment(
                checkout_public_id=checkout_id,
                actor=request.user,
                provider=serializer.validated_data["provider"],
                idempotency_key=request.headers.get("Idempotency-Key", ""),
                adapter_registry=self.adapter_registry,
            )
            checkout = owned_customer_digital_payment_checkout(user=request.user, public_id=checkout_id)
            return Response(
                digital_payment_projection(
                    checkout,
                    replayed=result.replayed,
                    customer_action_url=result.customer_action_url,
                ),
                status=status.HTTP_200_OK if result.replayed else status.HTTP_201_CREATED,
            )
        except (
            DigitalPaymentAdapterError,
            PlacementNotEligible,
            CollectionBlocked,
            RequestClaimConflict,
            StaleRequestClaim,
            IdempotencyConflict,
            ValidationError,
            PermissionDenied,
        ) as exc:
            return _domain_error(exc)


class CustomerDigitalPaymentStatusApi(CustomerDigitalPaymentApi):
    http_method_names = ("get", "head", "options")
    throttle_classes = ()

    @extend_schema(
        operation_id="digital_customer_payment_status",
        responses={
            200: DigitalPaymentOutputSerializer,
            401: DigitalApiErrorSerializer,
            403: DigitalApiErrorSerializer,
            404: DigitalApiErrorSerializer,
        },
        description="Read authoritative Financial Core payment-request state without side effects.",
    )
    def get(self, request, checkout_id):
        checkout = owned_customer_digital_payment_checkout(user=request.user, public_id=checkout_id)
        if checkout is None:
            return _domain_error(DigitalPaymentNotFound("Checkout was not found."))
        return Response(digital_payment_projection(checkout))
