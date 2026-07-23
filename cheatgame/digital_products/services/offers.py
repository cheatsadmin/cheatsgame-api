from decimal import Decimal, InvalidOperation
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from cheatgame.digital_products.models import (
    DigitalOffer,
    DigitalOfferCapacity,
    DigitalOfferSaleState,
    InventoryPool,
    InventoryPoolStatus,
    PoolStockAdjustmentReason,
)
from cheatgame.digital_products.services import (
    DigitalProductsConflictError,
    DigitalProductsValidationError,
    OfferTransitionError,
    require_admin,
    require_manager_or_admin,
)
from cheatgame.digital_products.services.inventory import adjust_pool_stock
from cheatgame.product.models import DeliveredVersion, NativeConsole, ProductCommerceAuthority, ProductType


ALLOWED_OFFER_TRANSITIONS = {
    DigitalOfferSaleState.DRAFT: {
        DigitalOfferSaleState.ACTIVE,
        DigitalOfferSaleState.HIDDEN,
        DigitalOfferSaleState.ARCHIVED,
    },
    DigitalOfferSaleState.ACTIVE: {
        DigitalOfferSaleState.PAUSED,
        DigitalOfferSaleState.HIDDEN,
        DigitalOfferSaleState.ARCHIVED,
    },
    DigitalOfferSaleState.PAUSED: {
        DigitalOfferSaleState.ACTIVE,
        DigitalOfferSaleState.HIDDEN,
        DigitalOfferSaleState.ARCHIVED,
    },
    DigitalOfferSaleState.HIDDEN: {
        DigitalOfferSaleState.DRAFT,
        DigitalOfferSaleState.ACTIVE,
        DigitalOfferSaleState.PAUSED,
        DigitalOfferSaleState.ARCHIVED,
    },
    DigitalOfferSaleState.ARCHIVED: set(),
}


def _normalize_price(value) -> Decimal:
    try:
        price = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DigitalProductsValidationError("Offer price is invalid.") from exc
    if not price.is_finite() or price != price.to_integral_value() or price < 0:
        raise DigitalProductsValidationError("Offer price must be a nonnegative whole amount.")
    return price


def _normalize_initial_stock(value) -> int:
    if isinstance(value, bool):
        raise DigitalProductsValidationError("Initial stock must be a nonnegative integer.")
    try:
        numeric = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DigitalProductsValidationError("Initial stock must be a nonnegative integer.") from exc
    if not numeric.is_finite() or numeric != numeric.to_integral_value() or numeric < 0:
        raise DigitalProductsValidationError("Initial stock must be a nonnegative integer.")
    return int(numeric)


def _get_delivered_version(delivered_version_id: int) -> DeliveredVersion:
    try:
        return DeliveredVersion.objects.select_related("product").get(pk=delivered_version_id)
    except DeliveredVersion.DoesNotExist as exc:
        raise DigitalProductsValidationError("Delivered version does not exist.") from exc


def _validate_offer_configuration(*, delivered_version, customer_console, capacity) -> None:
    if delivered_version.product.product_type != ProductType.GAME:
        raise DigitalProductsValidationError("Digital Offers require a GAME product.")
    if not delivered_version.is_active:
        raise DigitalProductsValidationError("Digital Offers require an active Delivered Version.")
    if customer_console not in NativeConsole.values:
        raise DigitalProductsValidationError("Customer console is invalid.")
    if capacity not in DigitalOfferCapacity.values:
        raise DigitalProductsValidationError("Capacity is invalid.")
    if customer_console == NativeConsole.PS4 and delivered_version.native_console != NativeConsole.PS4:
        raise DigitalProductsValidationError("A PS4 customer requires a PS4 delivered version.")


def create_digital_offer(
    *, delivered_version_id: int, customer_console: str, capacity: str, price, actor, initial_stock=0
):
    require_manager_or_admin(actor)
    normalized_price = _normalize_price(price)
    normalized_stock = _normalize_initial_stock(initial_stock)
    delivered_version = _get_delivered_version(delivered_version_id)
    _validate_offer_configuration(
        delivered_version=delivered_version,
        customer_console=customer_console,
        capacity=capacity,
    )
    try:
        with transaction.atomic():
            if DigitalOffer.objects.filter(
                delivered_version=delivered_version,
                customer_console=customer_console,
                capacity=capacity,
            ).exclude(sale_state=DigitalOfferSaleState.ARCHIVED).exists():
                raise DigitalProductsConflictError("A non-archived matching Offer already exists.")
            pool = InventoryPool.objects.create(status=InventoryPoolStatus.PAUSED)
            offer = DigitalOffer.objects.create(
                delivered_version=delivered_version,
                customer_console=customer_console,
                capacity=capacity,
                price=normalized_price,
                inventory_pool=pool,
                sale_state=DigitalOfferSaleState.DRAFT,
            )
            if normalized_stock:
                adjust_pool_stock(
                    pool_id=pool.id,
                    delta=normalized_stock,
                    reason=PoolStockAdjustmentReason.INVENTORY_RECEIVED,
                    actor=actor,
                    idempotency_key=uuid4(),
                )
                pool.refresh_from_db()
            return offer, pool
    except IntegrityError as exc:
        raise DigitalProductsConflictError("A non-archived matching Offer already exists.") from exc


