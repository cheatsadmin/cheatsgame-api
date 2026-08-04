from django.db import IntegrityError, transaction

from cheatgame.digital_products.models import (
    DigitalGameUpcomingStatus,
    DigitalOffer,
    DigitalOfferCapacity,
    DigitalOfferSaleState,
    InventoryPoolStatus,
)
from cheatgame.digital_products.services import (
    DigitalProductsConflictError,
    DigitalProductsValidationError,
    require_admin,
    require_manager_or_admin,
)
from cheatgame.digital_products.services.upcoming_games import (
    evaluate_upcoming_readiness,
)
from cheatgame.product.models import (
    DeliveredVersion,
    NativeConsole,
    Product,
    ProductCommerceAuthority,
    ProductType,
)


def _prefetched_delivered_versions(product):
    cache = getattr(product, "_prefetched_objects_cache", {})
    if "delivered_versions" not in cache:
        return None
    return list(cache["delivered_versions"])


def _prefetched_offers(product):
    versions = _prefetched_delivered_versions(product)
    if versions is None:
        return None
    offers = []
    for version in versions:
        cache = getattr(version, "_prefetched_objects_cache", {})
        if "digital_offers" not in cache:
            return None
        offers.extend(cache["digital_offers"])
    return [
        offer
        for offer in offers
        if offer.sale_state != DigitalOfferSaleState.ARCHIVED
    ]


def _offer_is_coherent_without_database_validation(offer):
    """Mirror DigitalOffer.clean() using the already-prefetched graph.

    This is intentionally limited to read-only Admin projections. Mutation
    services continue to use full model validation as the authority.
    """
    version = offer.delivered_version
    product = version.product
    metadata = getattr(product, "digital_release_metadata", None)
    if offer.customer_console not in NativeConsole.values:
        return False
    if offer.capacity not in DigitalOfferCapacity.values:
        return False
    if offer.price < 0:
        return False
    if not version.is_active and offer.sale_state == DigitalOfferSaleState.ACTIVE:
        return False
    if (
        offer.customer_console == NativeConsole.PS4
        and version.native_console != NativeConsole.PS4
    ):
        return False
    if (
        offer.sale_state == DigitalOfferSaleState.ACTIVE
        and product.commerce_authority
        != ProductCommerceAuthority.DIGITAL_PRODUCTS
    ):
        return False
    if (
        offer.sale_state == DigitalOfferSaleState.ACTIVE
        and metadata is not None
        and metadata.upcoming_status not in (
            DigitalGameUpcomingStatus.PREORDER_OPEN,
            DigitalGameUpcomingStatus.RELEASED,
        )
    ):
        return False
    return True


def evaluate_product_readiness(
    product,
    *,
    for_deactivation=False,
    use_prefetched=False,
    validate_offer_models=True,
):
    issues = []
    warnings = []
    if product.product_type != ProductType.GAME:
        issues.append("NOT_GAME")
    offers = _prefetched_offers(product) if use_prefetched else None
    if offers is None:
        offers = list(
            DigitalOffer.objects.filter(delivered_version__product=product)
            .exclude(sale_state=DigitalOfferSaleState.ARCHIVED)
            .select_related(
                "delivered_version__product__digital_release_metadata",
                "inventory_pool",
            )
        )
    if for_deactivation:
        if any(offer.sale_state == DigitalOfferSaleState.ACTIVE for offer in offers):
            issues.append("ACTIVE_OFFER")
    else:
        versions = _prefetched_delivered_versions(product) if use_prefetched else None
        active_versions = (
            [version for version in versions if version.is_active]
            if versions is not None
            else list(product.delivered_versions.filter(is_active=True))
        )
        if not active_versions:
            issues.append("NO_ACTIVE_VERSION")
        if not offers:
            issues.append("NO_OFFER")
        for offer in offers:
            if not offer.delivered_version.is_active or offer.inventory_pool.status == InventoryPoolStatus.ARCHIVED:
                issues.append("INVALID_OFFER")
                break
            if validate_offer_models:
                try:
                    offer.full_clean(exclude=("sale_state",))
                except Exception:
                    issues.append("INVALID_OFFER")
                    break
            elif not _offer_is_coherent_without_database_validation(offer):
                issues.append("INVALID_OFFER")
                break
        for pool_id in {offer.inventory_pool_id for offer in offers}:
            shared = [offer for offer in offers if offer.inventory_pool_id == pool_id]
            if len({(offer.delivered_version_id, offer.capacity) for offer in shared}) > 1:
                issues.append("INCOMPATIBLE_SHARED_POOL")
                break
    if product.quantity:
        warnings.append("LEGACY_QUANTITY_IGNORED")
    return {"ready": not issues, "issues": issues, "warnings": warnings}


