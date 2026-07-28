from dataclasses import dataclass

from cheatgame.digital_products.models import DigitalInventoryReservationState


CURRENT_DIGITAL_RESERVATION_STATES = frozenset(
    {
        DigitalInventoryReservationState.ACTIVE,
        DigitalInventoryReservationState.PAYMENT_HOLD,
        DigitalInventoryReservationState.HELD_FOR_REVIEW,
    }
)


class DigitalReservationCardinalityError(ValueError):
    pass


@dataclass(frozen=True)
class DigitalReservationLineage:
    by_line: dict
    current_by_line: dict
    original_by_line: dict
    consumed_by_line: dict


def classify_digital_reservations(reservations):
    """Classify already-loaded rows without choosing among conflicting authority."""
    by_line = {}
    current_by_line = {}
    original_by_line = {}
    consumed_by_line = {}

    for reservation in reservations:
        line_id = reservation.checkout_line_id
        by_line.setdefault(line_id, []).append(reservation)

        if reservation.recovery_authorization_id is None:
            if line_id in original_by_line:
                raise DigitalReservationCardinalityError(
                    "A Checkout line has more than one original Digital reservation."
                )
            original_by_line[line_id] = reservation

        if reservation.state in CURRENT_DIGITAL_RESERVATION_STATES:
            if line_id in current_by_line:
                raise DigitalReservationCardinalityError(
                    "A Checkout line has more than one current Digital reservation."
                )
            current_by_line[line_id] = reservation

        if reservation.state == DigitalInventoryReservationState.CONSUMED:
            if line_id in consumed_by_line:
                raise DigitalReservationCardinalityError(
                    "A Checkout line has more than one consumed Digital reservation."
                )
            consumed_by_line[line_id] = reservation

    return DigitalReservationLineage(
        by_line=by_line,
        current_by_line=current_by_line,
        original_by_line=original_by_line,
        consumed_by_line=consumed_by_line,
    )
