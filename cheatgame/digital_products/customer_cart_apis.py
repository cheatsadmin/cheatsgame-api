from django.core.exceptions import PermissionDenied
from django.db.models import ProtectedError
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
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
from cheatgame.digital_products.customer_cart import (
    DigitalCartProjectionIntegrityError,
    coherent_digital_selection,
    digital_cart_item_projection,
)
from cheatgame.digital_products.customer_cart_selectors import owned_customer_cart_item
from cheatgame.digital_products.customer_cart_serializers import (
    AddDigitalCartItemInputSerializer,
    ChangeDigitalFulfillmentMethodInputSerializer,
    DigitalCartItemOutputSerializer,
    RemoveDigitalCartItemOutputSerializer,
)
from cheatgame.digital_products.models import (
    DigitalCartFulfillmentMethod,
    DigitalOfferCapacity,
)
from cheatgame.digital_products.public_catalog_apis import (
    DigitalApiErrorSerializer,
    digital_api_error,
)
from cheatgame.digital_products.public_catalog_selectors import public_digital_offers
from cheatgame.digital_products.services import (
    DigitalCartLockedError,
    DigitalOfferUnavailableError,
    DigitalProductsConflictError,
    DigitalProductsValidationError,
    MixedCommerceAuthorityError,
)
from cheatgame.digital_products.services.cart import (
    add_digital_offer_to_cart,
    change_digital_cart_fulfillment_method,
    remove_digital_cart_item,
)
from cheatgame.shop.services.cart import get_cart_or_create
from cheatgame.users.models import UserTypes


class ActiveVerifiedCustomerPermission(BasePermission):
    def has_permission(self, request, view):
        user = request.user
        return bool(
            user
            and user.is_authenticated
            and user.is_active
            and user.user_type == UserTypes.CUSTOMER
            and user.phone_verified
        )


class CustomerDigitalCartAPIView(ApiAuthMixin, APIView):
    permission_classes = (ActiveVerifiedCustomerPermission,)
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = "checkout_write"

    def handle_exception(self, exc):
        if isinstance(exc, (NotAuthenticated, AuthenticationFailed)):
            return digital_api_error(
                code="authentication_required",
                detail="Authentication is required.",
                http_status=status.HTTP_401_UNAUTHORIZED,
            )
        if isinstance(exc, (PermissionDenied, DRFPermissionDenied)):
            return digital_api_error(
                code="digital_cart_permission_denied",
                detail="This customer action is not permitted.",
                http_status=status.HTTP_403_FORBIDDEN,
            )
        if isinstance(exc, MethodNotAllowed):
            return digital_api_error(
                code="method_not_allowed",
                detail="This method is not supported by the Digital Cart endpoint.",
                http_status=status.HTTP_405_METHOD_NOT_ALLOWED,
            )
        return super().handle_exception(exc)


def _validated_input(serializer_class, data):
    serializer = serializer_class(data=data)
    if serializer.is_valid():
        return serializer.validated_data, None
    return None, digital_api_error(
        code="invalid_request",
        detail="The Digital Cart request is invalid.",
        fields=serializer.errors,
        http_status=status.HTTP_400_BAD_REQUEST,
    )


def _owned_digital_item_or_error(*, user, cart_item_id):
    item = owned_customer_cart_item(user=user, cart_item_id=cart_item_id)
    if item is None:
        return None, digital_api_error(
            code="digital_cart_item_not_found",
            detail="Digital CartItem was not found.",
            http_status=status.HTTP_404_NOT_FOUND,
        )
    if item.commerce_authority != "digital_products":
        return None, digital_api_error(
            code="digital_route_requires_digital_item",
            detail="This route accepts only Digital CartItems.",
            http_status=status.HTTP_409_CONFLICT,
        )
    try:
        coherent_digital_selection(item)
    except DigitalCartProjectionIntegrityError:
        return None, digital_api_error(
            code="digital_cart_integrity_conflict",
            detail="The Digital Cart selection is inconsistent and cannot be changed.",
            http_status=status.HTTP_409_CONFLICT,
        )
    return item, None


