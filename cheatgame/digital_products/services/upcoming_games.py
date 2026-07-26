from django.db import transaction

from cheatgame.digital_products.models import (
    DigitalGameReleaseMetadata,
    DigitalGameUpcomingStatus,
    DigitalOfferSaleState,
)
from cheatgame.digital_products.services import (
    DigitalProductsValidationError,
    require_manager_or_admin,
)
from cheatgame.product.models import Product, ProductStatus, ProductType


UPCOMING_DISPLAY_STATUSES = {
    DigitalGameUpcomingStatus.ANNOUNCED,
    DigitalGameUpcomingStatus.COMING_SOON,
    DigitalGameUpcomingStatus.DELAYED,
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
    if preorder_enabled or upcoming_status == DigitalGameUpcomingStatus.PREORDER_OPEN:
        raise DigitalProductsValidationError(
            "Preorder purchasing is disabled until the commerce lifecycle supports it."
        )
    if (
        preorder_open_at
        and preorder_close_at
        and preorder_close_at <= preorder_open_at
    ):
        raise DigitalProductsValidationError(
            "Preorder close time must follow the open time."
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
        metadata.release_date = release_date
        metadata.upcoming_status = upcoming_status
        metadata.preorder_enabled = False
        metadata.preorder_open_at = preorder_open_at
        metadata.preorder_close_at = preorder_close_at
        metadata.save()

        target_status = ProductStatus.PUBLISHED if publish else ProductStatus.HIDDEN
        if product.status != target_status:
            product.status = target_status
            product.save(update_fields=["status", "updated_at"])
        return metadata
