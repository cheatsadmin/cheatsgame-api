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

from cheatgame.digital_products.models import (
    DigitalInventoryReservation,
    DigitalInventoryReservationState,
    DigitalOffer,
    DigitalOfferSaleState,
    InventoryPoolStatus,
)
from cheatgame.product.models import (
    NativeConsole,
    Product,
    ProductCommerceAuthority,
    ProductStatus,
    ProductType,
)


EFFECTIVE_PUBLIC_HOLD_STATES = (
    DigitalInventoryReservationState.ACTIVE,
    DigitalInventoryReservationState.PAYMENT_HOLD,
    DigitalInventoryReservationState.HELD_FOR_REVIEW,
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


def public_digital_games(*, search="", console="", capacity="", ordering="newest"):
    matching_offers = public_digital_offers().filter(delivered_version__product_id=OuterRef("pk"))
    if console:
        matching_offers = matching_offers.filter(customer_console=console)
    if capacity:
        matching_offers = matching_offers.filter(capacity=capacity)

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
