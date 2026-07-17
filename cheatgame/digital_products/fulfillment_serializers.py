from rest_framework import serializers

from cheatgame.digital_products.models import (
    DigitalCartFulfillmentMethod,
    DigitalFulfillmentStatus,
    FulfillmentActivityVisibility,
    InstalledGameClassification,
)
from cheatgame.users.models import UserTypes


def _selection(item):
    snapshot = item.obligation.checkout_line.digital_snapshot
    return {
        "game": {"id": snapshot.product_id, "title": snapshot.product_name},
        "customer_console": snapshot.customer_console,
        "capacity": snapshot.capacity,
        "delivered_version_label": snapshot.version_label,
        "native_console": snapshot.native_console,
        "compatibility_disclosure": snapshot.compatibility_disclosure,
        "capacity_disclosure": snapshot.capacity_disclosure,
        "purchased_fulfillment_method": item.obligation.fulfillment_method,
        "current_fulfillment_method": item.current_fulfillment_method,
    }


def _installed(item, *, include_audit):
    records = list(item.installed_games.all())
    superseded_ids = {record.corrects_id for record in records if record.corrects_id}
    if not include_audit:
        records = [
            record for record in records
            if record.pk not in superseded_ids and record.state == "recorded"
        ]
    return [{
        "classification": record.classification,
        "game": ({"id": record.game_id, "title": record.game.title} if record.game_id else None),
        "fallback_title": record.fallback_title,
        "delivered_version": record.delivered_version.native_console if record.delivered_version_id else None,
        "state": record.state,
        "is_current": record.pk not in superseded_ids,
        "installed_at": record.installed_at,
    } for record in records]


def _timeline(item, *, customer_safe):
    activities = item.activities.all()
    if customer_safe:
        activities = [activity for activity in activities if activity.visibility == FulfillmentActivityVisibility.CUSTOMER_SAFE]
    return [{
        "type": activity.activity_type,
        "status": activity.new_status,
        "waiting_reason": activity.waiting_reason,
        "note": activity.note if (not customer_safe or activity.visibility == FulfillmentActivityVisibility.CUSTOMER_SAFE) else "",
        "created_at": activity.created_at,
    } for activity in activities]


def _allowed_actions(item, actor=None):
    if item.status == DigitalFulfillmentStatus.COMPLETED:
        return ["add_note", "record_bonus"]
    actions = ["add_note", "open_exception"]
    if item.status == DigitalFulfillmentStatus.QUEUED:
        actions += ["assign_operator", "record_contact"]
    if item.status == DigitalFulfillmentStatus.WAITING_CUSTOMER and item.current_fulfillment_method == DigitalCartFulfillmentMethod.IN_STORE:
        actions += ["record_console_received"]
    if item.status in (DigitalFulfillmentStatus.READY_FOR_STAFF, DigitalFulfillmentStatus.WAITING_CUSTOMER):
        actions += ["start_work"]
    if item.status == DigitalFulfillmentStatus.IN_PROGRESS:
        actions += ["record_purchased_installation", "record_remote_handling", "staff_verify"]
    if item.status == DigitalFulfillmentStatus.WAITING_CONFIRMATION:
        actions += ["staff_verify"]
    if item.status == DigitalFulfillmentStatus.EXCEPTION:
        actions += ["retry"]
    if actor and item.assigned_operator_id and item.assigned_operator_id != actor.pk and actor.user_type != UserTypes.ADMIN:
        return []
    return actions


def admin_fulfillment_projection(item, *, actor=None):
    payment = item.obligation.finalization.payment
    customer = item.obligation.order.user
    return {
        "id": str(item.public_id),
        "selection": _selection(item),
        "customer": {"display_name": str(customer), "phone_number": customer.phone_number},
        "status": item.status,
        "waiting_reason": item.waiting_reason,
        "assigned_operator": ({"id": item.assigned_operator_id, "display_name": str(item.assigned_operator)} if item.assigned_operator_id else None),
        "payment_received": payment.collection_status == "paid",
        "entitlement_status": item.entitlement.status,
        "installed_games": _installed(item, include_audit=True),
        "timeline": _timeline(item, customer_safe=False),
        "allowed_actions": _allowed_actions(item, actor),
        "started_at": item.started_at,
        "completed_at": item.completed_at,
    }


def customer_fulfillment_projection(item):
    return {
        "id": str(item.public_id),
        "selection": _selection(item),
        "payment_received": item.obligation.finalization.payment.collection_status == "paid",
        "status": item.status,
        "required_action": item.waiting_reason,
        "entitlement_status": item.entitlement.status,
        "installed_games": _installed(item, include_audit=False),
        "timeline": _timeline(item, customer_safe=True),
        "remote_confirmation_eligible": (
            item.status == DigitalFulfillmentStatus.WAITING_CONFIRMATION
            and item.current_fulfillment_method == DigitalCartFulfillmentMethod.REMOTE
        ),
        "completed_at": item.completed_at,
    }


class AdminDigitalFulfillmentProjectionSerializer(serializers.Serializer):
    def to_representation(self, instance):
        return admin_fulfillment_projection(instance, actor=self.context.get("actor"))


class CustomerDigitalFulfillmentProjectionSerializer(serializers.Serializer):
    def to_representation(self, instance):
        return customer_fulfillment_projection(instance)
