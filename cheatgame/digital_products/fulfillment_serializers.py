from rest_framework import serializers

from cheatgame.digital_products.models import (
    DigitalCartFulfillmentMethod,
    DigitalOfferCapacity,
    DigitalFulfillmentStatus,
    FulfillmentActivityType,
    FulfillmentActivityVisibility,
    InstalledGameRecordState,
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
        "title": record.game.title if record.game_id else record.fallback_title,
        "fallback_title": record.fallback_title,
        "delivered_version": (
            record.delivered_version.native_console
            if record.delivered_version_id
            else None
        ),
        "completion_source": record.completion_source,
        "operator": (
            {"id": record.operator_id, "display_name": str(record.operator)}
            if record.operator_id
            else None
        ),
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
        "activity_type": activity.activity_type,
        "actor_type": activity.actor_type,
        "actor": (
            {"id": activity.actor_id, "display_name": str(activity.actor)}
            if activity.actor_id
            else None
        ),
        "previous_status": activity.previous_status,
        "new_status": activity.new_status,
        "waiting_reason": activity.waiting_reason,
        "note": activity.note if (not customer_safe or activity.visibility == FulfillmentActivityVisibility.CUSTOMER_SAFE) else "",
        "created_at": activity.created_at,
    } for activity in activities]


def _has_activity(item, activity_type):
    return any(activity.activity_type == activity_type for activity in item.activities.all())


def _has_current_purchased_evidence(item):
    records = list(item.installed_games.all())
    superseded_ids = {record.corrects_id for record in records if record.corrects_id}
    return any(
        record.classification == InstalledGameClassification.PURCHASED
        and record.state == InstalledGameRecordState.RECORDED
        and record.pk not in superseded_ids
        for record in records
    )


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
        has_purchased = _has_current_purchased_evidence(item)
        if not has_purchased:
            actions += ["record_purchased_installation"]
        elif item.current_fulfillment_method == DigitalCartFulfillmentMethod.REMOTE:
            if not _has_activity(item, FulfillmentActivityType.REMOTE_HANDLING_PERFORMED):
                actions += ["record_remote_handling"]
            else:
                actions += ["staff_verify"]
        else:
            actions += ["staff_verify"]
    if item.status == DigitalFulfillmentStatus.WAITING_CONFIRMATION:
        actions += ["staff_verify"]
    if item.status == DigitalFulfillmentStatus.EXCEPTION:
        actions += ["retry"]
    if actor and item.assigned_operator_id and item.assigned_operator_id != actor.pk and actor.user_type != UserTypes.ADMIN:
        return []
    return actions


def _next_permitted_action(item, actions):
    if (
        not actions
        or item.status == DigitalFulfillmentStatus.COMPLETED
    ):
        return None
    if not item.assigned_operator_id and "assign_operator" in actions:
        return "assign_operator"
    priority = (
        "record_contact",
        "record_console_received",
        "start_work",
        "record_purchased_installation",
        "record_remote_handling",
        "staff_verify",
        "retry",
        "assign_operator",
        "change_method",
        "open_exception",
        "record_bonus",
        "add_note",
    )
    return next((action for action in priority if action in actions), None)


def _appointment(item):
    if not item.appointment_id:
        return None
    schedule = item.appointment.schedule
    return {
        "id": item.appointment_id,
        "service_type": str(item.appointment.type) if item.appointment.type_id else None,
        "starts_at": schedule.start if schedule else None,
        "ends_at": schedule.end if schedule else None,
    }


def _queue_projection(item, *, actor=None):
    payment = item.obligation.finalization.payment
    customer = item.obligation.order.user
    order = item.obligation.order
    line = item.obligation.checkout_line
    snapshot = line.digital_snapshot
    activities = list(item.activities.all())
    latest_activity = activities[-1] if activities else None
    allowed_actions = _allowed_actions(item, actor)
    return {
        "id": str(item.public_id),
        "order": {
            "id": order.pk,
            "tracking_code": order.public_tracking_code,
        },
        "order_item_id": item.obligation.order_item_id,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "customer": {
            "id": customer.pk,
            "display_name": str(customer),
            "phone_number": customer.phone_number,
        },
        "game": {"title": snapshot.product_name},
        "selection": _selection(item),
        "commercial": {
            "payment_status": payment.collection_status,
            "accepted_line_price": line.line_payable_total,
            "order_total": payment.amount_due,
            "currency": payment.currency,
        },
        "status": item.status,
        "waiting_reason": item.waiting_reason,
        "assigned_operator": ({"id": item.assigned_operator_id, "display_name": str(item.assigned_operator)} if item.assigned_operator_id else None),
        "assignment_state": "assigned" if item.assigned_operator_id else "unassigned",
        "appointment": _appointment(item),
        "latest_activity": {
            "activity_type": latest_activity.activity_type if latest_activity else None,
            "created_at": latest_activity.created_at if latest_activity else None,
        },
        "entitlement_status": item.entitlement.status,
        "exception": item.status == DigitalFulfillmentStatus.EXCEPTION,
        "next_permitted_action": _next_permitted_action(
            item,
            allowed_actions,
        ),
        "allowed_actions": allowed_actions,
        "completed_at": item.completed_at,
    }


