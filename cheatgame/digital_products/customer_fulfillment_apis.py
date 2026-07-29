from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Q
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.exceptions import AuthenticationFailed, NotAuthenticated
from rest_framework.generics import GenericAPIView
from rest_framework.response import Response

from cheatgame.api.mixins import ApiAuthMixin
from cheatgame.api.pagination import LimitOffsetPagination
from cheatgame.digital_products.customer_checkout_apis import (
    ActiveVerifiedCheckoutCustomerPermission,
)
from cheatgame.digital_products.fulfillment_selectors import (
    customer_fulfillment_item,
    customer_fulfillment_items,
)
from cheatgame.digital_products.fulfillment_serializers import (
    CustomerDigitalFulfillmentListSerializer,
    CustomerDigitalFulfillmentProjectionSerializer,
)
from cheatgame.digital_products.models import (
    DigitalFulfillmentItem,
    DigitalFulfillmentStatus,
)
from cheatgame.digital_products.services.fulfillment import (
    DigitalFulfillmentConflict,
    DigitalFulfillmentValidationError,
    customer_confirm_remote_completion,
)


def _error(*, code, detail, http_status, fields=None):
    payload = {"code": code, "detail": detail}
    if fields:
        payload["fields"] = fields
    return Response(payload, status=http_status)


class CustomerFulfillmentApi(ApiAuthMixin, GenericAPIView):
    permission_classes = (ActiveVerifiedCheckoutCustomerPermission,)
    queryset = DigitalFulfillmentItem.objects.none()

    def handle_exception(self, exc):
        if isinstance(exc, (NotAuthenticated, AuthenticationFailed)):
            return _error(
                code="authentication_required",
                detail="Authentication is required.",
                http_status=status.HTTP_401_UNAUTHORIZED,
            )
        return super().handle_exception(exc)


class CustomerFulfillmentFilterSerializer(serializers.Serializer):
    view = serializers.ChoiceField(
        choices=("active", "completed"),
        required=False,
        default="active",
    )
    search = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        max_length=200,
    )


class ConfirmRemoteCompletionSerializer(serializers.Serializer):
    idempotency_key = serializers.UUIDField()


class CustomerFulfillmentListApi(CustomerFulfillmentApi):
    serializer_class = CustomerDigitalFulfillmentListSerializer
    pagination_class = LimitOffsetPagination

    @extend_schema(
        parameters=[CustomerFulfillmentFilterSerializer],
        responses={200: CustomerDigitalFulfillmentListSerializer(many=True)},
    )
    def get(self, request):
        filters = CustomerFulfillmentFilterSerializer(data=request.query_params)
        if not filters.is_valid():
            return _error(
                code="invalid_request",
                detail="The fulfillment list request is invalid.",
                fields=filters.errors,
                http_status=status.HTTP_400_BAD_REQUEST,
            )

        queryset = customer_fulfillment_items(request.user)
        if filters.validated_data["view"] == "completed":
            queryset = queryset.filter(status=DigitalFulfillmentStatus.COMPLETED)
        else:
            queryset = queryset.exclude(status=DigitalFulfillmentStatus.COMPLETED)

        search = filters.validated_data.get("search", "")
        if search:
            queryset = queryset.filter(
                Q(
                    obligation__checkout_line__digital_snapshot__product_name__icontains=search
                )
                | Q(obligation__order__public_tracking_code__icontains=search)
            )

        paginator = self.pagination_class()
        page = paginator.paginate_queryset(queryset, request, view=self)
        serializer = self.serializer_class(page, many=True)
        return paginator.get_paginated_response(serializer.data)


class CustomerFulfillmentDetailApi(CustomerFulfillmentApi):
    serializer_class = CustomerDigitalFulfillmentProjectionSerializer

    @extend_schema(responses={200: CustomerDigitalFulfillmentProjectionSerializer})
    def get(self, request, fulfillment_id):
        try:
            item = customer_fulfillment_item(
                public_id=fulfillment_id,
                customer=request.user,
            )
        except ObjectDoesNotExist:
            return _error(
                code="digital_fulfillment_not_found",
                detail="Digital fulfillment was not found.",
                http_status=status.HTTP_404_NOT_FOUND,
            )
        return Response(self.serializer_class(item).data)


class CustomerFulfillmentConfirmRemoteCompletionApi(CustomerFulfillmentApi):
    serializer_class = CustomerDigitalFulfillmentProjectionSerializer

    @extend_schema(
        request=ConfirmRemoteCompletionSerializer,
        responses={200: CustomerDigitalFulfillmentProjectionSerializer},
    )
    def post(self, request, fulfillment_id):
        request_serializer = ConfirmRemoteCompletionSerializer(data=request.data)
        if not request_serializer.is_valid():
            return _error(
                code="invalid_request",
                detail="The remote completion confirmation is invalid.",
                fields=request_serializer.errors,
                http_status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            customer_fulfillment_item(
                public_id=fulfillment_id,
                customer=request.user,
            )
        except ObjectDoesNotExist:
            return _error(
                code="digital_fulfillment_not_found",
                detail="Digital fulfillment was not found.",
                http_status=status.HTTP_404_NOT_FOUND,
            )

        try:
            customer_confirm_remote_completion(
                fulfillment_id=fulfillment_id,
                actor=request.user,
                idempotency_key=request_serializer.validated_data[
                    "idempotency_key"
                ],
            )
        except DigitalFulfillmentConflict:
            return _error(
                code="digital_fulfillment_confirmation_conflict",
                detail="This confirmation conflicts with an earlier request.",
                http_status=status.HTTP_409_CONFLICT,
            )
        except DigitalFulfillmentValidationError:
            return _error(
                code="digital_fulfillment_confirmation_unavailable",
                detail="Remote completion cannot be confirmed in the current state.",
                http_status=status.HTTP_409_CONFLICT,
            )

        item = customer_fulfillment_item(
            public_id=fulfillment_id,
            customer=request.user,
        )
        return Response(self.serializer_class(item).data)
