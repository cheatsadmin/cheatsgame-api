from datetime import timedelta

from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from cheatgame.digital_products.models import (
    DigitalEntitlementStatus,
    DigitalFulfillmentItem,
    DigitalGameUpcomingStatus,
    Entitlement,
)
from cheatgame.digital_products.services.fulfillment import (
    activate_digital_fulfillment_obligation,
)
from cheatgame.digital_products.services.upcoming_games import (
    update_upcoming_game_metadata,
)
from cheatgame.financial_core.models import (
    DigitalFulfillmentObligation,
    PaymentCollectionStatus,
)
from cheatgame.financial_core.test_commercial_finalizer_phase1 import (
    CommercialFinalizerFixture,
)
from cheatgame.users.models import BaseUser, UserTypes


class PreorderV1Tests(CommercialFinalizerFixture, TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.client = APIClient()
        self.admin = BaseUser.objects.create_user(
            phone_number="09127777111",
            firstname="Preorder",
            lastname="Admin",
            password="test-only-password",
            user_type=UserTypes.ADMIN,
        )

    def test_paid_preorder_waits_without_fulfillment_then_reuses_release_pipeline(self):
        placement, pool = self.ready_digital(preorder=True)
        snapshot = placement.order.checkout.lines.get().digital_snapshot
        self.assertEqual(
            snapshot.safe_display_metadata["purchase_kind"],
            "preorder",
        )

        self.finalize(placement)
        placement.payment.refresh_from_db()
        pool.refresh_from_db()
        self.assertEqual(
            placement.payment.collection_status,
            PaymentCollectionStatus.PAID,
        )
        self.assertEqual(pool.sellable_quantity, 1)
        self.assertFalse(
            DigitalFulfillmentObligation.objects.filter(
                order=placement.order
            ).exists()
        )
        self.assertFalse(DigitalFulfillmentItem.objects.exists())
        self.assertFalse(Entitlement.objects.exists())

        self.client.force_authenticate(placement.order.user)
        response = self.client.get(
            "/api/digital-products/customer/preorders/"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(
            response.data[0]["status"]["code"],
            "WAITING_FOR_RELEASE",
        )
        other_customer = BaseUser.objects.create_user(
            phone_number="09127777112",
            firstname="Other",
            lastname="Customer",
            password="test-only-password",
            user_type=UserTypes.CUSTOMER,
        )
        other_customer.phone_verified = True
        other_customer.save(update_fields=["phone_verified"])
        self.client.force_authenticate(other_customer)
        self.assertEqual(
            self.client.get("/api/digital-products/customer/preorders/").data,
            [],
        )
        self.client.force_authenticate(placement.order.user)

        product = placement.order.order_items.get().product
        metadata = product.digital_release_metadata
        update_upcoming_game_metadata(
            product_id=product.pk,
            release_date=metadata.release_date,
            upcoming_status=DigitalGameUpcomingStatus.RELEASED,
            preorder_enabled=False,
            preorder_open_at=None,
            preorder_close_at=None,
            publish=True,
            actor=self.admin,
        )

        obligation = DigitalFulfillmentObligation.objects.get(
            order=placement.order
        )
        from cheatgame.digital_products.services.preorders import (
            release_paid_preorders_for_product,
        )

        product.digital_release_metadata.refresh_from_db()
        self.assertEqual(release_paid_preorders_for_product(product=product), 0)
        self.assertEqual(
            DigitalFulfillmentObligation.objects.filter(order=placement.order).count(),
            1,
        )
        self.assertFalse(DigitalFulfillmentItem.objects.exists())
        self.assertEqual(
            self.client.get(
                "/api/digital-products/customer/preorders/"
            ).data,
            [],
        )

        item = activate_digital_fulfillment_obligation(
            obligation_public_id=obligation.public_id
        )
        self.assertEqual(item.obligation_id, obligation.pk)
        entitlement = Entitlement.objects.get(obligation=obligation)
        self.assertEqual(
            entitlement.status,
            DigitalEntitlementStatus.PENDING_FULFILLMENT,
        )

    def test_announced_product_cannot_be_purchased(self):
        placement, _ = self.ready_digital(preorder=True)
        product = placement.order.order_items.get().product
        metadata = product.digital_release_metadata
        metadata.upcoming_status = DigitalGameUpcomingStatus.ANNOUNCED
        metadata.preorder_enabled = False
        metadata.release_date = timezone.localdate() + timedelta(days=30)
        metadata.save()

        from cheatgame.digital_products.public_catalog_selectors import (
            public_digital_game_detail,
        )

        self.assertIsNone(public_digital_game_detail(slug=product.slug))
