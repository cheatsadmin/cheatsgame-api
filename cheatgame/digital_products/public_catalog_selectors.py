import re

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
from django.db.models.functions import Coalesce, Greatest, Lower, Replace
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
    DigitalGameUpcomingStatus.PREORDER_OPEN,
    DigitalGameUpcomingStatus.DELAYED,
)

_LOCALIZED_DIGITS = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
    "01234567890123456789",
)


def _public_catalog_search_terms(search):
    normalized = " ".join(
        str(search or "").translate(_LOCALIZED_DIGITS).split()
    ).lower()
    if not normalized:
        return ()
    terms = {normalized}
    terms.add(re.sub(r"\b6\b", "vi", normalized))
    terms.add(re.sub(r"\bvi\b", "6", normalized))
    return tuple(term for term in terms if term)


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
                delivered_version__product__digital_release_metadata__upcoming_status__in=(
                    DigitalGameUpcomingStatus.PREORDER_OPEN,
                    DigitalGameUpcomingStatus.RELEASED,
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
                    digital_release_metadata__upcoming_status__in=(
                        DigitalGameUpcomingStatus.COMING_SOON,
                        DigitalGameUpcomingStatus.PREORDER_OPEN,
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
    search_terms = _public_catalog_search_terms(search)
    if search_terms:
        queryset = queryset.annotate(
            _compact_search_title=Lower(
                Replace(Replace("title", Value(" "), Value("")), Value("-"), Value(""))
            ),
            _compact_search_slug=Lower(
                Replace(Replace("slug", Value(" "), Value("")), Value("-"), Value(""))
            ),
        )
        search_filter = Q()
        for term in search_terms:
            compact_term = term.replace(" ", "").replace("-", "")
            search_filter |= (
                Q(title__icontains=term)
                | Q(slug__icontains=term)
                | Q(_compact_search_title__icontains=compact_term)
                | Q(_compact_search_slug__icontains=compact_term)
            )
        queryset = queryset.filter(search_filter)

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

    return queryset.select_related("digital_release_metadata").prefetch_related(
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