def admin_fulfillment_list_projection(item, *, actor=None):
    return _queue_projection(item, actor=actor)


def admin_fulfillment_projection(item, *, actor=None):
    result = _queue_projection(item, actor=actor)
    customer = item.obligation.order.user
    snapshot = item.obligation.checkout_line.digital_snapshot
    payment = item.obligation.finalization.payment
    activities = _timeline(item, customer_safe=False)
    result.update({
        "customer": {
            **result["customer"],
            "email": customer.email,
        },
        "purchased_fulfillment_method": item.obligation.fulfillment_method,
        "compatibility": {
            "code": snapshot.compatibility_disclosure,
            "text": snapshot.compatibility_disclosure,
        },
        "capacity_disclosure": {
            "code": snapshot.capacity_disclosure,
            "text": snapshot.capacity_disclosure,
        },
        "operational_reference": item.internal_reference,
        "payment": {
            "id": str(payment.public_id),
            "status": payment.collection_status,
            "confirmed_amount": payment.confirmed_amount,
            "amount_due": payment.amount_due,
            "currency": payment.currency,
        },
        "entitlement": {
            "status": item.entitlement.status,
            "created_at": item.entitlement.created_at,
            "activated_at": item.entitlement.activated_at,
        },
        "installation": {
            "started_at": item.started_at,
            "completed_at": item.completed_at,
            "installed_games": _installed(item, include_audit=True),
        },
        "exception_context": next(
            (
                {
                    "note": activity["note"],
                    "created_at": activity["created_at"],
                }
                for activity in reversed(activities)
                if activity["activity_type"] == FulfillmentActivityType.FAILURE_RECORDED
            ),
            None,
        ),
        "notes": [
            activity
            for activity in activities
            if activity["activity_type"] == FulfillmentActivityType.NOTE_ADDED
        ],
        "activities": activities,
        "revision": item.updated_at,
        "requires_customer_account_credentials": (
            snapshot.capacity in (
                DigitalOfferCapacity.CAPACITY_2,
                DigitalOfferCapacity.CAPACITY_3,
            )
        ),
        "credential_state": "not_supported",
        "account_information": None,
    })
    return result


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


class AdminOrderSummarySerializer(serializers.Serializer):
    id = serializers.IntegerField()
    tracking_code = serializers.CharField()


class AdminCustomerSummarySerializer(serializers.Serializer):
    id = serializers.IntegerField()
    display_name = serializers.CharField()
    phone_number = serializers.CharField(allow_null=True)
    email = serializers.EmailField(allow_null=True, required=False)


class AdminGameSummarySerializer(serializers.Serializer):
    title = serializers.CharField()


class AdminSelectionSummarySerializer(serializers.Serializer):
    customer_console = serializers.CharField()
    capacity = serializers.CharField()
    delivered_version_label = serializers.CharField()
    native_console = serializers.CharField()
    compatibility_disclosure = serializers.CharField()
    capacity_disclosure = serializers.CharField()
    purchased_fulfillment_method = serializers.CharField()
    current_fulfillment_method = serializers.CharField()


class AdminCommercialSummarySerializer(serializers.Serializer):
    payment_status = serializers.CharField()
    accepted_line_price = serializers.DecimalField(
        max_digits=18,
        decimal_places=0,
    )
    order_total = serializers.DecimalField(
        max_digits=18,
        decimal_places=0,
    )
    currency = serializers.CharField()


class AdminOperatorSummarySerializer(serializers.Serializer):
    id = serializers.IntegerField()
    display_name = serializers.CharField()


class AdminAppointmentSummarySerializer(serializers.Serializer):
    id = serializers.IntegerField()
    service_type = serializers.CharField(allow_null=True)
    starts_at = serializers.DateTimeField(allow_null=True)
    ends_at = serializers.DateTimeField(allow_null=True)


class AdminLatestActivitySummarySerializer(serializers.Serializer):
    activity_type = serializers.CharField(allow_null=True)
    created_at = serializers.DateTimeField(allow_null=True)


