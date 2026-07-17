from django.db.models import Prefetch

from cheatgame.digital_products.models import (
    DigitalFulfillmentItem,
    FulfillmentActivity,
    FulfillmentActivityVisibility,
    InstalledGameRecord,
)


def digital_fulfillment_queryset():
    """Bounded traversal from mutable execution to immutable commercial truth."""
    return DigitalFulfillmentItem.objects.select_related(
        "assigned_operator",
        "obligation__order__user",
        "obligation__order_item__product",
        "obligation__checkout_line__digital_snapshot__delivered_version",
        "obligation__finalization__payment",
        "entitlement",
    ).prefetch_related(
        Prefetch("activities", queryset=FulfillmentActivity.objects.select_related("actor").order_by("created_at", "pk")),
        Prefetch("installed_games", queryset=InstalledGameRecord.objects.select_related("game", "delivered_version", "operator").order_by("created_at", "pk")),
    )


def admin_fulfillment_item(public_id):
    return digital_fulfillment_queryset().get(public_id=public_id)


def customer_fulfillment_items(customer):
    return digital_fulfillment_queryset().filter(obligation__order__user=customer).order_by("-created_at", "-pk")


def customer_fulfillment_item(*, public_id, customer):
    return digital_fulfillment_queryset().get(public_id=public_id, obligation__order__user=customer)


def customer_safe_activities(item):
    return item.activities.filter(visibility=FulfillmentActivityVisibility.CUSTOMER_SAFE).order_by("created_at", "pk")
