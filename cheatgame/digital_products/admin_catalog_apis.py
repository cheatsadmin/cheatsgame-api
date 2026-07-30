from django.core.exceptions import PermissionDenied
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.exceptions import AuthenticationFailed, NotAuthenticated
from rest_framework.generics import GenericAPIView
from rest_framework.response import Response

from cheatgame.api.mixins import ApiAuthMixin
from cheatgame.api.pagination import LimitOffsetPagination
from cheatgame.digital_products.admin_catalog import (
    admin_catalog_game_list_projection,
    admin_catalog_game_projection,
    admin_catalog_games,
    filter_admin_catalog_games,
    readiness_result_projection,
)
from cheatgame.digital_products.models import (
    DigitalGameUpcomingStatus,
    DigitalOffer,
    DigitalOfferCapacity,
    DigitalOfferSaleState,
    PoolStockAdjustmentReason,
)
from cheatgame.digital_products.services import (
    DigitalProductsConflictError,
    DigitalProductsValidationError,
    OfferTransitionError,
)
from cheatgame.digital_products.services.catalog_admin import (
    activate_digital_product,
    archive_delivered_version,
    create_delivered_version,
    deactivate_digital_product,
)
from cheatgame.digital_products.services.inventory import (
    adjust_pool_stock,
    enable_inventory_pool,
    pause_inventory_pool,
)
from cheatgame.digital_products.services.offers import (
    create_digital_offer,
    link_offer_to_shared_pool,
    move_offer_to_new_independent_pool,
    transition_offer_sale_state,
    update_offer_price,
)
from cheatgame.digital_products.services.upcoming_games import (
    update_upcoming_game_metadata,
)
from cheatgame.product.models import (
    DeliveredVersion,
    NativeConsole,
    Product,
    ProductCommerceAuthority,
    ProductStatus,
    ProductType,
)
from cheatgame.product.permissions import AdminOrManagerPermission


def _error(*, code, detail, http_status, fields=None, readiness=None):
    payload = {"code": code, "detail": detail}
    if fields:
        payload["fields"] = fields
    if readiness is not None:
        payload["readiness"] = readiness
    return Response(payload, status=http_status)


class AdminCatalogApi(ApiAuthMixin, GenericAPIView):
    permission_classes = (AdminOrManagerPermission,)

    def handle_exception(self, exc):
        if isinstance(exc, (NotAuthenticated, AuthenticationFailed)):
            return _error(
                code="authentication_required",
                detail="Authentication is required.",
                http_status=status.HTTP_401_UNAUTHORIZED,
            )
        if isinstance(exc, PermissionDenied):
            return _error(
                code="catalog_permission_denied",
                detail="This catalog action is not permitted.",
                http_status=status.HTTP_403_FORBIDDEN,
            )
        return super().handle_exception(exc)


class CatalogGameFilterSerializer(serializers.Serializer):
    search = serializers.CharField(required=False, max_length=100)
    commerce_authority = serializers.ChoiceField(
        choices=("standard_commerce", "digital_game"),
        required=False,
    )
    readiness = serializers.ChoiceField(
        choices=("ready", "not_ready"),
        required=False,
    )
    offers = serializers.ChoiceField(
        choices=("has_offers", "no_offers"),
        required=False,
    )
    status = serializers.ChoiceField(
        choices=ProductStatus.values,
        required=False,
    )
    limit = serializers.IntegerField(required=False, min_value=1, max_value=50)
    offset = serializers.IntegerField(required=False, min_value=0)


def _game(product_id):
    return (
        admin_catalog_games()
        .filter(pk=product_id, product_type=ProductType.GAME.value)
        .first()
    )


def _detail_response(product_id, actor):
    product = _game(product_id)
    if product is None:
        return _error(
            code="catalog_game_not_found",
            detail="Catalog game was not found.",
            http_status=status.HTTP_404_NOT_FOUND,
        )
    response = Response(admin_catalog_game_projection(product, actor=actor))
    response["Cache-Control"] = "no-store, private"
    return response


def _domain_error(exc):
    readiness = getattr(exc, "readiness", None)
    if readiness is not None:
        readiness = readiness_result_projection(readiness)
    if isinstance(exc, (DigitalProductsConflictError, OfferTransitionError)):
        return _error(
            code=exc.code,
            detail=str(exc),
            readiness=readiness,
            http_status=status.HTTP_409_CONFLICT,
        )
    if isinstance(exc, DigitalProductsValidationError):
        return _error(
            code=exc.code,
            detail=str(exc),
            http_status=status.HTTP_400_BAD_REQUEST,
        )
    raise exc


