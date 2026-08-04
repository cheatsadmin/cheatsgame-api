from django.db import transaction
from django.utils import timezone

from cheatgame.digital_products.models import (
    DigitalGameReleaseMetadata,
    DigitalGameUpcomingStatus,
    DigitalOfferSaleState,
)
from cheatgame.digital_products.services import (
    DigitalProductsValidationError,
    require_manager_or_admin,
)
from cheatgame.product.models import (
    Product,
    ProductCommerceAuthority,
    ProductStatus,
    ProductType,
)


UPCOMING_DISPLAY_STATUSES = {
    DigitalGameUpcomingStatus.ANNOUNCED,
    DigitalGameUpcomingStatus.COMING_SOON,
    DigitalGameUpcomingStatus.DELAYED,
}

DATED_PUBLIC_STATES = {
    DigitalGameUpcomingStatus.COMING_SOON,
    DigitalGameUpcomingStatus.PREORDER_OPEN,
}


def evaluate_upcoming_readiness(product, *, use_prefetched=False):
    """Return the public-display gates without consulting commercial Offers."""
    metadata = getattr(product, "digital_release_metadata", None)
    display_status = bool(
        metadata and metadata.upcoming_status in UPCOMING_DISPLAY_STATUSES
    )
    release_date = metadata.release_date if metadata else None
    release_information_coherent = bool(
        display_status
        and (
            metadata.upcoming_status not in DATED_PUBLIC_STATES
            or release_date is not None
        )
        and (release_date is None or release_date >= timezone.localdate())
    )
    prefetched_versions = getattr(
        product,
        "_prefetched_objects_cache",
        {},
    ).get("delivered_versions") if use_prefetched else None
    has_active_version = (
        any(version.is_active for version in prefetched_versions)
        if prefetched_versions is not None
        else product.delivered_versions.filter(is_active=True).exists()
    )
    gates = [
        {
            "code": "GAME_PRODUCT",
            "passed": product.product_type == ProductType.GAME.value,
        },
        {
            "code": "DIGITAL_AUTHORITY",
            "passed": (
                product.commerce_authority
                == ProductCommerceAuthority.DIGITAL_PRODUCTS
            ),
        },
        {
            "code": "PUBLIC_PRODUCT",
            "passed": product.status == ProductStatus.PUBLISHED,
        },
        {
            "code": "UPCOMING_STATUS",
            "passed": display_status,
        },
        {
            "code": "RELEASE_INFORMATION",
            "passed": release_information_coherent,
        },
        {
            "code": "ACTIVE_VERSION",
            "passed": has_active_version,
        },
        {
            "code": "PUBLIC_IDENTITY",
            "passed": bool(
                str(product.title or "").strip()
                and str(product.slug or "").strip()
                and product.main_image
            ),
        },
    ]
    return {
        "ready": all(gate["passed"] for gate in gates),
        "ready_for_authority": all(
            gate["passed"]
            for gate in gates
            if gate["code"] != "DIGITAL_AUTHORITY"
        ),
        "gates": gates,
    }


def update_upcoming_game_metadata(
    *,
    product_id,
    release_date,
    upcoming_status,
    preorder_enabled,
    preorder_open_at,
    preorder_close_at,
    publish,
    actor,
):
    require_manager_or_admin(actor)
    if upcoming_status not in DigitalGameUpcomingStatus.values:
        raise DigitalProductsValidationError("Upcoming status is invalid.")
    expected_preorder = upcoming_status == DigitalGameUpcomingStatus.PREORDER_OPEN
    if bool(preorder_enabled) != expected_preorder:
        raise DigitalProductsValidationError(
            "Preorder enablement must exactly match the PREORDER_OPEN Product state."
        )
    if preorder_open_at is not None or preorder_close_at is not None:
        raise DigitalProductsValidationError(
            "Preorder V1 does not use separate sale windows."
        )
    if (
        preorder_open_at
        and preorder_close_at
        and preorder_close_at <= preorder_open_at
    ):
        raise DigitalProductsValidationError(
            "Preorder close time must follow the open time."
        )
    if upcoming_status in UPCOMING_DISPLAY_STATUSES | {
        DigitalGameUpcomingStatus.PREORDER_OPEN
    }:
        if (
            upcoming_status in DATED_PUBLIC_STATES
            and release_date is None
        ):
            raise DigitalProductsValidationError(
                "This publication state requires a release date."
            )
        if release_date is not None and release_date < timezone.localdate():
            raise DigitalProductsValidationError(
                "Upcoming release date cannot be in the past."
            )

    with transaction.atomic():
        try:
            product = Product.objects.select_for_update().get(
                pk=product_id,
                product_type=ProductType.GAME.value,
            )
        except Product.DoesNotExist as exc:
            raise DigitalProductsValidationError("Catalog game does not exist.") from exc

        if (
            upcoming_status in UPCOMING_DISPLAY_STATUSES
            and product.delivered_versions.filter(
                digital_offers__sale_state=DigitalOfferSaleState.ACTIVE
            ).exists()
        ):
            raise DigitalProductsValidationError(
                "Pause every active Offer before marking a game as upcoming."
            )

        metadata, _ = DigitalGameReleaseMetadata.objects.select_for_update().get_or_create(
            product=product
        )
        previous_status = metadata.upcoming_status
        metadata.release_date = release_date
        metadata.upcoming_status = upcoming_status
        metadata.preorder_enabled = expected_preorder
        metadata.preorder_open_at = None
        metadata.preorder_close_at = None
        metadata.save()

        if (
            upcoming_status == DigitalGameUpcomingStatus.RELEASED
            and previous_status != DigitalGameUpcomingStatus.RELEASED
        ):
            from cheatgame.digital_products.services.preorders import (
                release_paid_preorders_for_product,
            )

            release_paid_preorders_for_product(product=product)

        target_status = ProductStatus.PUBLISHED if publish else ProductStatus.HIDDEN
        if product.status != target_status:
            product.status = target_status
            product.save(update_fields=["status", "updated_at"])
        return metadata
