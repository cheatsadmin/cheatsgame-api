from django.db.models import (
    Exists,
    F,
    IntegerField,
    OuterRef,
    Prefetch,
    Q,
    Subquery,
    Sum,
    Value,
)
from django.db.models.functions import Coalesce, Greatest
from django.utils import timezone

from cheatgame.digital_products.models import (
    DigitalGameUpcomingStatus,
    DigitalInventoryReservation,
    DigitalOffer,
    DigitalOfferSaleState,
    InventoryPoolStatus,
)
from cheatgame.digital_products.services.reservations import (
    CURRENT_DIGITAL_RESERVATION_STATES,
)
from cheatgame.product.models import (
    DeliveredVersion,
    NativeConsole,
    Product,
    ProductCommerceAuthority,
    ProductStatus,
    ProductType,
)


EFFECTIVE_PUBLIC_HOLD_STATES = CURRENT_DIGITAL_RESERVATION_STATES

PUBLIC_UPCOMING_STATUSES = (
    DigitalGameUpcomingStatus.ANNOUNCED,
    DigitalGameUpcomingStatus.COMING_SOON,
    DigitalGameUpcomingStatus.DELAYED,
)


def public_digital_offers():
    """Customer-visible Offers with availability calculated in one SQL query."""
    held_quantity = (
        DigitalInventoryReservation.objects.filter(
            inventory_pool_id=OuterRef("inventory_pool_id"),
            state__in=EFFECTIVE_PUBLIC_HOLD_STATES,
        )
        .values("inventory_pool_id")
        .annotate(total=Sum("quantity"))
        .values("total")[:1]
    )
    return (
        DigitalOffer.objects.filter(
            sale_state=DigitalOfferSaleState.ACTIVE,
            inventory_pool__status=InventoryPoolStatus.ENABLED,
            delivered_version__is_active=True,
            delivered_version__product__product_type=ProductType.GAME.value,
            delivered_version__product__status=ProductStatus.PUBLISHED,
            delivered_version__product__commerce_authority=ProductCommerceAuthority.DIGITAL_PRODUCTS,
        )
        .filter(
            Q(delivered_version__product__digital_release_metadata__isnull=True)
            | Q(
                delivered_version__product__digital_release_metadata__upcoming_status=(
                    DigitalGameUpcomingStatus.RELEASED
                )
            )
        )
        .filter(
            Q(customer_console=NativeConsole.PS5)
            | Q(customer_console=NativeConsole.PS4, delivered_version__native_console=NativeConsole.PS4)
        )
        .select_related("delivered_version", "inventory_pool")
        .annotate(
            effective_held_quantity=Coalesce(
                Subquery(held_quantity, output_field=IntegerField()),
                Value(0),
            ),
            customer_available_quantity=Greatest(
                F("inventory_pool__sellable_quantity") - F("effective_held_quantity"),
                Value(0),
            ),
        )
        .order_by("customer_console", "capacity", "delivered_version__native_console", "pk")
    )


def public_upcoming_digital_games(*, console=""):
    today = timezone.localdate()
    queryset = (
        Product.objects.filter(
            product_type=ProductType.GAME.value,
            status=ProductStatus.PUBLISHED,
            commerce_authority=ProductCommerceAuthority.DIGITAL_PRODUCTS,
            digital_release_metadata__upcoming_status__in=PUBLIC_UPCOMING_STATUSES,
            delivered_versions__is_active=True,
        )
        .filter(
            Q(
                digital_release_metadata__upcoming_status=(
                    DigitalGameUpcomingStatus.COMING_SOON
                ),
                digital_release_metadata__release_date__gte=today,
            )
            | (
                Q(
                    digital_release_metadata__upcoming_status__in=(
                        DigitalGameUpcomingStatus.ANNOUNCED,
                        DigitalGameUpcomingStatus.DELAYED,
                    ),
                )
                & (
                    Q(digital_release_metadata__release_date__isnull=True)
                    | Q(digital_release_metadata__release_date__gte=today)
                )
            )
        )
        .exclude(title="")
        .exclude(slug="")
        .exclude(main_image="")
        .select_related("digital_release_metadata")
        .prefetch_related(
            Prefetch(
                "delivered_versions",
                queryset=DeliveredVersion.objects.filter(is_active=True).order_by(
                    "native_console", "pk"
                ),
                to_attr="public_upcoming_versions",
            )
        )
        .distinct()
    )
    if console:
        queryset = queryset.filter(
            delivered_versions__is_active=True,
            delivered_versions__native_console=console,
        )
    return queryset.order_by(
        F("digital_release_metadata__release_date").asc(nulls_last=True),
        "title",
        "pk",
    )


def public_digital_games(
    *,
    search="",
    console="",
    capacity="",
    availability="all",
    ordering="newest",
):
    matching_offers = public_digital_offers().filter(delivered_version__product_id=OuterRef("pk"))
    if console:
        matching_offers = matching_offers.filter(customer_console=console)
    if capacity:
        matching_offers = matching_offers.filter(capacity=capacity)
    if availability == "available":
        matching_offers = matching_offers.filter(customer_available_quantity__gt=0)

    queryset = (
        Product.objects.filter(
            product_type=ProductType.GAME.value,
            status=ProductStatus.PUBLISHED,
            commerce_authority=ProductCommerceAuthority.DIGITAL_PRODUCTS,
        )
        .annotate(has_customer_offer=Exists(matching_offers))
        .filter(has_customer_offer=True)
    )
    if search:
        queryset = queryset.filter(Q(title__icontains=search) | Q(slug__icontains=search))

    minimum_price = (
        public_digital_offers()
        .filter(delivered_version__product_id=OuterRef("pk"))
        .order_by("price", "pk")
        .values("price")[:1]
    )
    queryset = queryset.annotate(minimum_active_offer_price=Subquery(minimum_price))
    if ordering == "title":
        queryset = queryset.order_by("title", "pk")
    elif ordering == "minimum_price":
        queryset = queryset.order_by(F("minimum_active_offer_price").asc(nulls_last=True), "pk")
    else:
        queryset = queryset.order_by("-updated_at", "pk")

    return queryset.prefetch_related(
        Prefetch(
            "delivered_versions__digital_offers",
            queryset=public_digital_offers(),
            to_attr="public_offers",
        )
    )


def public_digital_game_detail(*, slug):
    return public_digital_games().filter(slug=slug).first()


def prefetched_public_offers(product):
    offers = []
    for version in product.delivered_versions.all():
        offers.extend(getattr(version, "public_offers", ()))
    return sorted(
        offers,
        key=lambda offer: (
            offer.customer_console,
            offer.capacity,
            offer.delivered_version.native_console,
            offer.pk,
        ),
    )
