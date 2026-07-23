from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.exceptions import MethodNotAllowed
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from cheatgame.api.pagination import LimitOffsetPagination
from cheatgame.digital_products.public_catalog import public_game_projection
from cheatgame.digital_products.public_catalog_selectors import (
    public_digital_game_detail,
    public_digital_games,
)
from cheatgame.digital_products.public_catalog_serializers import (
    DigitalApiErrorSerializer,
    PaginatedPublicDigitalGameSerializer,
    PublicDigitalGameDetailSerializer,
    PublicDigitalGameFilterSerializer,
)


def digital_api_error(*, code, detail, http_status, fields=None):
    payload = {"code": code, "detail": detail}
    if fields:
        payload["fields"] = fields
    return Response(payload, status=http_status)


class PublicDigitalReadOnlyAPIView(APIView):
    authentication_classes = ()
    permission_classes = (AllowAny,)
    http_method_names = ("get", "head", "options")

    def handle_exception(self, exc):
        if isinstance(exc, MethodNotAllowed):
            return digital_api_error(
                code="method_not_allowed",
                detail="This Digital catalog endpoint is read-only.",
                http_status=status.HTTP_405_METHOD_NOT_ALLOWED,
            )
        return super().handle_exception(exc)


class PublicDigitalGameListApi(PublicDigitalReadOnlyAPIView):
    @extend_schema(
        operation_id="digital_public_game_list",
        parameters=[PublicDigitalGameFilterSerializer],
        responses={
            status.HTTP_200_OK: PaginatedPublicDigitalGameSerializer,
            status.HTTP_400_BAD_REQUEST: DigitalApiErrorSerializer,
            status.HTTP_405_METHOD_NOT_ALLOWED: DigitalApiErrorSerializer,
        },
        description=(
            "Public Digital GAME catalog. DigitalOffer is price authority and InventoryPool minus "
            "effective reservations is availability authority. Exact stock and internal records are omitted."
        ),
    )
    def get(self, request):
        filters = PublicDigitalGameFilterSerializer(data=request.query_params)
        if not filters.is_valid():
            return digital_api_error(
                code="invalid_request",
                detail="Digital catalog filters are invalid.",
                fields=filters.errors,
                http_status=status.HTTP_400_BAD_REQUEST,
            )
        values = dict(filters.validated_data)
        values.pop("limit", None)
        values.pop("offset", None)
        queryset = public_digital_games(**values)
        paginator = LimitOffsetPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        return paginator.get_paginated_response(
            [public_game_projection(product) for product in page]
        )


class PublicDigitalGameDetailApi(PublicDigitalReadOnlyAPIView):
    @extend_schema(
        operation_id="digital_public_game_detail",
        responses={
            status.HTTP_200_OK: PublicDigitalGameDetailSerializer,
            status.HTTP_404_NOT_FOUND: DigitalApiErrorSerializer,
            status.HTTP_405_METHOD_NOT_ALLOWED: DigitalApiErrorSerializer,
        },
        description="Public Digital GAME detail with customer-visible active Offers only.",
    )
    def get(self, request, slug):
        product = public_digital_game_detail(slug=slug)
        if product is None:
            return digital_api_error(
                code="digital_game_not_found",
                detail="Digital Game was not found.",
                http_status=status.HTTP_404_NOT_FOUND,
            )
        return Response(public_game_projection(product, detail=True))