def create_delivered_version(*, product_id, native_console, actor):
    require_manager_or_admin(actor)
    if native_console not in NativeConsole.values:
        raise DigitalProductsValidationError("Native console is invalid.")
    try:
        with transaction.atomic():
            product = Product.objects.select_for_update().get(pk=product_id)
            if product.product_type != ProductType.GAME:
                raise DigitalProductsValidationError("Delivered versions require a GAME product.")
            return DeliveredVersion.objects.create(product=product, native_console=native_console)
    except Product.DoesNotExist as exc:
        raise DigitalProductsValidationError("Product does not exist.") from exc
    except IntegrityError as exc:
        raise DigitalProductsConflictError("An active matching Delivered Version already exists.") from exc


def archive_delivered_version(*, version_id, actor):
    require_manager_or_admin(actor)
    with transaction.atomic():
        try:
            version = DeliveredVersion.objects.select_for_update().get(pk=version_id)
        except DeliveredVersion.DoesNotExist as exc:
            raise DigitalProductsValidationError("Delivered Version does not exist.") from exc
        if version.digital_offers.exclude(sale_state=DigitalOfferSaleState.ARCHIVED).exists():
            raise DigitalProductsConflictError("Archive dependent Offers before this Delivered Version.")
        if version.is_active:
            version.is_active = False
            version.save(update_fields=["is_active", "updated_at"])
        return version


def activate_digital_product(*, product_id, actor):
    require_admin(actor)
    with transaction.atomic():
        try:
            product = Product.objects.select_for_update().get(pk=product_id)
        except Product.DoesNotExist as exc:
            raise DigitalProductsValidationError("Product does not exist.") from exc
        if product.commerce_authority == ProductCommerceAuthority.DIGITAL_PRODUCTS:
            return product
        readiness = evaluate_product_readiness(product)
        upcoming_readiness = evaluate_upcoming_readiness(product)
        if (
            not readiness["ready"]
            and not upcoming_readiness["ready_for_authority"]
        ):
            error = DigitalProductsConflictError("Product is not ready for Digital Products authority.")
            error.readiness = readiness
            raise error
        product.commerce_authority = ProductCommerceAuthority.DIGITAL_PRODUCTS
        product.save(update_fields=["commerce_authority", "updated_at"])
        return product


def deactivate_digital_product(*, product_id, actor):
    require_admin(actor)
    with transaction.atomic():
        try:
            product = Product.objects.select_for_update().get(pk=product_id)
        except Product.DoesNotExist as exc:
            raise DigitalProductsValidationError("Product does not exist.") from exc
        if product.commerce_authority == ProductCommerceAuthority.STANDARD_COMMERCE:
            return product
        readiness = evaluate_product_readiness(product, for_deactivation=True)
        if not readiness["ready"]:
            error = DigitalProductsConflictError("Product cannot return to Standard Commerce.")
            error.readiness = readiness
            raise error
        product.commerce_authority = ProductCommerceAuthority.STANDARD_COMMERCE
        product.save(update_fields=["commerce_authority", "updated_at"])
        return product
