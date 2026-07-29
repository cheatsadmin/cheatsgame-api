from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.utils import timezone

from cheatgame.digital_products.models import (
    DigitalInventoryReservation,
    DigitalInventoryReservationState,
)
from cheatgame.digital_products.services.reservations import (
    DigitalReservationCardinalityError,
    classify_digital_reservations,
)
from cheatgame.financial_core.models import (
    CommercialFinalization,
    DigitalFulfillmentObligation,
    FinancialAllocation,
    PaymentAttemptStatus,
    PaymentCollectionStatus,
    PaymentTransactionStatus,
)
from cheatgame.financial_core.services.locks import LockRank, lock_many
from cheatgame.shop.models import (
    CartLockReason,
    CartState,
    CheckoutStatus,
    CommerceActorType,
    CommerceEvent,
    CommerceEventType,
    OrderStatus,
)
from cheatgame.shop.services.commerce_foundation import append_commerce_event


DEFINITIVE_UNPAID_TRANSACTION_STATES = frozenset(
    {
        PaymentTransactionStatus.DECLINED,
        PaymentTransactionStatus.CANCELED,
        PaymentTransactionStatus.EXPIRED,
    }
)


class DefinitiveDigitalPaymentFailureConflict(ValidationError):
    pass


@dataclass(frozen=True)
class DefinitiveDigitalPaymentFailureResult:
    released_reservation_ids: tuple[int, ...]
    replayed: bool


def _event_once(*, checkout, order, event_type, reference, metadata):
    existing = CommerceEvent.objects.filter(
        checkout=checkout,
        event_type=event_type,
        idempotency_reference=reference,
    ).first()
    if existing is not None:
        return existing
    return append_commerce_event(
        checkout=checkout,
        order=order,
        event_type=event_type,
        actor_type=CommerceActorType.SYSTEM,
        idempotency_reference=reference,
        correlation_id=str(order.public_tracking_code or ""),
        metadata=metadata,
    )


