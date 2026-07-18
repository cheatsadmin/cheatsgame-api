from django.core.exceptions import PermissionDenied
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.exceptions import (
    AuthenticationFailed,
    MethodNotAllowed,
    NotAuthenticated,
    PermissionDenied as DRFPermissionDenied,
)
from rest_framework.permissions import BasePermission
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from cheatgame.api.mixins import ApiAuthMixin
from cheatgame.digital_products.customer_checkout import (
    DigitalCheckoutProjectionIntegrityError,
    digital_checkout_projection,
)
from cheatgame.digital_products.customer_checkout_selectors import (
    active_customer_digital_checkout,
    owned_customer_digital_checkout,
)
from cheatgame.digital_products.customer_checkout_serializers import (
    DigitalCheckoutOutputSerializer,
    EmptyDigitalCheckoutInputSerializer,
    PrepareDigitalCheckoutInputSerializer,
)
from cheatgame.digital_products.public_catalog_apis import DigitalApiErrorSerializer, digital_api_error
from cheatgame.digital_products.services import (
    DigitalCartLockedError,
    DigitalCartStaleError,
    DigitalCheckoutExpiredError,
    DigitalCheckoutIdempotencyError,
    DigitalCheckoutIntegrityError,
    DigitalOfferUnavailableError,
    DigitalProductsConflictError,
    DigitalProductsValidationError,
    EmptyDigitalCartError,
    InsufficientDigitalAvailabilityError,
    MixedCommerceAuthorityError,
    StandardCartNotSupportedError,
)
from cheatgame.digital_products.services.checkout_preparation import (
    expire_owned_digital_checkout_if_due,
    prepare_digital_checkout,
)
from cheatgame.shop.models import CheckoutStatus
from cheatgame.shop.services.checkout import CheckoutServiceError, cancel_checkout
from cheatgame.users.models import UserTypes


class ActiveVerifiedCheckoutCustomerPermission(BasePermission):
    def has_permission(self, request, view):
        user = request.user
        return bool(
            user
            and user.is_authenticated
            and user.is_active
            and user.user_type == UserTypes.CUSTOMER
            and user.phone_verified
        )


def _checkout_error(*, code, detail, http_status, fields=None):
    return digital_api_error(code=code, detail=detail, fields=fields, http_status=http_status)


def _validated(serializer_class, data):
    serializer = serializer_class(data=data)
    if serializer.is_valid():
        return serializer.validated_data, None
    return None, _checkout_error(
        code="invalid_request",
        detail="The Digital Checkout request is invalid.",
        fields=serializer.errors,
        http_status=status.HTTP_400_BAD_REQUEST,
    )


