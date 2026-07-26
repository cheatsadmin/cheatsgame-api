from django.db.models import Prefetch, Q

from cheatgame.digital_products.models import (
    DigitalGameUpcomingStatus,
    DigitalOffer,
    DigitalOfferSaleState,
)
from cheatgame.digital_products.public_catalog import compatibility_code_for
from cheatgame.digital_products.services.catalog_admin import (
    evaluate_product_readiness,
)
from cheatgame.digital_products.services.inventory import (
    get_effective_held_quantity,
)
from cheatgame.product.models import (
    AttachmentType,
    Product,
    ProductCommerceAuthority,
    ProductStatus,
    ProductType,
)
from cheatgame.users.models import UserTypes


_READINESS_LABELS = {
    "NOT_GAME": "این محصول بازی نیست.",
    "ACTIVE_OFFER": "ابتدا همه گزینه‌های فروش فعال را متوقف کنید.",
    "NO_ACTIVE_VERSION": "نسخه تحویلی فعال تعریف نشده است.",
    "NO_OFFER": "گزینه فروش دیجیتال تعریف نشده است.",
    "INVALID_OFFER": "یکی از گزینه‌های فروش پیکربندی معتبر ندارد.",
    "INCOMPATIBLE_SHARED_POOL": "موجودی مشترک ناسازگار است.",
    "LEGACY_QUANTITY_IGNORED": "موجودی قدیمی محصول در فروش دیجیتال استفاده نمی‌شود.",
}


def _commerce_authority(value):
    if value == ProductCommerceAuthority.DIGITAL_PRODUCTS:
        return "digital_game"
    return ProductCommerceAuthority.STANDARD_COMMERCE


def _release_metadata_projection(product):
    metadata = getattr(product, "digital_release_metadata", None)
    return {
        "configured": metadata is not None,
        "release_date": metadata.release_date if metadata else None,
        "upcoming_status": (
            metadata.upcoming_status
            if metadata
            else DigitalGameUpcomingStatus.RELEASED
        ),
        "preorder_enabled": bool(
            metadata and metadata.preorder_enabled
        ),
        "preorder_open_at": (
            metadata.preorder_open_at if metadata else None
        ),
        "preorder_close_at": (
            metadata.preorder_close_at if metadata else None
        ),
        "published": product.status == ProductStatus.PUBLISHED,
        "preorder_commerce_supported": False,
    }


def readiness_result_projection(result):
    def messages(codes):
        return [
            {"code": code, "label": _READINESS_LABELS.get(code, code)}
            for code in codes
        ]

    return {
        "ready": result["ready"],
        "issues": messages(result["issues"]),
        "warnings": messages(result["warnings"]),
    }


def _readiness_projection(product, *, for_deactivation=False):
    return readiness_result_projection(
        evaluate_product_readiness(
            product,
            for_deactivation=for_deactivation,
        )
    )


def admin_catalog_games():
    offers = DigitalOffer.objects.select_related(
        "delivered_version",
        "inventory_pool",
    ).order_by("pk")
    return (
        Product.objects.filter(product_type=ProductType.GAME.value)
        .select_related("digital_release_metadata")
        .prefetch_related(
            "attachments",
            "delivered_versions",
            Prefetch(
                "delivered_versions__digital_offers",
                queryset=offers,
            ),
        )
        .order_by("-updated_at", "pk")
    )


def filter_admin_catalog_games(queryset, values):
    if values.get("search"):
        term = values["search"]
        queryset = queryset.filter(
            Q(title__icontains=term) | Q(slug__icontains=term)
        )
    if values.get("commerce_authority"):
        authority = values["commerce_authority"]
        if authority == "digital_game":
            authority = ProductCommerceAuthority.DIGITAL_PRODUCTS
        queryset = queryset.filter(commerce_authority=authority)
    if values.get("status"):
        queryset = queryset.filter(status=values["status"])
    if values.get("offers") == "has_offers":
        queryset = queryset.filter(
            delivered_versions__digital_offers__isnull=False
        ).distinct()
    if values.get("offers") == "no_offers":
        queryset = queryset.filter(
            delivered_versions__digital_offers__isnull=True
        )
    if values.get("readiness"):
        expected = values["readiness"] == "ready"
        queryset = [
            product
            for product in queryset
            if evaluate_product_readiness(product)["ready"] is expected
        ]
    return queryset