def terminate_locked_definitive_unpaid_digital_graph(
    *,
    cart,
    checkout,
    order,
    payment,
    attempt,
    transaction_obj,
    reason_code,
    idempotency_identity,
    locked_reservations=None,
):
    """
    Terminate one already-locked Digital commercial attempt.

    The caller owns an atomic transaction and must lock Cart -> Checkout ->
    Order -> Payment -> Attempt -> Transaction before entering this service.
    Financial terminal evidence remains immutable; only the commercial hold
    and ownership projection are terminated.
    """
    if order.checkout_id != checkout.pk or checkout.cart_id != cart.pk or payment.order_id != order.pk:
        raise DefinitiveDigitalPaymentFailureConflict("Definitive-failure graph ownership is inconsistent.")
    if payment.collection_status != PaymentCollectionStatus.OPEN:
        raise DefinitiveDigitalPaymentFailureConflict("Definitive unpaid termination requires an open Payment.")
    if (
        payment.confirmed_amount
        or FinancialAllocation.objects.filter(payment=payment).exists()
        or CommercialFinalization.objects.filter(payment=payment).exists()
        or DigitalFulfillmentObligation.objects.filter(finalization__payment=payment).exists()
    ):
        raise DefinitiveDigitalPaymentFailureConflict("Recognized funds cannot use definitive-unpaid termination.")
    if attempt.payment_id != payment.pk or attempt.status != PaymentAttemptStatus.DEFINITIVE_FAILED:
        raise DefinitiveDigitalPaymentFailureConflict("PaymentAttempt is not authoritatively definitive-failed.")
    if (
        transaction_obj.attempt_id != attempt.pk
        or transaction_obj.status not in DEFINITIVE_UNPAID_TRANSACTION_STATES
    ):
        raise DefinitiveDigitalPaymentFailureConflict("PaymentTransaction is not authoritatively terminal unpaid.")

    if locked_reservations is None:
        reservation_ids = DigitalInventoryReservation.objects.filter(order=order).values_list("pk", flat=True)
        reservations = lock_many(
            queryset=DigitalInventoryReservation.objects.all(),
            rank=LockRank.RESERVATION,
            pks=reservation_ids,
        )
    else:
        reservations = list(locked_reservations)
        if any(reservation.order_id != order.pk for reservation in reservations):
            raise DefinitiveDigitalPaymentFailureConflict(
                "Prelocked reservation ownership is inconsistent."
            )
    if not reservations:
        return DefinitiveDigitalPaymentFailureResult((), False)
    try:
        lineage = classify_digital_reservations(reservations)
    except DigitalReservationCardinalityError as exc:
        raise DefinitiveDigitalPaymentFailureConflict(str(exc)) from exc

    line_ids = set(checkout.lines.values_list("pk", flat=True))
    if set(lineage.by_line) != line_ids:
        raise DefinitiveDigitalPaymentFailureConflict("Digital reservation coverage is incomplete.")

    current = tuple(lineage.current_by_line.values())
    replayed = (
        checkout.status == CheckoutStatus.CANCELED
        and order.payment_status == OrderStatus.FAIDED.value
        and cart.state == CartState.OPEN
        and cart.active_checkout_id is None
        and not current
    )
    if replayed:
        return DefinitiveDigitalPaymentFailureResult((), True)
    if checkout.status != CheckoutStatus.PENDING_PAYMENT:
        raise DefinitiveDigitalPaymentFailureConflict("Checkout is outside definitive-unpaid termination.")
    if (
        cart.state != CartState.LOCKED
        or cart.active_checkout_id != checkout.pk
        or cart.lock_reason not in (CartLockReason.CHECKOUT_IN_PROGRESS, CartLockReason.PAYMENT_IN_PROGRESS)
    ):
        raise DefinitiveDigitalPaymentFailureConflict("Cart is not owned by the failed Checkout.")
    if set(lineage.current_by_line) != line_ids or any(
        reservation.state != DigitalInventoryReservationState.PAYMENT_HOLD for reservation in current
    ):
        raise DefinitiveDigitalPaymentFailureConflict(
            "Every Checkout line requires one authoritative PAYMENT_HOLD."
        )

    now = timezone.now()
    released_ids = tuple(sorted(reservation.pk for reservation in current))
    for reservation in current:
        reservation.state = DigitalInventoryReservationState.RELEASED
        reservation.state_changed_at = now
        reservation.resolution_reason = "definitive_unpaid_failure"
        reservation.save(
            update_fields=("state", "state_changed_at", "resolution_reason", "updated_at")
        )

    checkout.status = CheckoutStatus.CANCELED
    checkout.canceled_at = now
    checkout.version += 1
    checkout.save(update_fields=("status", "canceled_at", "version", "updated_at"))

    order.payment_status = OrderStatus.FAIDED.value
    order.save(update_fields=("payment_status", "updated_at"))

    cart.state = CartState.OPEN
    cart.lock_reason = None
    cart.active_checkout = None
    cart.locked_at = None
    cart.lock_version += 1
    cart.save(
        update_fields=(
            "state",
            "lock_reason",
            "active_checkout",
            "locked_at",
            "lock_version",
            "updated_at",
        )
    )

    reference = f"definitive-unpaid:{idempotency_identity}"
    common = {"reason_code": str(reason_code)[:64], "payment_id": str(payment.public_id)}
    _event_once(
        checkout=checkout,
        order=order,
        event_type=CommerceEventType.PAYMENT_FAILED,
        reference=reference,
        metadata=common,
    )
    _event_once(
        checkout=checkout,
        order=order,
        event_type=CommerceEventType.STOCK_RESERVATION_RELEASED,
        reference=reference,
        metadata={**common, "reservation_count": len(released_ids)},
    )
    _event_once(
        checkout=checkout,
        order=order,
        event_type=CommerceEventType.CHECKOUT_CANCELED,
        reference=reference,
        metadata={**common, "new_status": CheckoutStatus.CANCELED},
    )
    _event_once(
        checkout=checkout,
        order=order,
        event_type=CommerceEventType.CART_UNLOCKED,
        reference=reference,
        metadata={**common, "new_status": CartState.OPEN},
    )
    return DefinitiveDigitalPaymentFailureResult(released_ids, False)
