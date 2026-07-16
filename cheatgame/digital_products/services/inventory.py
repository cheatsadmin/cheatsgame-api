from decimal import Decimal, InvalidOperation
from uuid import UUID

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.db.models import Sum

from cheatgame.digital_products.models import (
    DigitalInventoryReservation,
    DigitalInventoryReservationState,
    InventoryPool,
    PoolStockAdjustment,
    PoolStockAdjustmentReason,
)
from cheatgame.digital_products.services import (
    DigitalProductsValidationError,
    InsufficientStockError,
    StockIdempotencyConflictError,
    require_admin,
    require_manager_or_admin,
)


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


EFFECTIVE_RESERVATION_STATES = (
    DigitalInventoryReservationState.ACTIVE,
    DigitalInventoryReservationState.HELD_FOR_REVIEW,
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
