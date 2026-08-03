import logging
from decimal import Decimal, InvalidOperation
from uuid import UUID

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Sum

from cheatgame.digital_products.models import (
    DigitalGameUpcomingStatus,
    DigitalInventoryReservation,
    DigitalOffer,
    DigitalOfferCapacity,
    DigitalOfferSaleState,
    InventoryPool,
    InventoryPoolStatus,
    PoolStockAdjustment,
    PoolStockAdjustmentReason,
)
from cheatgame.digital_products.services.reservations import (
    CURRENT_DIGITAL_RESERVATION_STATES,
)
from cheatgame.digital_products.services import (
    DigitalProductsValidationError,
    InsufficientStockError,
    InventoryPoolTransitionError,
    StockIdempotencyConflictError,
    require_admin,
    require_manager_or_admin,
)
from cheatgame.product.models import (
    NativeConsole,
    ProductCommerceAuthority,
    ProductStatus,
    ProductType,
)


logger = logging.getLogger(__name__)


def _normalize_delta(value) -> int:
    if isinstance(value, bool):
        raise DigitalProductsValidationError("Stock delta must be a nonzero integer.")
    try:
        numeric = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DigitalProductsValidationError("Stock delta must be a nonzero integer.") from exc
    if not numeric.is_finite() or numeric != numeric.to_integral_value() or numeric == 0:
        raise DigitalProductsValidationError("Stock delta must be a nonzero integer.")
    return int(numeric)


def _normalize_idempotency_key(value) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise DigitalProductsValidationError("A valid stock idempotency UUID is required.") from exc


def _validate_reason(reason: str, actor) -> None:
    if reason not in PoolStockAdjustmentReason.values:
        raise DigitalProductsValidationError("Stock adjustment reason is invalid.")
    if reason == PoolStockAdjustmentReason.RECONCILIATION:
        try:
            require_admin(actor)
        except PermissionDenied as exc:
            raise PermissionDenied("Reconciliation adjustments require Admin authority.") from exc


def _resolve_existing_adjustment(*, adjustment, pool_id, delta, reason, actor_id):
    if (
        adjustment.inventory_pool_id == pool_id
        and adjustment.delta == delta
        and adjustment.reason == reason
        and adjustment.actor_id == actor_id
    ):
        return adjustment, adjustment.resulting_quantity
    raise StockIdempotencyConflictError("Stock idempotency key was reused with different command semantics.")


EFFECTIVE_RESERVATION_STATES = CURRENT_DIGITAL_RESERVATION_STATES


def _validate_inventory_pool_activation(
    *,
    offer: DigitalOffer,
    pool: InventoryPool,
    validate_model: bool = True,
) -> None:
    if pool.status == InventoryPoolStatus.ARCHIVED:
        raise InventoryPoolTransitionError("Archived Inventory Pools cannot be enabled.")
    if offer.sale_state != DigitalOfferSaleState.ACTIVE:
        raise InventoryPoolTransitionError("Inventory can be enabled only for an active Digital Offer.")
    if offer.delivered_version.product.product_type != ProductType.GAME:
        raise InventoryPoolTransitionError("Inventory activation requires a GAME product.")
    if (
        offer.delivered_version.product.commerce_authority
        != ProductCommerceAuthority.DIGITAL_PRODUCTS
    ):
        raise InventoryPoolTransitionError("Inventory activation requires DIGITAL_PRODUCTS authority.")
    if offer.delivered_version.product.status != ProductStatus.PUBLISHED:
        raise InventoryPoolTransitionError("Inventory activation requires a published game.")
    if not offer.delivered_version.is_active:
        raise InventoryPoolTransitionError("Inventory activation requires an active Delivered Version.")
    if offer.customer_console not in NativeConsole.values:
        raise InventoryPoolTransitionError("Inventory activation requires a valid customer Console.")
    if offer.capacity not in DigitalOfferCapacity.values:
        raise InventoryPoolTransitionError("Inventory activation requires a valid Capacity.")
    if (
        offer.customer_console == NativeConsole.PS4
        and offer.delivered_version.native_console != NativeConsole.PS4
    ):
        raise InventoryPoolTransitionError("A PS4 customer requires a PS4 Delivered Version.")
    if offer.price <= 0:
        raise InventoryPoolTransitionError("Inventory activation requires a positive authoritative price.")
    if pool.sellable_quantity < 0:
        raise InventoryPoolTransitionError("Inventory quantity cannot be negative.")
    release_metadata = getattr(
        offer.delivered_version.product,
        "digital_release_metadata",
        None,
    )
    if (
        release_metadata is not None
        and release_metadata.upcoming_status
        not in (
            DigitalGameUpcomingStatus.PREORDER_OPEN,
            DigitalGameUpcomingStatus.RELEASED,
        )
    ):
        raise InventoryPoolTransitionError(
            "Only PREORDER_OPEN or RELEASED games can enable purchasable inventory."
        )
    if not validate_model:
        return
    try:
        offer.full_clean()
    except ValidationError as exc:
        raise InventoryPoolTransitionError("Digital Offer configuration is invalid.") from exc


def inventory_pool_allowed_actions(*, offer: DigitalOffer, actor) -> list[str]:
    try:
        require_admin(actor)
    except PermissionDenied:
        return []
    pool = offer.inventory_pool
    if offer.sale_state == DigitalOfferSaleState.ARCHIVED or pool.status == InventoryPoolStatus.ARCHIVED:
        return []
    if pool.status == InventoryPoolStatus.ENABLED:
        return ["pause_inventory"]
    if pool.status != InventoryPoolStatus.PAUSED:
        return []
    try:
        _validate_inventory_pool_activation(
            offer=offer,
            pool=pool,
            validate_model=False,
        )
    except InventoryPoolTransitionError:
        return []
    return ["enable_inventory"]


