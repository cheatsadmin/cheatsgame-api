"""Controlled Digital Products mutation boundary through Batch B."""

from django.core.exceptions import PermissionDenied

from cheatgame.users.models import UserTypes


class DigitalProductsDomainError(ValueError):
    code = "DIGITAL_PRODUCTS_ERROR"

    def __init__(self, message, *, details=None):
        self.message = message
        self.details = details or {}
        super().__init__(message)


class DigitalProductsValidationError(DigitalProductsDomainError):
    code = "DIGITAL_PRODUCTS_VALIDATION_ERROR"


class DigitalProductsConflictError(DigitalProductsDomainError):
    code = "DIGITAL_PRODUCTS_CONFLICT"


class MixedCommerceAuthorityError(DigitalProductsConflictError):
    code = "MIXED_COMMERCE_AUTHORITY_NOT_SUPPORTED"


class StandardCartNotSupportedError(DigitalProductsConflictError):
    code = "STANDARD_CART_REQUIRES_STANDARD_CHECKOUT"


class DigitalCartLockedError(DigitalProductsConflictError):
    code = "DIGITAL_CART_LOCKED"


class DigitalCartStaleError(DigitalProductsConflictError):
    code = "DIGITAL_CART_STALE"


class DigitalCheckoutExpiredError(DigitalProductsConflictError):
    code = "DIGITAL_CHECKOUT_EXPIRED"


class DigitalOfferUnavailableError(DigitalProductsValidationError):
    code = "DIGITAL_OFFER_UNAVAILABLE"


class DigitalReservationConflictError(DigitalProductsConflictError):
    code = "DIGITAL_RESERVATION_CONFLICT"


class InsufficientDigitalAvailabilityError(DigitalProductsValidationError):
    code = "DIGITAL_AVAILABILITY_UNAVAILABLE"


class EmptyDigitalCartError(DigitalProductsValidationError):
    code = "DIGITAL_CART_EMPTY"


class DigitalCheckoutIntegrityError(DigitalProductsConflictError):
    code = "CHECKOUT_AUTHORITY_INCOHERENT"


class DigitalCheckoutIdempotencyError(DigitalProductsConflictError):
    code = "IDEMPOTENCY_CONFLICT"


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
