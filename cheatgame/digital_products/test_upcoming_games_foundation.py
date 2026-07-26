from datetime import date, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from rest_framework.test import APIClient

from cheatgame.digital_products.models import (
    DigitalGameReleaseMetadata,
    DigitalGameUpcomingStatus,
    DigitalOffer,
    DigitalOfferCapacity,
    DigitalOfferSaleState,
    InventoryPool,
    InventoryPoolStatus,
)
from cheatgame.product.models import (
    DeliveredVersion,
    NativeConsole,
    Product,
    ProductCommerceAuthority,
    ProductStatus,
    ProductType,
)
from cheatgame.users.models import BaseUser, UserTypes


class UpcomingGamesFoundationTests(TestCase):
    public_url = "/api/digital-products/catalog/upcoming-games/"

    def setUp(self):
        self.client = APIClient()
        self.admin = BaseUser.objects.create_user(
            phone_number="09128888111",
            firstname="Upcoming",
            lastname="Admin",
            password="test-only-password",
            user_type=UserTypes.ADMIN,
        )

    def game(self, title, *, status=ProductStatus.PUBLISHED):
        product = Product.objects.create(
            product_type=ProductType.GAME,
            commerce_authority=ProductCommerceAuthority.DIGITAL_PRODUCTS,
            title=title,
            status=status,
            main_image="tests/upcoming-cover.jpg",
            description="tests/upcoming-description.html",
            price=Decimal("0"),
            off_price=Decimal("0"),
            quantity=0,
        )
        DeliveredVersion.objects.create(
            product=product,
            native_console=NativeConsole.PS5,
        )
        return product

    def metadata(self, product, *, release_date=None, upcoming_status=DigitalGameUpcomingStatus.COMING_SOON):
        return DigitalGameReleaseMetadata.objects.create(
            product=product,
            release_date=release_date,
            upcoming_status=upcoming_status,
        )

    def active_offer(self, product):
        version = product.delivered_versions.get()
        pool = InventoryPool.objects.create(
            sellable_quantity=3,
            status=InventoryPoolStatus.ENABLED,
        )
        return DigitalOffer.objects.create(
            delivered_version=version,
            customer_console=NativeConsole.PS5,
            capacity=DigitalOfferCapacity.CAPACITY_2,
            price=Decimal("500000"),
            inventory_pool=pool,
            sale_state=DigitalOfferSaleState.ACTIVE,
        )

    def test_model_is_game_owned_and_preorder_fails_closed(self):
        physical = Product.objects.create(
            product_type=ProductType.PHYSCIAL,
            title="Physical",
            main_image="tests/physical.jpg",
            description="tests/physical.html",
            price=1,
            off_price=1,
        )
        with self.assertRaises(ValidationError):
            DigitalGameReleaseMetadata.objects.create(product=physical)

        game = self.game("No Preorder")
        with self.assertRaises(ValidationError):
            DigitalGameReleaseMetadata.objects.create(
                product=game,
                upcoming_status=DigitalGameUpcomingStatus.PREORDER_OPEN,
                preorder_enabled=True,
            )

    def test_public_projection_is_bounded_ordered_and_contains_no_price_authority(self):
        unknown = self.game("Unknown Date")
        nearest = self.game("Nearest")
        later = self.game("Later")
        self.metadata(unknown)
        self.metadata(nearest, release_date=date.today() + timedelta(days=10))
        self.metadata(later, release_date=date.today() + timedelta(days=30))

        response = self.client.get(self.public_url, {"limit": 2, "offset": 0})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 3)
        self.assertEqual(
            [row["title"] for row in response.data["results"]],
            ["Nearest", "Later"],
        )
        row = response.data["results"][0]
        self.assertEqual(row["supported_customer_consoles"], ["ps5"])
        self.assertEqual(row["upcoming_status_label"], "بزودی")
        self.assertFalse(row["preorder_available"])
        self.assertIsNone(row["preorder_price"])
        self.assertNotIn("purchase_flow", row)

    def test_released_cancelled_hidden_and_wrong_console_are_excluded(self):
        released = self.game("Released")
        cancelled = self.game("Cancelled")
        hidden = self.game("Hidden", status=ProductStatus.HIDDEN)
        ps4_only = self.game("PS4 Only")
        ps4_only.delivered_versions.all().delete()
        DeliveredVersion.objects.create(
            product=ps4_only,
            native_console=NativeConsole.PS4,
        )
        self.metadata(released, upcoming_status=DigitalGameUpcomingStatus.RELEASED)
        self.metadata(cancelled, upcoming_status=DigitalGameUpcomingStatus.CANCELLED)
        self.metadata(hidden)
        self.metadata(ps4_only)

        response = self.client.get(self.public_url, {"console": "ps5"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["results"], [])

    def test_admin_can_configure_display_metadata_but_not_preorder(self):
        game = self.game("Admin Upcoming", status=ProductStatus.HIDDEN)
        self.client.force_authenticate(self.admin)
        url = f"/api/digital-products/admin/catalog/games/{game.pk}/release-metadata/"
        payload = {
            "release_date": str(date.today() + timedelta(days=45)),
            "upcoming_status": DigitalGameUpcomingStatus.COMING_SOON,
            "preorder_enabled": False,
            "preorder_open_at": None,
            "preorder_close_at": None,
            "publish": True,
        }
        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["release_metadata"]["configured"])
        self.assertTrue(response.data["release_metadata"]["published"])
        self.assertFalse(
            response.data["release_metadata"]["preorder_commerce_supported"]
        )

        payload["preorder_enabled"] = True
        forbidden = self.client.post(url, payload, format="json")
        self.assertEqual(forbidden.status_code, 400)
        self.assertEqual(
            DigitalGameReleaseMetadata.objects.get(product=game).preorder_enabled,
            False,
        )

    def test_active_offer_blocks_upcoming_admin_transition_and_public_purchase(self):
        game = self.game("Already Sellable")
        offer = self.active_offer(game)
        self.client.force_authenticate(self.admin)
        url = f"/api/digital-products/admin/catalog/games/{game.pk}/release-metadata/"
        response = self.client.post(
            url,
            {
                "release_date": str(date.today() + timedelta(days=20)),
                "upcoming_status": DigitalGameUpcomingStatus.COMING_SOON,
                "preorder_enabled": False,
                "publish": True,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(
            DigitalGameReleaseMetadata.objects.filter(product=game).exists()
        )

        self.metadata(game)
        normal_catalog = self.client.get("/api/digital-products/catalog/games/")
        self.assertNotIn(game.title, str(normal_catalog.data))
        self.assertEqual(
            self.client.get(self.public_url).data["results"][0]["id"],
            game.pk,
        )
        offer.refresh_from_db()
        self.assertEqual(offer.sale_state, DigitalOfferSaleState.ACTIVE)

    def test_invalid_pagination_limit_is_rejected(self):
        response = self.client.get(self.public_url, {"limit": 51})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "invalid_request")