def _transition_inventory_pool(*, offer_id: int, target_status: str, actor) -> InventoryPool:
    require_admin(actor)
    if target_status not in (InventoryPoolStatus.ENABLED, InventoryPoolStatus.PAUSED):
        raise InventoryPoolTransitionError("Target Inventory Pool state is invalid.")

    with transaction.atomic():
        try:
            offer = (
                DigitalOffer.objects.select_for_update()
                .select_related("delivered_version__product", "inventory_pool")
                .get(pk=offer_id)
            )
        except DigitalOffer.DoesNotExist as exc:
            raise DigitalProductsValidationError("Digital Offer does not exist.") from exc

        pool = InventoryPool.objects.select_for_update().get(pk=offer.inventory_pool_id)
        if pool.status == target_status:
            return pool
        if pool.status == InventoryPoolStatus.ARCHIVED:
            raise InventoryPoolTransitionError("Archived Inventory Pools cannot change sale availability.")
        if target_status == InventoryPoolStatus.ENABLED:
            _validate_inventory_pool_activation(offer=offer, pool=pool)

        previous_status = pool.status
        pool.status = target_status
        pool.save(update_fields=["status", "updated_at"])
        logger.info(
            "digital_inventory_pool_status_changed",
            extra={
                "actor_id": actor.pk,
                "digital_offer_id": offer.pk,
                "inventory_pool_id": pool.pk,
                "previous_status": previous_status,
                "target_status": target_status,
            },
        )
        return pool


def enable_inventory_pool(*, offer_id: int, actor) -> InventoryPool:
    return _transition_inventory_pool(
        offer_id=offer_id,
        target_status=InventoryPoolStatus.ENABLED,
        actor=actor,
    )


def pause_inventory_pool(*, offer_id: int, actor) -> InventoryPool:
    return _transition_inventory_pool(
        offer_id=offer_id,
        target_status=InventoryPoolStatus.PAUSED,
        actor=actor,
    )


def get_effective_held_quantity(*, pool_id: int) -> int:
    return (
        DigitalInventoryReservation.objects.filter(
            inventory_pool_id=pool_id,
            state__in=EFFECTIVE_RESERVATION_STATES,
        ).aggregate(total=Sum("quantity"))["total"]
        or 0
    )


def get_available_quantity(*, pool_id: int) -> int:
    """Available Digital stock is Pool total minus effective reservations."""
    try:
        total = InventoryPool.objects.values_list("sellable_quantity", flat=True).get(pk=pool_id)
    except InventoryPool.DoesNotExist as exc:
        raise DigitalProductsValidationError("Inventory Pool does not exist.") from exc
    return max(total - get_effective_held_quantity(pool_id=pool_id), 0)


def adjust_pool_stock(*, pool_id: int, delta, reason: str, actor, idempotency_key):
    require_manager_or_admin(actor)
    normalized_delta = _normalize_delta(delta)
    normalized_key = _normalize_idempotency_key(idempotency_key)
    _validate_reason(reason, actor)

    existing = PoolStockAdjustment.objects.filter(idempotency_key=normalized_key).first()
    if existing is not None:
        return _resolve_existing_adjustment(
            adjustment=existing,
            pool_id=pool_id,
            delta=normalized_delta,
            reason=reason,
            actor_id=actor.pk,
        )

    try:
        with transaction.atomic():
            try:
                pool = InventoryPool.objects.select_for_update().get(pk=pool_id)
            except InventoryPool.DoesNotExist as exc:
                raise DigitalProductsValidationError("Inventory Pool does not exist.") from exc
            existing = PoolStockAdjustment.objects.filter(idempotency_key=normalized_key).first()
            if existing is not None:
                return _resolve_existing_adjustment(
                    adjustment=existing,
                    pool_id=pool_id,
                    delta=normalized_delta,
                    reason=reason,
                    actor_id=actor.pk,
                )
            previous_quantity = pool.sellable_quantity
            resulting_quantity = previous_quantity + normalized_delta
            if resulting_quantity < 0:
                raise InsufficientStockError("Stock adjustment would make Pool quantity negative.")
            held_quantity = get_effective_held_quantity(pool_id=pool.id)
            if resulting_quantity < held_quantity:
                raise InsufficientStockError(
                    "Stock adjustment would reduce Pool quantity below active reservations."
                )
            pool.sellable_quantity = resulting_quantity
            pool.save(update_fields=["sellable_quantity", "updated_at"])
            adjustment = PoolStockAdjustment.objects.create(
                inventory_pool=pool,
                delta=normalized_delta,
                previous_quantity=previous_quantity,
                resulting_quantity=resulting_quantity,
                reason=reason,
                actor=actor,
                idempotency_key=normalized_key,
            )
            return adjustment, resulting_quantity - held_quantity
    except IntegrityError:
        existing = PoolStockAdjustment.objects.filter(idempotency_key=normalized_key).first()
        if existing is None:
            raise
        return _resolve_existing_adjustment(
            adjustment=existing,
            pool_id=pool_id,
            delta=normalized_delta,
            reason=reason,
            actor_id=actor.pk,
        )
