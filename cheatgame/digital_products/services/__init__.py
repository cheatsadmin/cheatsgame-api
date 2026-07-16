"""Controlled Batch A Digital Products mutation boundary."""

from django.core.exceptions import PermissionDenied

from cheatgame.users.models import UserTypes


class DigitalProductsDomainError(ValueError):
    pass


class DigitalProductsValidationError(DigitalProductsDomainError):
    pass


class DigitalProductsConflictError(DigitalProductsDomainError):
    pass


class OfferTransitionError(DigitalProductsValidationError):
    pass


class StockIdempotencyConflictError(DigitalProductsConflictError):
    pass


class InsufficientStockError(DigitalProductsValidationError):
    pass


def require_manager_or_admin(actor) -> None:
    if (
        actor is None
        or not getattr(actor, "is_authenticated", False)
        or not getattr(actor, "is_active", False)
        or actor.user_type not in (UserTypes.MANAGER, UserTypes.ADMIN)
    ):
        raise PermissionDenied("Manager or Admin authority is required.")


def require_admin(actor) -> None:
    if (
        actor is None
        or not getattr(actor, "is_authenticated", False)
        or not getattr(actor, "is_active", False)
        or actor.user_type != UserTypes.ADMIN
    ):
        raise PermissionDenied("Admin authority is required.")