class AdminCatalogGameListApi(AdminCatalogApi):
    http_method_names = ("get", "head", "options")

    @extend_schema(
        operation_id="admin_digital_catalog_game_list",
        parameters=[CatalogGameFilterSerializer],
        responses=OpenApiTypes.OBJECT,
    )
    def get(self, request):
        filters = CatalogGameFilterSerializer(data=request.query_params)
        if not filters.is_valid():
            return _error(
                code="invalid_catalog_filters",
                detail="Catalog filters are invalid.",
                fields=filters.errors,
                http_status=status.HTTP_400_BAD_REQUEST,
            )
        values = dict(filters.validated_data)
        values.pop("limit", None)
        values.pop("offset", None)
        games = filter_admin_catalog_games(admin_catalog_games(), values)
        paginator = LimitOffsetPagination()
        page = paginator.paginate_queryset(games, request, view=self)
        return paginator.get_paginated_response(
            [admin_catalog_game_list_projection(game) for game in page]
        )


class AdminCatalogGameDetailApi(AdminCatalogApi):
    http_method_names = ("get", "head", "options")

    @extend_schema(
        operation_id="admin_digital_catalog_game_detail",
        responses=OpenApiTypes.OBJECT,
    )
    def get(self, request, product_id):
        return _detail_response(product_id, request.user)


class NativeConsoleSerializer(serializers.Serializer):
    native_console = serializers.ChoiceField(choices=NativeConsole.values)


class CreateOfferSerializer(serializers.Serializer):
    delivered_version_id = serializers.IntegerField(min_value=1)
    customer_console = serializers.ChoiceField(choices=NativeConsole.values)
    capacity = serializers.ChoiceField(choices=DigitalOfferCapacity.values)
    price = serializers.DecimalField(max_digits=15, decimal_places=0, min_value=0)
    initial_stock = serializers.IntegerField(min_value=0, default=0)


class PriceSerializer(serializers.Serializer):
    price = serializers.DecimalField(max_digits=15, decimal_places=0, min_value=0)


class UpcomingGameMetadataSerializer(serializers.Serializer):
    release_date = serializers.DateField(required=False, allow_null=True)
    upcoming_status = serializers.ChoiceField(
        choices=DigitalGameUpcomingStatus.choices
    )
    preorder_enabled = serializers.BooleanField(default=False)
    preorder_open_at = serializers.DateTimeField(required=False, allow_null=True)
    preorder_close_at = serializers.DateTimeField(required=False, allow_null=True)
    publish = serializers.BooleanField()


class OfferStateSerializer(serializers.Serializer):
    sale_state = serializers.ChoiceField(choices=DigitalOfferSaleState.values)


class StockAdjustmentSerializer(serializers.Serializer):
    delta = serializers.IntegerField()
    reason = serializers.ChoiceField(choices=PoolStockAdjustmentReason.values)
    idempotency_key = serializers.UUIDField()

    def validate_delta(self, value):
        if value == 0:
            raise serializers.ValidationError("Stock delta cannot be zero.")
        return value


class ShareStockSerializer(serializers.Serializer):
    source_offer_id = serializers.IntegerField(min_value=1)