def update_offer_price(*, offer_id: int, price, actor) -> DigitalOffer:
    require_manager_or_admin(actor)
    normalized_price = _normalize_price(price)
    with transaction.atomic():
        try:
            offer = DigitalOffer.objects.select_for_update().get(pk=offer_id)
        except DigitalOffer.DoesNotExist as exc:
            raise DigitalProductsValidationError("Digital Offer does not exist.") from exc
        offer.price = normalized_price
        offer.save(update_fields=["price", "updated_at"])
        return offer


def _validate_activation(offer: DigitalOffer) -> None:
    if offer.delivered_version.product.commerce_authority != ProductCommerceAuthority.DIGITAL_PRODUCTS:
        raise OfferTransitionError("Offer activation requires DIGITAL_PRODUCTS authority.")
    if not offer.delivered_version.is_active:
        raise OfferTransitionError("Delivered version must be active before Offer activation.")
    if offer.inventory_pool.status == InventoryPoolStatus.ARCHIVED:
        raise OfferTransitionError("An archived Inventory Pool cannot support an active Offer.")
    try:
        offer.full_clean()
    except ValidationError as exc:
        raise OfferTransitionError("Offer configuration is invalid.") from exc


def transition_offer_sale_state(*, offer_id: int, target_state: str, actor) -> DigitalOffer:
    require_manager_or_admin(actor)
    if target_state not in DigitalOfferSaleState.values:
        raise OfferTransitionError("Target Offer state is invalid.")
    with transaction.atomic():
        try:
            offer = DigitalOffer.objects.select_for_update().select_related(
                "delivered_version__product", "inventory_pool"
            ).get(pk=offer_id)
        except DigitalOffer.DoesNotExist as exc:
            raise DigitalProductsValidationError("Digital Offer does not exist.") from exc
        if offer.sale_state == target_state:
            return offer
        if target_state not in ALLOWED_OFFER_TRANSITIONS[offer.sale_state]:
            raise OfferTransitionError(f"Offer cannot transition from {offer.sale_state} to {target_state}.")
        if target_state == DigitalOfferSaleState.ACTIVE:
            _validate_activation(offer)
        offer.sale_state = target_state
        offer.save(update_fields=["sale_state", "updated_at"])
        return offer


def link_offer_to_shared_pool(*, offer_id: int, target_pool_id: int, actor) -> DigitalOffer:
    require_admin(actor)
    with transaction.atomic():
        try:
            offer = DigitalOffer.objects.select_for_update().select_related("delivered_version").get(pk=offer_id)
        except DigitalOffer.DoesNotExist as exc:
            raise DigitalProductsValidationError("Digital Offer does not exist.") from exc
        if offer.sale_state == DigitalOfferSaleState.ARCHIVED:
            raise DigitalProductsValidationError("Archived Offers cannot move between Pools.")
        pool_ids = sorted({offer.inventory_pool_id, target_pool_id})
        locked_pools = {
            pool.pk: pool
            for pool in InventoryPool.objects.select_for_update().filter(pk__in=pool_ids).order_by("pk")
        }
        if target_pool_id not in locked_pools:
            raise DigitalProductsValidationError("Target Inventory Pool does not exist.")
        if offer.inventory_pool_id == target_pool_id:
            return offer
        incompatible = DigitalOffer.objects.filter(inventory_pool_id=target_pool_id).exclude(
            delivered_version_id=offer.delivered_version_id,
            capacity=offer.capacity,
        ).exists()
        if incompatible:
            raise DigitalProductsValidationError("Shared Pools require the same Delivered Version and Capacity.")
        offer.inventory_pool_id = target_pool_id
        offer.save(update_fields=["inventory_pool", "updated_at"])
        return offer


def move_offer_to_new_independent_pool(*, offer_id: int, actor):
    require_admin(actor)
    with transaction.atomic():
        try:
            offer = DigitalOffer.objects.select_for_update().get(pk=offer_id)
        except DigitalOffer.DoesNotExist as exc:
            raise DigitalProductsValidationError("Digital Offer does not exist.") from exc
        if offer.sale_state == DigitalOfferSaleState.ARCHIVED:
            raise DigitalProductsValidationError("Archived Offers cannot move between Pools.")
        InventoryPool.objects.select_for_update().get(pk=offer.inventory_pool_id)
        new_pool = InventoryPool.objects.create(status=InventoryPoolStatus.PAUSED)
        offer.inventory_pool = new_pool
        offer.save(update_fields=["inventory_pool", "updated_at"])
        return offer, new_pool
