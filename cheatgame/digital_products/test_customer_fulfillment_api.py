from unittest.mock import patch
from uuid import uuid4

from django.test import TransactionTestCase
from rest_framework.test import APIClient

from cheatgame.digital_products.models import (
    DigitalCartFulfillmentMethod,
    DigitalEntitlementStatus,
    DigitalFulfillmentStatus,
    DigitalOffer,
    DigitalOfferCapacity,
    DigitalOfferSaleState,
    FulfillmentActivity,
    FulfillmentActivityType,
    InstalledGameCompletionSource,
    InventoryPool,
    InventoryPoolStatus,
)
from cheatgame.digital_products.services.cart import add_digital_offer_to_cart
from cheatgame.digital_products.services.fulfillment import (
    add_fulfillment_note,
    assign_fulfillment_operator,
    customer_confirm_remote_completion,
    provision_digital_fulfillment_obligation,
    record_customer_contact,
    record_purchased_game_installation,
    record_remote_handling,
    start_fulfillment_work,
)
from cheatgame.financial_core.models import DigitalFulfillmentObligation
from cheatgame.financial_core.test_commercial_finalizer_phase1 import (
    CommercialFinalizerFixture,
)
from cheatgame.users.models import BaseUser, UserTypes


class CustomerDigitalFulfillmentApiTests(
    CommercialFinalizerFixture,
    TransactionTestCase,
):
    reset_sequences = True

    list_url = "/api/digital-products/customer/fulfillments/"

    def customer_client(self, customer):
        customer.phone_verified = True
        customer.save(update_fields=("phone_verified", "updated_at"))
        client = APIClient()
        client.force_authenticate(customer)
        return client

    def manager(self, suffix="1"):
        return BaseUser.objects.create_user(
            phone_number=f"0912888800{suffix}",
            firstname="Synthetic",
            lastname="Operator",
            user_type=UserTypes.MANAGER,
        )

    def provision(self):
        placement, pool = self.ready_digital()
        self.finalize(placement)
        obligation = DigitalFulfillmentObligation.objects.get(order=placement.order)
        item = provision_digital_fulfillment_obligation(
            obligation_public_id=obligation.public_id,
            idempotency_key=uuid4(),
        )
        return placement, pool, item

    def provision_remote(self):
        def add_remote_line(**kwargs):
            pool = InventoryPool.objects.create(
                sellable_quantity=2,
                status=InventoryPoolStatus.ENABLED,
            )
            offer = DigitalOffer.objects.create(
                delivered_version=kwargs["offer"].delivered_version,
                customer_console=kwargs["offer"].customer_console,
                capacity=DigitalOfferCapacity.CAPACITY_2,
                price=kwargs["offer"].price,
                inventory_pool=pool,
                sale_state=DigitalOfferSaleState.ACTIVE,
            )
            return add_digital_offer_to_cart(
                cart=kwargs["cart"],
                offer=offer,
                fulfillment_method=DigitalCartFulfillmentMethod.REMOTE,
                actor=kwargs["actor"],
            )

        with patch(
            "cheatgame.financial_core.test_commercial_finalizer_phase1.add_digital_offer_to_cart",
            side_effect=add_remote_line,
        ):
            placement, pool = self.ready_digital()
        self.finalize(placement)
        obligation = DigitalFulfillmentObligation.objects.get(order=placement.order)
        item = provision_digital_fulfillment_obligation(
            obligation_public_id=obligation.public_id,
            idempotency_key=uuid4(),
        )
        return placement, pool, item

    def prepare_remote_confirmation(self):
        placement, _, item = self.provision_remote()
        operator = self.manager()
        assign_fulfillment_operator(
            fulfillment_id=item.public_id,
            operator=operator,
            actor=operator,
            idempotency_key=uuid4(),
        )
        record_customer_contact(
            fulfillment_id=item.public_id,
            actor=operator,
            idempotency_key=uuid4(),
        )
        start_fulfillment_work(
            fulfillment_id=item.public_id,
            actor=operator,
            idempotency_key=uuid4(),
        )
        record_purchased_game_installation(
            fulfillment_id=item.public_id,
            actor=operator,
            idempotency_key=uuid4(),
            completion_source=InstalledGameCompletionSource.STAFF_VERIFIED_REMOTE,
        )
        record_remote_handling(
            fulfillment_id=item.public_id,
            actor=operator,
            idempotency_key=uuid4(),
        )
        return placement, item, operator

    def detail_url(self, item):
        return f"{self.list_url}{item.public_id}/"

    def confirm_url(self, item):
        return f"{self.detail_url(item)}confirm-remote-completion/"

    def test_owned_list_and_detail_use_one_uuid_customer_contract(self):
        placement, _, item = self.provision()
        client = self.customer_client(placement.order.user)

        list_response = client.get(self.list_url)
        detail_response = client.get(self.detail_url(item))

        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(detail_response.status_code, 200)
        listed = list_response.json()["results"][0]
        detail = detail_response.json()
        self.assertEqual(listed["id"], str(item.public_id))
        self.assertEqual(detail["id"], str(item.public_id))
        self.assertEqual(
            set(listed),
            {
                "id",
                "order_tracking_code",
                "created_at",
                "updated_at",
                "last_meaningful_update_at",
                "game",
                "selection",
                "commercial",
                "status",
                "required_action",
                "completed_at",
                "entitlement",
                "can_confirm_remote_completion",
            },
        )
        self.assertEqual(
            set(detail) - set(listed),
            {
                "timeline",
                "timeline_truncated",
                "timeline_limit",
                "installed_games",
            },
        )
        self.assertEqual(detail["commercial"]["payment_status"], "PAID")
        self.assertEqual(detail["entitlement"]["status"], "PENDING_FULFILLMENT")

    def test_foreign_customer_cannot_list_or_retrieve_fulfillment(self):
        _, _, item = self.provision()
        stranger = BaseUser.objects.create_user(
            phone_number="09128888999",
            firstname="Foreign",
            lastname="Customer",
            user_type=UserTypes.CUSTOMER,
        )
        client = self.customer_client(stranger)

        self.assertEqual(client.get(self.list_url).json()["count"], 0)
        response = client.get(self.detail_url(item))

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "digital_fulfillment_not_found")

    def test_remote_confirmation_is_owner_only_replay_safe_and_completes(self):
        placement, item, _ = self.prepare_remote_confirmation()
        client = self.customer_client(placement.order.user)
        key = str(uuid4())

        before = client.get(self.detail_url(item)).json()
        first = client.post(
            self.confirm_url(item),
            {"idempotency_key": key},
            format="json",
        )
        second = client.post(
            self.confirm_url(item),
            {"idempotency_key": key},
            format="json",
        )

        self.assertTrue(before["can_confirm_remote_completion"])
        self.assertEqual(
            before["required_action"]["code"],
            "CONFIRM_REMOTE_COMPLETION",
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(first.json()["status"]["code"], "COMPLETED")
        self.assertEqual(first.json()["entitlement"]["status"], "ACTIVE")
        self.assertFalse(first.json()["can_confirm_remote_completion"])
        self.assertEqual(
            FulfillmentActivity.objects.filter(
                fulfillment_item=item,
                activity_type=FulfillmentActivityType.CUSTOMER_CONFIRMED,
            ).count(),
            1,
        )

    def test_invalid_remote_confirmation_state_and_uuid_fail_safely(self):
        placement, _, item = self.provision()
        client = self.customer_client(placement.order.user)

        invalid_state = client.post(
            self.confirm_url(item),
            {"idempotency_key": str(uuid4())},
            format="json",
        )
        malformed = client.get(f"{self.list_url}not-a-uuid/")

        self.assertEqual(invalid_state.status_code, 409)
        self.assertEqual(
            invalid_state.json()["code"],
            "digital_fulfillment_confirmation_unavailable",
        )
        self.assertEqual(malformed.status_code, 404)
        item.refresh_from_db()
        self.assertEqual(item.status, DigitalFulfillmentStatus.QUEUED)

    def test_completed_filter_and_projection_remain_customer_safe(self):
        placement, item, operator = self.prepare_remote_confirmation()
        add_fulfillment_note(
            fulfillment_id=item.public_id,
            actor=operator,
            idempotency_key=uuid4(),
            note="Internal synthetic support note",
        )
        add_fulfillment_note(
            fulfillment_id=item.public_id,
            actor=operator,
            idempotency_key=uuid4(),
            note="Customer-safe synthetic note",
            customer_safe=True,
        )
        customer_confirm_remote_completion(
            fulfillment_id=item.public_id,
            actor=placement.order.user,
            idempotency_key=uuid4(),
        )
        client = self.customer_client(placement.order.user)

        active = client.get(self.list_url, {"view": "active"}).json()
        completed = client.get(self.list_url, {"view": "completed"}).json()
        detail = client.get(self.detail_url(item)).json()
        rendered = repr(detail).lower()

        self.assertEqual(active["count"], 0)
        self.assertEqual(completed["count"], 1)
        self.assertEqual(
            detail["entitlement"]["status"],
            DigitalEntitlementStatus.ACTIVE.upper(),
        )
        self.assertNotIn("operator", rendered)
        self.assertNotIn("internal synthetic support note", rendered)
        self.assertNotIn("customer-safe synthetic note", rendered)
        self.assertNotIn("assigned_operator", rendered)
        self.assertNotIn("payment_id", rendered)
        self.assertNotIn("reservation", rendered)
