from django.db import IntegrityError, transaction

from cheatgame.digital_products.models import DigitalOffer, DigitalOfferSaleState, InventoryPoolStatus
from cheatgame.digital_products.services import (
    DigitalProductsConflictError,
    DigitalProductsValidationError,
    require_admin,
    require_manager_or_admin,
)
from cheatgame.product.models import (
    DeliveredVersion,
    NativeConsole,
    Product,
    ProductCommerceAuthority,
    ProductType,
)


def evaluate_product_readiness(product, *, for_deactivation=False):
    issues = []
    warnings = []
    if product.product_type != ProductType.GAME:
        issues.append("NOT_GAME")
    offers = list(
        DigitalOffer.objects.filter(delivered_version__product=product)
        .exclude(sale_state=DigitalOfferSaleState.ARCHIVED)
        .select_related("delivered_version", "inventory_pool")
    )
    if for_deactivation:
        if any(offer.sale_state == DigitalOfferSaleState.ACTIVE for offer in offers):
            issues.append("ACTIVE_OFFER")
    else:
        active_versions = list(product.delivered_versions.filter(is_active=True))
        if not active_versions:
            issues.append("NO_ACTIVE_VERSION")
        if not offers:
            issues.append("NO_OFFER")
        for offer in offers:
            if not offer.delivered_version.is_active or offer.inventory_pool.status == InventoryPoolStatus.ARCHIVED:
                issues.append("INVALID_OFFER")
                break
            try:
                offer.full_clean(exclude=("sale_state",))
            except Exception:
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
        if not readiness["ready"]:
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
