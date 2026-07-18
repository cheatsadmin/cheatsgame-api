from decimal import Decimal

from django.core.exceptions import ObjectDoesNotExist
from django.utils import timezone

from cheatgame.digital_products.models import DigitalInventoryReservationState
from cheatgame.digital_products.public_catalog import (
    CAPACITY_LABELS,
    CAPACITY_DISCLOSURES,
    COMPATIBILITY_DISCLOSURES,
    CONSOLE_LABELS,
    FULFILLMENT_METHOD_LABELS,
    PUBLIC_DIGITAL_CURRENCY,
)
from cheatgame.digital_products.services.checkout_preparation import COMMERCIAL_SNAPSHOT_REVISION
from cheatgame.product.models import ProductCommerceAuthority
from cheatgame.shop.models import CartState, CheckoutStatus


class DigitalCheckoutProjectionIntegrityError(ValueError):
    pass


def _money(value):
    return str(Decimal(value).quantize(Decimal("1")))


def _checkout_graph(checkout):
    lines = list(checkout.lines.all())
    reservations = list(checkout.digital_inventory_reservations.all())
    if not lines:
        raise DigitalCheckoutProjectionIntegrityError("Checkout has no immutable lines.")
    if any(line.commerce_authority != ProductCommerceAuthority.DIGITAL_PRODUCTS for line in lines):
        raise DigitalCheckoutProjectionIntegrityError("Checkout authority is incoherent.")
    if checkout.stock_reservations.all():
        raise DigitalCheckoutProjectionIntegrityError("Digital Checkout has Standard reservations.")
    if len(reservations) != len(lines):
        raise DigitalCheckoutProjectionIntegrityError("Checkout reservations are incomplete.")

    reservation_by_line = {reservation.checkout_line_id: reservation for reservation in reservations}
    if len(reservation_by_line) != len(reservations):
        raise DigitalCheckoutProjectionIntegrityError("Checkout reservations are duplicated.")

    rows = []
    revisions = set()
    for line in lines:
        try:
            snapshot = line.digital_snapshot
        except ObjectDoesNotExist as exc:
            raise DigitalCheckoutProjectionIntegrityError("Checkout snapshot is incomplete.") from exc
        reservation = reservation_by_line.get(line.pk)
        if reservation is None or reservation.inventory_pool_id != snapshot.inventory_pool_id:
            raise DigitalCheckoutProjectionIntegrityError("Checkout reservation ownership is incoherent.")
        revision = line.snapshot.get("commercial_revision")
        revisions.add(revision)
        rows.append((line, snapshot, reservation))
    if revisions != {COMMERCIAL_SNAPSHOT_REVISION}:
        raise DigitalCheckoutProjectionIntegrityError("Checkout commercial revision is incoherent.")
    return rows, COMMERCIAL_SNAPSHOT_REVISION


def digital_checkout_projection(checkout):
    rows, commercial_revision = _checkout_graph(checkout)
    now = timezone.now()
    expired_by_policy = checkout.expires_at <= now
    cart_lock_coherent = bool(
        checkout.cart_id
        and checkout.cart is not None
        and checkout.cart.state == CartState.LOCKED
        and checkout.cart.active_checkout_id == checkout.pk
    )
    reservations_active = all(
        reservation.state == DigitalInventoryReservationState.ACTIVE
        and reservation.expires_at == checkout.expires_at
        for _, _, reservation in rows
    )
    commercially_ready = bool(
        checkout.status == CheckoutStatus.CHECKOUT_DRAFT
        and not expired_by_policy
        and cart_lock_coherent
        and reservations_active
    )
    if commercially_ready:
        readiness_code = "READY"
    elif checkout.status == CheckoutStatus.EXPIRED or expired_by_policy:
        readiness_code = "CHECKOUT_EXPIRED"
    elif checkout.status != CheckoutStatus.CHECKOUT_DRAFT:
        readiness_code = "CHECKOUT_NOT_DRAFT"
    elif not cart_lock_coherent:
        readiness_code = "CART_LOCK_INCOHERENT"
    else:
        readiness_code = "RESERVATIONS_NOT_ACTIVE"

    line_rows = []
    subtotal = Decimal("0")
    for line, snapshot, _reservation in rows:
        subtotal += line.line_payable_total
        line_rows.append(
            {
                "offer_id": snapshot.offer_id,
                "game": {
                    "id": snapshot.product_id,
                    "slug": line.product_sku or "",
                    "title": snapshot.product_name,
                },
                "customer_console": snapshot.customer_console,
                "customer_console_label": CONSOLE_LABELS[snapshot.customer_console],
                "capacity": snapshot.capacity,
                "capacity_label": CAPACITY_LABELS[snapshot.capacity],
                "delivered_version_label": snapshot.version_label,
                "native_console": snapshot.native_console,
                "native_console_label": CONSOLE_LABELS[snapshot.native_console],
                "compatibility_code": snapshot.compatibility_disclosure,
                "compatibility_disclosure": COMPATIBILITY_DISCLOSURES[snapshot.compatibility_disclosure],
                "capacity_code": snapshot.capacity_disclosure,
                "capacity_disclosure": CAPACITY_DISCLOSURES[snapshot.capacity_disclosure],
                "fulfillment_method": {
                    "code": snapshot.fulfillment_method,
                    "label": FULFILLMENT_METHOD_LABELS[snapshot.fulfillment_method],
                },
                "unit_price": _money(snapshot.unit_price),
                "quantity": snapshot.quantity,
                "line_total": _money(snapshot.line_total),
                "currency": PUBLIC_DIGITAL_CURRENCY,
            }
        )

    return {
        "public_id": str(checkout.public_id),
        "status": checkout.status,
        "commerce_authority": "DIGITAL_PRODUCTS",
        "commercial_revision": commercial_revision,
        "created_at": checkout.created_at,
        "expires_at": checkout.expires_at,
        "maximum_expires_at": checkout.maximum_expires_at,
        "is_commercially_ready": commercially_ready,
        "is_payment_ready": commercially_ready,
        "readiness_code": readiness_code,
        "can_cancel": checkout.status == CheckoutStatus.CHECKOUT_DRAFT and not expired_by_policy,
        "currency": PUBLIC_DIGITAL_CURRENCY,
        "lines": line_rows,
        "totals": {
            "subtotal": _money(subtotal),
            "discount": "0",
            "total": _money(subtotal),
        },
    }
