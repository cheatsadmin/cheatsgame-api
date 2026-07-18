from django.db.models import Prefetch

from cheatgame.digital_products.models import DigitalInventoryReservation
from cheatgame.product.models import ProductCommerceAuthority
from cheatgame.shop.models import Checkout, CheckoutLine


def customer_digital_checkout_queryset(*, user):
    lines = CheckoutLine.objects.order_by("pk").select_related(
        "digital_snapshot",
        "digital_snapshot__delivered_version",
    )
    reservations = DigitalInventoryReservation.objects.order_by("checkout_line_id")
    return (
        Checkout.objects.filter(user=user)
        .filter(lines__commerce_authority=ProductCommerceAuthority.DIGITAL_PRODUCTS)
        .select_related("cart")
        .prefetch_related(
            Prefetch("lines", queryset=lines),
            Prefetch("digital_inventory_reservations", queryset=reservations),
            "stock_reservations",
        )
        .distinct()
    )


def active_customer_digital_checkout(*, user):
    return (
        customer_digital_checkout_queryset(user=user)
        .filter(status__in=Checkout.ACTIVE_STATUSES)
        .order_by("-created_at", "-pk")
        .first()
    )


def owned_customer_digital_checkout(*, user, public_id):
    return customer_digital_checkout_queryset(user=user).filter(public_id=public_id).first()