def _domain_error_response(exc):
    if isinstance(exc, DigitalCartLockedError):
        return digital_api_error(
            code="digital_cart_locked",
            detail="The Cart is locked by an active purchase flow.",
            http_status=status.HTTP_409_CONFLICT,
        )
    if isinstance(exc, MixedCommerceAuthorityError):
        return digital_api_error(
            code="mixed_commerce_authority",
            detail="Standard and Digital items cannot be combined in this Cart.",
            http_status=status.HTTP_409_CONFLICT,
        )
    if isinstance(exc, DigitalOfferUnavailableError):
        return digital_api_error(
            code="digital_offer_unavailable",
            detail="The selected Digital Offer is no longer available.",
            http_status=status.HTTP_409_CONFLICT,
        )
    if isinstance(exc, DigitalProductsConflictError):
        return digital_api_error(
            code="digital_cart_selection_conflict",
            detail="This Digital selection conflicts with the current Cart.",
            http_status=status.HTTP_409_CONFLICT,
        )
    if isinstance(exc, DigitalProductsValidationError):
        return digital_api_error(
            code="digital_cart_validation_error",
            detail="The Digital Cart request is not valid.",
            http_status=status.HTTP_400_BAD_REQUEST,
        )
    if isinstance(exc, PermissionDenied):
        return digital_api_error(
            code="digital_cart_permission_denied",
            detail="This customer action is not permitted.",
            http_status=status.HTTP_403_FORBIDDEN,
        )
    return digital_api_error(
        code="digital_cart_integrity_conflict",
        detail="The Digital Cart could not be changed safely.",
        http_status=status.HTTP_409_CONFLICT,
    )


class CustomerDigitalCartItemCreateApi(CustomerDigitalCartAPIView):
    http_method_names = ("post", "options")

    @extend_schema(
        operation_id="digital_customer_cart_item_add",
        request=AddDigitalCartItemInputSerializer,
        responses={
            status.HTTP_201_CREATED: DigitalCartItemOutputSerializer,
            status.HTTP_400_BAD_REQUEST: DigitalApiErrorSerializer,
            status.HTTP_401_UNAUTHORIZED: DigitalApiErrorSerializer,
            status.HTTP_403_FORBIDDEN: DigitalApiErrorSerializer,
            status.HTTP_404_NOT_FOUND: DigitalApiErrorSerializer,
            status.HTTP_409_CONFLICT: DigitalApiErrorSerializer,
            status.HTTP_405_METHOD_NOT_ALLOWED: DigitalApiErrorSerializer,
        },
        description=(
            "Add one exact customer-visible DigitalOffer to the authenticated customer's Cart. "
            "Price, quantity, Product identity, and inventory authority are server-owned."
        ),
    )
    def post(self, request):
        values, error = _validated_input(AddDigitalCartItemInputSerializer, request.data)
        if error:
            return error
        offer = public_digital_offers().filter(pk=values["offer_id"]).first()
        if offer is None:
            return digital_api_error(
                code="digital_offer_not_found",
                detail="The selected Digital Offer was not found.",
                http_status=status.HTTP_404_NOT_FOUND,
            )
        if (
            offer.capacity == DigitalOfferCapacity.CAPACITY_1
            and values["fulfillment_method"] != DigitalCartFulfillmentMethod.IN_STORE
        ):
            return digital_api_error(
                code="digital_fulfillment_method_not_allowed",
                detail="The fulfillment method is not allowed for this Digital Offer.",
                fields={"fulfillment_method": ["Capacity 1 requires in-store fulfillment."]},
                http_status=status.HTTP_409_CONFLICT,
            )
        try:
            cart = get_cart_or_create(user=request.user)
            item, _ = add_digital_offer_to_cart(
                cart=cart,
                offer=offer,
                fulfillment_method=values["fulfillment_method"],
                actor=request.user,
            )
            item = owned_customer_cart_item(user=request.user, cart_item_id=item.pk)
            return Response(digital_cart_item_projection(item), status=status.HTTP_201_CREATED)
        except (DigitalProductsValidationError, DigitalProductsConflictError, PermissionDenied) as exc:
            return _domain_error_response(exc)