class _CatalogCommandApi(AdminCatalogApi):
    http_method_names = ("post", "options")
    input_serializer_class = serializers.Serializer

    def execute(self, request, values, **kwargs):
        raise NotImplementedError

    @extend_schema(request=OpenApiTypes.OBJECT, responses=OpenApiTypes.OBJECT)
    def post(self, request, **kwargs):
        serializer = self.input_serializer_class(data=request.data)
        if not serializer.is_valid():
            return _error(
                code="invalid_catalog_command",
                detail="Catalog command is invalid.",
                fields=serializer.errors,
                http_status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            product_id = self.execute(
                request,
                serializer.validated_data,
                **kwargs,
            )
            return _detail_response(product_id, request.user)
        except (DigitalProductsValidationError, DigitalProductsConflictError) as exc:
            return _domain_error(exc)


class CreateDeliveredVersionApi(_CatalogCommandApi):
    input_serializer_class = NativeConsoleSerializer

    def execute(self, request, values, product_id):
        create_delivered_version(
            product_id=product_id,
            native_console=values["native_console"],
            actor=request.user,
        )
        return product_id


class UpdateUpcomingGameMetadataApi(_CatalogCommandApi):
    input_serializer_class = UpcomingGameMetadataSerializer

    def execute(self, request, values, product_id):
        update_upcoming_game_metadata(
            product_id=product_id,
            release_date=values.get("release_date"),
            upcoming_status=values["upcoming_status"],
            preorder_enabled=values["preorder_enabled"],
            preorder_open_at=values.get("preorder_open_at"),
            preorder_close_at=values.get("preorder_close_at"),
            publish=values["publish"],
            actor=request.user,
        )
        return product_id


class ArchiveDeliveredVersionApi(_CatalogCommandApi):
    def execute(self, request, values, version_id):
        version = DeliveredVersion.objects.filter(pk=version_id).first()
        if version is None:
            raise DigitalProductsValidationError(
                "Delivered Version does not exist."
            )
        product_id = version.product_id
        archive_delivered_version(version_id=version_id, actor=request.user)
        return product_id


class CreateDigitalOfferApi(_CatalogCommandApi):
    input_serializer_class = CreateOfferSerializer

    def execute(self, request, values, product_id):
        version = DeliveredVersion.objects.filter(
            pk=values["delivered_version_id"],
            product_id=product_id,
        ).first()
        if version is None:
            raise DigitalProductsValidationError(
                "Delivered Version does not belong to this game."
            )
        create_digital_offer(
            delivered_version_id=version.pk,
            customer_console=values["customer_console"],
            capacity=values["capacity"],
            price=values["price"],
            initial_stock=values["initial_stock"],
            actor=request.user,
        )
        return product_id


class _OfferCommandApi(_CatalogCommandApi):
    def offer(self, offer_id):
        offer = DigitalOffer.objects.select_related(
            "delivered_version",
        ).filter(pk=offer_id).first()
        if offer is None:
            raise DigitalProductsValidationError(
                "Digital Offer does not exist."
            )
        return offer


class UpdateOfferPriceApi(_OfferCommandApi):
    input_serializer_class = PriceSerializer

    def execute(self, request, values, offer_id):
        offer = self.offer(offer_id)
        update_offer_price(
            offer_id=offer_id,
            price=values["price"],
            actor=request.user,
        )
        return offer.delivered_version.product_id


class ChangeOfferStateApi(_OfferCommandApi):
    input_serializer_class = OfferStateSerializer

    def execute(self, request, values, offer_id):
        offer = self.offer(offer_id)
        transition_offer_sale_state(
            offer_id=offer_id,
            target_state=values["sale_state"],
            actor=request.user,
        )
        return offer.delivered_version.product_id


class AdjustOfferStockApi(_OfferCommandApi):
    input_serializer_class = StockAdjustmentSerializer

    def execute(self, request, values, offer_id):
        offer = self.offer(offer_id)
        adjust_pool_stock(
            pool_id=offer.inventory_pool_id,
            delta=values["delta"],
            reason=values["reason"],
            actor=request.user,
            idempotency_key=values["idempotency_key"],
        )
        return offer.delivered_version.product_id


class ShareOfferStockApi(_OfferCommandApi):
    input_serializer_class = ShareStockSerializer

    def execute(self, request, values, offer_id):
        offer = self.offer(offer_id)
        source = self.offer(values["source_offer_id"])
        link_offer_to_shared_pool(
            offer_id=offer_id,
            target_pool_id=source.inventory_pool_id,
            actor=request.user,
        )
        return offer.delivered_version.product_id


class MakeOfferStockIndependentApi(_OfferCommandApi):
    def execute(self, request, values, offer_id):
        offer = self.offer(offer_id)
        move_offer_to_new_independent_pool(
            offer_id=offer_id,
            actor=request.user,
        )
        return offer.delivered_version.product_id


class EnableOfferInventoryApi(_OfferCommandApi):
    def execute(self, request, values, offer_id):
        offer = self.offer(offer_id)
        enable_inventory_pool(offer_id=offer_id, actor=request.user)
        return offer.delivered_version.product_id


class PauseOfferInventoryApi(_OfferCommandApi):
    def execute(self, request, values, offer_id):
        offer = self.offer(offer_id)
        pause_inventory_pool(offer_id=offer_id, actor=request.user)
        return offer.delivered_version.product_id


class ActivateDigitalProductApi(_CatalogCommandApi):
    def execute(self, request, values, product_id):
        activate_digital_product(product_id=product_id, actor=request.user)
        return product_id


class DeactivateDigitalProductApi(_CatalogCommandApi):
    def execute(self, request, values, product_id):
        deactivate_digital_product(product_id=product_id, actor=request.user)
        return product_id