def _domain_error(exc):
    if isinstance(exc, DigitalCheckoutExpiredError):
        return _checkout_error(
            code="digital_checkout_expired",
            detail="The Digital Checkout has expired.",
            http_status=status.HTTP_409_CONFLICT,
        )
    if isinstance(exc, DigitalCheckoutIdempotencyError):
        return _checkout_error(
            code="digital_checkout_idempotency_conflict",
            detail="This checkout UUID was already used for different commercial terms.",
            http_status=status.HTTP_409_CONFLICT,
        )
    if isinstance(exc, DigitalCartLockedError):
        return _checkout_error(
            code="digital_cart_locked",
            detail="The Cart is locked by an active Checkout.",
            http_status=status.HTTP_409_CONFLICT,
        )
    if isinstance(exc, MixedCommerceAuthorityError):
        return _checkout_error(
            code="mixed_commerce_authority",
            detail="Standard and Digital items must be purchased separately.",
            http_status=status.HTTP_409_CONFLICT,
        )
    if isinstance(exc, StandardCartNotSupportedError):
        return _checkout_error(
            code="standard_cart_requires_standard_checkout",
            detail="This Cart must use the Standard Checkout flow.",
            http_status=status.HTTP_409_CONFLICT,
        )
    if isinstance(exc, DigitalCartStaleError):
        return _checkout_error(
            code="digital_cart_stale",
            detail="The Digital Cart changed and must be reviewed again.",
            http_status=status.HTTP_409_CONFLICT,
        )
    if isinstance(exc, (DigitalOfferUnavailableError, InsufficientDigitalAvailabilityError)):
        return _checkout_error(
            code="digital_availability_unavailable",
            detail="One or more Digital selections are no longer available.",
            http_status=status.HTTP_409_CONFLICT,
        )
    if isinstance(exc, (DigitalCheckoutIntegrityError, DigitalCheckoutProjectionIntegrityError)):
        return _checkout_error(
            code="digital_checkout_integrity_conflict",
            detail="The Digital Checkout could not be represented safely.",
            http_status=status.HTTP_409_CONFLICT,
        )
    if isinstance(exc, EmptyDigitalCartError):
        return _checkout_error(
            code="digital_cart_empty",
            detail="The Digital Cart is empty.",
            http_status=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    if isinstance(exc, (DigitalProductsConflictError, CheckoutServiceError)):
        return _checkout_error(
            code="digital_checkout_not_ready",
            detail="The Digital Checkout is not ready for this operation.",
            http_status=status.HTTP_409_CONFLICT,
        )
    if isinstance(exc, DigitalProductsValidationError):
        return _checkout_error(
            code="invalid_request",
            detail="The Digital Checkout request is invalid.",
            http_status=status.HTTP_400_BAD_REQUEST,
        )
    if isinstance(exc, PermissionDenied):
        return _checkout_error(
            code="digital_checkout_permission_denied",
            detail="This customer action is not permitted.",
            http_status=status.HTTP_403_FORBIDDEN,
        )
    return _checkout_error(
        code="digital_checkout_integrity_conflict",
        detail="The Digital Checkout operation failed safely.",
        http_status=status.HTTP_409_CONFLICT,
    )


class CustomerDigitalCheckoutApi(ApiAuthMixin, APIView):
    permission_classes = (ActiveVerifiedCheckoutCustomerPermission,)
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = "checkout_write"

    def handle_exception(self, exc):
        if isinstance(exc, (NotAuthenticated, AuthenticationFailed)):
            return _checkout_error(
                code="authentication_required",
                detail="Authentication is required.",
                http_status=status.HTTP_401_UNAUTHORIZED,
            )
        if isinstance(exc, (PermissionDenied, DRFPermissionDenied)):
            return _checkout_error(
                code="digital_checkout_permission_denied",
                detail="This customer action is not permitted.",
                http_status=status.HTTP_403_FORBIDDEN,
            )
        if isinstance(exc, MethodNotAllowed):
            return _checkout_error(
                code="method_not_allowed",
                detail="This method is not supported by the Digital Checkout endpoint.",
                http_status=status.HTTP_405_METHOD_NOT_ALLOWED,
            )
        return super().handle_exception(exc)


class CustomerDigitalCheckoutPrepareApi(CustomerDigitalCheckoutApi):
    http_method_names = ("post", "options")

    @extend_schema(
        operation_id="digital_customer_checkout_prepare",
        request=PrepareDigitalCheckoutInputSerializer,
        responses={
            200: DigitalCheckoutOutputSerializer,
            201: DigitalCheckoutOutputSerializer,
            400: DigitalApiErrorSerializer,
            401: DigitalApiErrorSerializer,
            403: DigitalApiErrorSerializer,
            409: DigitalApiErrorSerializer,
            422: DigitalApiErrorSerializer,
            405: DigitalApiErrorSerializer,
        },
        description="Prepare or coherently reuse one immutable reservation-backed Digital Checkout.",
    )
    def post(self, request):
        values, error = _validated(PrepareDigitalCheckoutInputSerializer, request.data)
        if error:
            return error
        try:
            checkout, created = prepare_digital_checkout(
                actor=request.user,
                client_checkout_uuid=values["checkout_uuid"],
            )
            if checkout.status == CheckoutStatus.EXPIRED:
                return _domain_error(DigitalCheckoutExpiredError("Checkout has expired."))
            checkout = owned_customer_digital_checkout(user=request.user, public_id=checkout.public_id)
            return Response(
                digital_checkout_projection(checkout),
                status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
            )
        except (
            DigitalProductsValidationError,
            DigitalProductsConflictError,
            DigitalCheckoutProjectionIntegrityError,
            PermissionDenied,
        ) as exc:
            return _domain_error(exc)


class CustomerDigitalCheckoutActiveApi(CustomerDigitalCheckoutApi):
    http_method_names = ("get", "head", "options")
    throttle_classes = ()

    @extend_schema(
        operation_id="digital_customer_checkout_active",
        responses={
            200: DigitalCheckoutOutputSerializer,
            401: DigitalApiErrorSerializer,
            403: DigitalApiErrorSerializer,
            404: DigitalApiErrorSerializer,
            409: DigitalApiErrorSerializer,
            405: DigitalApiErrorSerializer,
        },
        description="Read the authenticated customer's active Digital Checkout without renewing its lease.",
    )
    def get(self, request):
        try:
            checkout = active_customer_digital_checkout(user=request.user)
            if checkout is None:
                return _checkout_error(
                    code="digital_checkout_not_found",
                    detail="No active Digital Checkout was found.",
                    http_status=status.HTTP_404_NOT_FOUND,
                )
            if checkout.expires_at <= timezone.now():
                expire_owned_digital_checkout_if_due(actor=request.user, checkout_id=checkout.pk)
                checkout = active_customer_digital_checkout(user=request.user)
                if checkout is None:
                    return _checkout_error(
                        code="digital_checkout_not_found",
                        detail="No active Digital Checkout was found.",
                        http_status=status.HTTP_404_NOT_FOUND,
                    )
            return Response(digital_checkout_projection(checkout))
        except (
            DigitalProductsValidationError,
            DigitalProductsConflictError,
            DigitalCheckoutProjectionIntegrityError,
            PermissionDenied,
        ) as exc:
            return _domain_error(exc)


class CustomerDigitalCheckoutDetailApi(CustomerDigitalCheckoutApi):
    http_method_names = ("get", "head", "options")
    throttle_classes = ()

    @extend_schema(
        operation_id="digital_customer_checkout_detail",
        responses={
            200: DigitalCheckoutOutputSerializer,
            401: DigitalApiErrorSerializer,
            403: DigitalApiErrorSerializer,
            404: DigitalApiErrorSerializer,
            409: DigitalApiErrorSerializer,
            405: DigitalApiErrorSerializer,
        },
        description="Read one owned immutable Digital Checkout by public UUID.",
    )
    def get(self, request, checkout_id):
        checkout = owned_customer_digital_checkout(user=request.user, public_id=checkout_id)
        if checkout is None:
            return _checkout_error(
                code="digital_checkout_not_found",
                detail="Digital Checkout was not found.",
                http_status=status.HTTP_404_NOT_FOUND,
            )
        try:
            if checkout.expires_at <= timezone.now():
                expire_owned_digital_checkout_if_due(actor=request.user, checkout_id=checkout.pk)
                checkout = owned_customer_digital_checkout(user=request.user, public_id=checkout_id)
            return Response(digital_checkout_projection(checkout))
        except (
            DigitalProductsValidationError,
            DigitalProductsConflictError,
            DigitalCheckoutProjectionIntegrityError,
            PermissionDenied,
        ) as exc:
            return _domain_error(exc)


class CustomerDigitalCheckoutCancelApi(CustomerDigitalCheckoutApi):
    http_method_names = ("post", "options")

    @extend_schema(
        operation_id="digital_customer_checkout_cancel",
        request=EmptyDigitalCheckoutInputSerializer,
        responses={
            200: DigitalCheckoutOutputSerializer,
            400: DigitalApiErrorSerializer,
            401: DigitalApiErrorSerializer,
            403: DigitalApiErrorSerializer,
            404: DigitalApiErrorSerializer,
            409: DigitalApiErrorSerializer,
            405: DigitalApiErrorSerializer,
        },
        description="Cancel one owned pre-payment Digital Checkout and release its temporary reservations.",
    )
    def post(self, request, checkout_id):
        _values, error = _validated(EmptyDigitalCheckoutInputSerializer, request.data)
        if error:
            return error
        checkout = owned_customer_digital_checkout(user=request.user, public_id=checkout_id)
        if checkout is None:
            return _checkout_error(
                code="digital_checkout_not_found",
                detail="Digital Checkout was not found.",
                http_status=status.HTTP_404_NOT_FOUND,
            )
        try:
            if checkout.expires_at <= timezone.now():
                expire_owned_digital_checkout_if_due(actor=request.user, checkout_id=checkout.pk)
                checkout.refresh_from_db()
                if checkout.status == CheckoutStatus.EXPIRED:
                    raise DigitalCheckoutExpiredError("Checkout has expired.")
            if checkout.status not in (CheckoutStatus.CHECKOUT_DRAFT, CheckoutStatus.CANCELED):
                raise DigitalProductsConflictError("Only a pre-payment Digital Checkout can be canceled.")
            cancel_checkout(user=request.user, public_id=checkout_id)
            checkout = owned_customer_digital_checkout(user=request.user, public_id=checkout_id)
            return Response(digital_checkout_projection(checkout))
        except (
            DigitalProductsValidationError,
            DigitalProductsConflictError,
            CheckoutServiceError,
            PermissionDenied,
        ) as exc:
            return _domain_error(exc)
