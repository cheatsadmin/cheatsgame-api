from uuid import uuid4

from django.test import TransactionTestCase
from rest_framework.test import APIClient

from cheatgame.digital_products.models import (
    DigitalFulfillmentStatus,
    FulfillmentActivity,
    FulfillmentActivityType,
)
from cheatgame.digital_products.services.fulfillment import (
    provision_digital_fulfillment_obligation,
)
from cheatgame.financial_core.models import DigitalFulfillmentObligation
from cheatgame.financial_core.test_commercial_finalizer_phase1 import (
    CommercialFinalizerFixture,
)
from cheatgame.users.models import BaseUser, UserTypes


class AdminDigitalFulfillmentContractTests(
    CommercialFinalizerFixture,
    TransactionTestCase,
):
    reset_sequences = True

    def setUp(self):
        super().setUp()
        placement, _ = self.ready_digital()
        self.finalize(placement)
        obligation = DigitalFulfillmentObligation.objects.get(
            order=placement.order
        )
        self.item = provision_digital_fulfillment_obligation(
            obligation_public_id=obligation.public_id,
            idempotency_key=uuid4(),
        )
        self.customer = placement.order.user
        self.admin = BaseUser.objects.create_user(
            phone_number="09128888001",
            firstname="Admin",
            lastname="Operator",
            user_type=UserTypes.ADMIN,
        )
        self.manager = BaseUser.objects.create_user(
            phone_number="09128888002",
            firstname="Manager",
            lastname="Operator",
            user_type=UserTypes.MANAGER,
        )
        self.inactive_manager = BaseUser.objects.create_user(
            phone_number="09128888003",
            firstname="Inactive",
            lastname="Operator",
            user_type=UserTypes.MANAGER,
            is_active=False,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    @property
    def list_url(self):
        return "/api/digital-products/admin/fulfillments/"

    @property
    def detail_url(self):
        return (
            f"/api/digital-products/admin/fulfillments/"
            f"{self.item.public_id}/"
        )

    def command_url(self, command):
        return f"{self.detail_url}{command}/"

    def post(self, command, **payload):
        payload.setdefault("idempotency_key", str(uuid4()))
        return self.client.post(
            self.command_url(command),
            payload,
            format="json",
        )

    def test_list_contract_is_paginated_safe_and_contains_no_credentials(self):
        response = self.client.get(
            self.list_url,
            {"queue": "new", "assignment": "unassigned"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        row = response.data["results"][0]
        self.assertEqual(row["id"], str(self.item.public_id))
        self.assertEqual(row["assignment_state"], "unassigned")
        self.assertEqual(row["selection"]["current_fulfillment_method"], "in_store")
        self.assertIn("assign_operator", row["allowed_actions"])
        self.assertEqual(row["next_permitted_action"], "assign_operator")
        rendered = repr(response.data).lower()
        for forbidden in ("password", "recovery", "account_information"):
            self.assertNotIn(forbidden, rendered)

    def test_detail_contract_uses_frozen_authority_and_safe_credential_gate(self):
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-store, private")
        self.assertEqual(
            response.data["game"]["title"],
            self.item.obligation.checkout_line.digital_snapshot.product_name,
        )
        self.assertEqual(
            response.data["customer"]["phone_number"],
            self.customer.phone_number,
        )
        self.assertEqual(response.data["credential_state"], "not_supported")
        self.assertIsNone(response.data["account_information"])
        self.assertIn("activities", response.data)
        self.assertIn("revision", response.data)

    def test_operator_directory_only_contains_active_admins_and_managers(self):
        customer = BaseUser.objects.create_user(
            phone_number="09128888004",
            firstname="Customer",
            lastname="User",
            user_type=UserTypes.CUSTOMER,
        )
        response = self.client.get(
            "/api/digital-products/admin/operators/"
        )
        self.assertEqual(response.status_code, 200)
        ids = {row["id"] for row in response.data}
        self.assertIn(self.admin.pk, ids)
        self.assertIn(self.manager.pk, ids)
        self.assertNotIn(self.inactive_manager.pk, ids)
        self.assertNotIn(customer.pk, ids)

    def test_options_are_the_actual_server_vocabulary(self):
        response = self.client.get(
            "/api/digital-products/admin/fulfillment-options/"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            {item["value"] for item in response.data["statuses"]},
            set(DigitalFulfillmentStatus.values),
        )
        self.assertIn("record_purchased_installation", response.data["actions"])
        self.assertIn("staff_verify", response.data["actions"])
        self.assertNotIn("record_installation", response.data["actions"])
        self.assertNotIn("staff_verify_completion", response.data["actions"])

    def test_assignment_replays_and_conflicting_reuse_is_stable(self):
        key = str(uuid4())
        first = self.client.post(
            self.command_url("assign-operator"),
            {
                "operator_id": self.manager.pk,
                "idempotency_key": key,
            },
            format="json",
        )
        second = self.client.post(
            self.command_url("assign-operator"),
            {
                "operator_id": self.manager.pk,
                "idempotency_key": key,
            },
            format="json",
        )
        conflict = self.client.post(
            self.command_url("assign-operator"),
            {
                "operator_id": self.admin.pk,
                "idempotency_key": key,
            },
            format="json",
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.data["next_permitted_action"], "record_contact")
        self.assertEqual(
            FulfillmentActivity.objects.filter(
                fulfillment_item=self.item,
                activity_type=FulfillmentActivityType.OPERATOR_ASSIGNED,
            ).count(),
            1,
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.data["code"], "fulfillment_conflict")

    def test_unauthorized_customer_cannot_read_or_mutate(self):
        self.client.force_authenticate(self.customer)
        self.assertEqual(self.client.get(self.list_url).status_code, 403)
        response = self.post("record-contact", contacted=True)
        self.assertEqual(response.status_code, 403)

    def test_invalid_transition_and_delivery_bypass_are_conflicts(self):
        response = self.post("staff-verify")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.data["code"],
            "invalid_fulfillment_transition",
        )
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, DigitalFulfillmentStatus.QUEUED)

    def test_allowed_actions_follow_real_in_store_prerequisites(self):
        assign = self.post("assign-operator", operator_id=self.admin.pk)
        self.assertEqual(assign.status_code, 200)
        contact = self.post("record-contact", contacted=True)
        self.assertEqual(contact.status_code, 200)
        self.assertIn(
            "record_console_received",
            contact.data["allowed_actions"],
        )
        received = self.post("record-console-received")
        self.assertEqual(received.status_code, 200)
        self.assertIn("start_work", received.data["allowed_actions"])
        started = self.post("start-work")
        self.assertEqual(started.status_code, 200)
        self.assertEqual(
            started.data["allowed_actions"],
            [
                "add_note",
                "open_exception",
                "record_purchased_installation",
            ],
        )
        installed = self.post("record-purchased-installation")
        self.assertEqual(installed.status_code, 200)
        self.assertIn("staff_verify", installed.data["allowed_actions"])
        completed = self.post("staff-verify")
        self.assertEqual(completed.status_code, 200)
        self.assertEqual(completed.data["status"], "completed")

    def test_old_optional_nulls_serialize_without_failure(self):
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data["appointment"])
        self.assertIsNone(response.data["assigned_operator"])
        self.assertIsNone(response.data["exception_context"])