class CustomerDigitalCartItemDeleteApi(CustomerDigitalCartAPIView):
    http_method_names = ("delete", "options")

    @extend_schema(
        operation_id="digital_customer_cart_item_remove",
        responses={
            status.HTTP_200_OK: RemoveDigitalCartItemOutputSerializer,
            status.HTTP_401_UNAUTHORIZED: DigitalApiErrorSerializer,
            status.HTTP_403_FORBIDDEN: DigitalApiErrorSerializer,
            status.HTTP_404_NOT_FOUND: DigitalApiErrorSerializer,
            status.HTTP_409_CONFLICT: DigitalApiErrorSerializer,
            status.HTTP_405_METHOD_NOT_ALLOWED: DigitalApiErrorSerializer,
        },
        description="Remove one owned, unlocked Digital CartItem through the Digital domain service.",
    )
    def delete(self, request, cart_item_id):
        _, error = _owned_digital_item_or_error(
            user=request.user,
            cart_item_id=cart_item_id,
        )
        if error:
            return error
        try:
            remove_digital_cart_item(cart_item_id=cart_item_id, actor=request.user)
            return Response(
                {"removed": True, "cart_item_id": cart_item_id},
                status=status.HTTP_200_OK,
            )
        except (DigitalProductsValidationError, DigitalProductsConflictError, PermissionDenied, ProtectedError) as exc:
            return _domain_error_response(exc)


class CustomerDigitalCartFulfillmentMethodApi(CustomerDigitalCartAPIView):
    http_method_names = ("patch", "options")

    @extend_schema(
        operation_id="digital_customer_cart_fulfillment_method_change",
        request=ChangeDigitalFulfillmentMethodInputSerializer,
        responses={
            status.HTTP_200_OK: DigitalCartItemOutputSerializer,
            status.HTTP_400_BAD_REQUEST: DigitalApiErrorSerializer,
            status.HTTP_401_UNAUTHORIZED: DigitalApiErrorSerializer,
            status.HTTP_403_FORBIDDEN: DigitalApiErrorSerializer,
            status.HTTP_404_NOT_FOUND: DigitalApiErrorSerializer,
            status.HTTP_409_CONFLICT: DigitalApiErrorSerializer,
            status.HTTP_405_METHOD_NOT_ALLOWED: DigitalApiErrorSerializer,
        },
        description="Change only the allowed fulfillment method of one owned Digital CartItem.",
    )
    def patch(self, request, cart_item_id):
        values, error = _validated_input(
            ChangeDigitalFulfillmentMethodInputSerializer,
            request.data,
        )
        if error:
            return error
        item, error = _owned_digital_item_or_error(
            user=request.user,
            cart_item_id=cart_item_id,
        )
        if error:
            return error
        selection = item.digital_selection
        if (
            selection.offer.capacity == DigitalOfferCapacity.CAPACITY_1
            and values["fulfillment_method"] != DigitalCartFulfillmentMethod.IN_STORE
        ):
            return digital_api_error(
                code="digital_fulfillment_method_not_allowed",
                detail="The fulfillment method is not allowed for this Digital Offer.",
                fields={"fulfillment_method": ["Capacity 1 requires in-store fulfillment."]},
                http_status=status.HTTP_409_CONFLICT,
            )
        try:
            change_digital_cart_fulfillment_method(
                cart_item_id=cart_item_id,
                fulfillment_method=values["fulfillment_method"],
                actor=request.user,
            )
            item = owned_customer_cart_item(
                user=request.user,
                cart_item_id=cart_item_id,
            )
            return Response(digital_cart_item_projection(item), status=status.HTTP_200_OK)
        except (DigitalProductsValidationError, DigitalProductsConflictError, PermissionDenied) as exc:
            return _domain_error_response(exc)