def _offers(product):
    result = []
    for version in product.delivered_versions.all():
        result.extend(version.digital_offers.all())
    return sorted(result, key=lambda offer: offer.pk)


def admin_catalog_game_list_projection(product):
    offers = _offers(product)
    non_archived = [
        offer
        for offer in offers
        if offer.sale_state != DigitalOfferSaleState.ARCHIVED
    ]
    return {
        "id": product.pk,
        "title": product.title,
        "slug": product.slug,
        "status": product.status,
        "commerce_authority": _commerce_authority(
            product.commerce_authority
        ),
        "delivered_version_count": len(product.delivered_versions.all()),
        "offer_count": len(offers),
        "active_offer_count": sum(
            offer.sale_state == DigitalOfferSaleState.ACTIVE
            for offer in offers
        ),
        "configured_options": [
            {
                "customer_console": offer.customer_console,
                "capacity": offer.capacity,
            }
            for offer in non_archived
        ],
        "readiness": _readiness_projection(product),
        "release_metadata": _release_metadata_projection(product),
        "updated_at": product.updated_at,
    }


def _offer_allowed_actions(offer, actor):
    actions = ["update_price", "adjust_stock"]
    if offer.sale_state != DigitalOfferSaleState.ARCHIVED:
        actions.append("change_state")
        if actor.user_type == UserTypes.ADMIN:
            actions.extend(("share_stock", "independent_stock"))
    return actions


def admin_catalog_game_projection(product, *, actor):
    offers = _offers(product)
    pool_offer_map = {}
    for offer in offers:
        pool_offer_map.setdefault(offer.inventory_pool_id, []).append(offer)
    held_by_pool = {
        pool_id: get_effective_held_quantity(pool_id=pool_id)
        for pool_id in pool_offer_map
    }

    versions = []
    for version in product.delivered_versions.all():
        referenced = version.digital_offers.exclude(
            sale_state=DigitalOfferSaleState.ARCHIVED
        ).exists()
        versions.append(
            {
                "id": version.pk,
                "native_console": version.native_console,
                "display_label": version.get_native_console_display(),
                "is_active": version.is_active,
                "referenced_by_non_archived_offers": referenced,
                "allowed_actions": (
                    ["archive"]
                    if version.is_active and not referenced
                    else []
                ),
                "created_at": version.created_at,
                "updated_at": version.updated_at,
            }
        )

    offer_rows = []
    for offer in offers:
        pool_offers = pool_offer_map[offer.inventory_pool_id]
        held = held_by_pool[offer.inventory_pool_id]
        shared_with = [
            {
                "offer_id": peer.pk,
                "customer_console": peer.customer_console,
                "capacity": peer.capacity,
            }
            for peer in pool_offers
            if peer.pk != offer.pk
        ]
        offer_rows.append(
            {
                "id": offer.pk,
                "customer_console": offer.customer_console,
                "capacity": offer.capacity,
                "delivered_version": {
                    "id": offer.delivered_version_id,
                    "native_console": offer.delivered_version.native_console,
                    "display_label": (
                        offer.delivered_version.get_native_console_display()
                    ),
                },
                "compatibility": compatibility_code_for(
                    customer_console=offer.customer_console,
                    native_console=offer.delivered_version.native_console,
                ),
                "price": str(offer.price),
                "sale_state": offer.sale_state,
                "inventory": {
                    "status": offer.inventory_pool.status,
                    "gross_sellable_quantity": (
                        offer.inventory_pool.sellable_quantity
                    ),
                    "held_quantity": held,
                    "available_quantity": max(
                        offer.inventory_pool.sellable_quantity - held,
                        0,
                    ),
                    "mode": "shared" if shared_with else "independent",
                    "shared_with": shared_with,
                },
                "allowed_actions": _offer_allowed_actions(offer, actor),
                "created_at": offer.created_at,
                "updated_at": offer.updated_at,
            }
        )

    return {
        "game": {
            "id": product.pk,
            "title": product.title,
            "slug": product.slug,
            "status": product.status,
            "commerce_authority": _commerce_authority(
                product.commerce_authority
            ),
            "legacy_capacity_attachments_present": (
                product.attachments.filter(
                    attachment_type=AttachmentType.CAPACITY.value
                ).exists()
            ),
            "legacy_quantity_present": bool(product.quantity),
            "updated_at": product.updated_at,
        },
        "release_metadata": _release_metadata_projection(product),
        "readiness": _readiness_projection(product),
        "delivered_versions": versions,
        "offers": offer_rows,
    }