class AdminDigitalFulfillmentListProjectionSerializer(
    serializers.Serializer
):
    id = serializers.UUIDField()
    order = AdminOrderSummarySerializer()
    order_item_id = serializers.IntegerField()
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()
    customer = AdminCustomerSummarySerializer()
    game = AdminGameSummarySerializer()
    selection = AdminSelectionSummarySerializer()
    commercial = AdminCommercialSummarySerializer()
    status = serializers.CharField()
    waiting_reason = serializers.CharField(allow_null=True)
    assigned_operator = AdminOperatorSummarySerializer(allow_null=True)
    assignment_state = serializers.ChoiceField(
        choices=("assigned", "unassigned")
    )
    appointment = AdminAppointmentSummarySerializer(allow_null=True)
    latest_activity = AdminLatestActivitySummarySerializer()
    entitlement_status = serializers.CharField()
    exception = serializers.BooleanField()
    next_permitted_action = serializers.CharField(allow_null=True)
    allowed_actions = serializers.ListField(
        child=serializers.CharField()
    )
    completed_at = serializers.DateTimeField(allow_null=True)

    def to_representation(self, instance):
        return admin_fulfillment_list_projection(
            instance,
            actor=self.context.get("actor"),
        )


class AdminPaymentSummarySerializer(serializers.Serializer):
    id = serializers.UUIDField()
    status = serializers.CharField()
    confirmed_amount = serializers.DecimalField(
        max_digits=18,
        decimal_places=0,
    )
    amount_due = serializers.DecimalField(
        max_digits=18,
        decimal_places=0,
    )
    currency = serializers.CharField()


class AdminEntitlementSummarySerializer(serializers.Serializer):
    status = serializers.CharField()
    created_at = serializers.DateTimeField()
    activated_at = serializers.DateTimeField(allow_null=True)


class AdminInstalledGameSummarySerializer(serializers.Serializer):
    classification = serializers.CharField()
    title = serializers.CharField()
    fallback_title = serializers.CharField(allow_blank=True)
    delivered_version = serializers.CharField(allow_null=True)
    completion_source = serializers.CharField()
    operator = AdminOperatorSummarySerializer(allow_null=True)
    state = serializers.CharField()
    is_current = serializers.BooleanField()
    installed_at = serializers.DateTimeField()


class AdminInstallationSummarySerializer(serializers.Serializer):
    started_at = serializers.DateTimeField(allow_null=True)
    completed_at = serializers.DateTimeField(allow_null=True)
    installed_games = AdminInstalledGameSummarySerializer(many=True)


class AdminActivitySummarySerializer(serializers.Serializer):
    type = serializers.CharField()
    status = serializers.CharField(allow_null=True)
    activity_type = serializers.CharField()
    actor_type = serializers.CharField()
    actor = AdminOperatorSummarySerializer(allow_null=True)
    previous_status = serializers.CharField(allow_null=True)
    new_status = serializers.CharField(allow_null=True)
    waiting_reason = serializers.CharField(allow_null=True)
    note = serializers.CharField(allow_blank=True)
    created_at = serializers.DateTimeField()


class AdminTextDisclosureSerializer(serializers.Serializer):
    code = serializers.CharField()
    text = serializers.CharField()


class AdminExceptionSummarySerializer(serializers.Serializer):
    note = serializers.CharField()
    created_at = serializers.DateTimeField()


class AdminDigitalFulfillmentProjectionSerializer(
    AdminDigitalFulfillmentListProjectionSerializer
):
    purchased_fulfillment_method = serializers.CharField()
    compatibility = AdminTextDisclosureSerializer()
    capacity_disclosure = AdminTextDisclosureSerializer()
    operational_reference = serializers.CharField(allow_blank=True)
    payment = AdminPaymentSummarySerializer()
    entitlement = AdminEntitlementSummarySerializer()
    installation = AdminInstallationSummarySerializer()
    exception_context = AdminExceptionSummarySerializer(allow_null=True)
    notes = AdminActivitySummarySerializer(many=True)
    activities = AdminActivitySummarySerializer(many=True)
    revision = serializers.DateTimeField()
    requires_customer_account_credentials = serializers.BooleanField()
    credential_state = serializers.ChoiceField(
        choices=("not_supported",)
    )
    account_information = serializers.JSONField(allow_null=True)

    def to_representation(self, instance):
        return admin_fulfillment_projection(
            instance,
            actor=self.context.get("actor"),
        )


class CustomerDigitalFulfillmentProjectionSerializer(serializers.Serializer):
    def to_representation(self, instance):
        return customer_fulfillment_projection(instance)
