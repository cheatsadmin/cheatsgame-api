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

    def game(
        self,
        title,
        *,
        status=ProductStatus.PUBLISHED,
        commerce_authority=ProductCommerceAuthority.DIGITAL_PRODUCTS,
    ):
        product = Product.objects.create(
            product_type=ProductType.GAME,
            commerce_authority=commerce_authority,
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

    def test_model_is_game_owned_and_preorder_state_is_coherent(self):
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

        game = self.game("Preorder")
        metadata = DigitalGameReleaseMetadata.objects.create(
            product=game,
            release_date=date.today() + timedelta(days=30),
            upcoming_status=DigitalGameUpcomingStatus.PREORDER_OPEN,
            preorder_enabled=True,
        )
        self.assertTrue(metadata.preorder_enabled)

        another = self.game("Incoherent Preorder")
        with self.assertRaises(ValidationError):
            DigitalGameReleaseMetadata.objects.create(
                product=another,
                upcoming_status=DigitalGameUpcomingStatus.PREORDER_OPEN,
                preorder_enabled=False,
            )

    def test_public_projection_is_bounded_ordered_and_contains_no_price_authority(self):
        unknown = self.game("Unknown Date")
        nearest = self.game("Nearest")
        later = self.game("Later")
        self.metadata(
            unknown,
            upcoming_status=DigitalGameUpcomingStatus.ANNOUNCED,
        )
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
        self.assertEqual(row["upcoming_status_label"], "به‌زودی")
        self.assertFalse(row["preorder_available"])
        self.assertIsNone(row["preorder_price"])
        self.assertNotIn("purchase_flow", row)

    def test_public_upcoming_requires_digital_authority_but_no_offer_or_inventory(self):
        digital = self.game("Digital Upcoming")
        standard = self.game(
            "Standard Upcoming",
            commerce_authority=ProductCommerceAuthority.STANDARD_COMMERCE,
        )
        self.metadata(
            digital,
            release_date=date.today() + timedelta(days=15),
        )
        self.metadata(
            standard,
            release_date=date.today() + timedelta(days=15),
        )

        response = self.client.get(self.public_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [row["title"] for row in response.data["results"]],
            ["Digital Upcoming"],
        )
        self.assertFalse(DigitalOffer.objects.filter(delivered_version__product=digital).exists())
        self.assertFalse(InventoryPool.objects.exists())

    def test_public_status_labels_distinguish_announced_and_delayed(self):
        announced = self.game("Announced")
        delayed = self.game("Delayed")
        self.metadata(
            announced,
            upcoming_status=DigitalGameUpcomingStatus.ANNOUNCED,
        )
        self.metadata(
            delayed,
            upcoming_status=DigitalGameUpcomingStatus.DELAYED,
        )

        response = self.client.get(self.public_url)
        labels = {
            row["title"]: row["upcoming_status_label"]
            for row in response.data["results"]
        }

        self.assertEqual(labels["Announced"], "معرفی‌شده")
        self.assertEqual(labels["Delayed"], "تأخیرخورده")

    def test_released_cancelled_hidden_and_wrong_console_are_excluded(self):
        released = self.game("Released")
        cancelled = self.game("Cancelled")
        hidden = self.game("Hidden", status=ProductStatus.HIDDEN)
        draft = self.game("Draft", status=ProductStatus.DRAFT)
        ps4_only = self.game("PS4 Only")
        ps4_only.delivered_versions.all().delete()
        DeliveredVersion.objects.create(
            product=ps4_only,
            native_console=NativeConsole.PS4,
        )
        self.metadata(released, upcoming_status=DigitalGameUpcomingStatus.RELEASED)
        self.metadata(cancelled, upcoming_status=DigitalGameUpcomingStatus.CANCELLED)
        self.metadata(hidden)
        self.metadata(draft)
        self.metadata(ps4_only)

        response = self.client.get(self.public_url, {"console": "ps5"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["results"], [])

    def test_stale_release_date_is_excluded_from_public_upcoming(self):
        stale = self.game("Stale Upcoming")
        self.metadata(
            stale,
            release_date=date.today() - timedelta(days=1),
        )

        response = self.client.get(self.public_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["results"], [])

    def test_admin_can_configure_display_metadata_and_preorder_state(self):
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
        self.assertTrue(
            response.data["release_metadata"]["preorder_commerce_supported"]
        )

        payload["upcoming_status"] = DigitalGameUpcomingStatus.PREORDER_OPEN
        payload["preorder_enabled"] = True
        preorder = self.client.post(url, payload, format="json")
        self.assertEqual(preorder.status_code, 200)
        metadata = DigitalGameReleaseMetadata.objects.get(product=game)
        self.assertTrue(metadata.preorder_enabled)
        self.assertEqual(
            metadata.upcoming_status,
            DigitalGameUpcomingStatus.PREORDER_OPEN,
        )

    def test_admin_rejects_incoherent_coming_soon_release_information(self):
        game = self.game("Invalid Upcoming", status=ProductStatus.HIDDEN)
        self.client.force_authenticate(self.admin)
        url = f"/api/digital-products/admin/catalog/games/{game.pk}/release-metadata/"
        base = {
            "upcoming_status": DigitalGameUpcomingStatus.COMING_SOON,
            "preorder_enabled": False,
            "preorder_open_at": None,
            "preorder_close_at": None,
            "publish": True,
        }

        missing = self.client.post(url, {**base, "release_date": None}, format="json")
        past = self.client.post(
            url,
            {**base, "release_date": str(date.today() - timedelta(days=1))},
            format="json",
        )

        self.assertEqual(missing.status_code, 400)
        self.assertEqual(past.status_code, 400)
        self.assertFalse(DigitalGameReleaseMetadata.objects.filter(product=game).exists())

    def test_admin_can_assign_digital_authority_from_upcoming_readiness(self):
        game = self.game(
            "Authority Ready Upcoming",
            commerce_authority=ProductCommerceAuthority.STANDARD_COMMERCE,
        )
        self.metadata(
            game,
            release_date=date.today() + timedelta(days=30),
        )
        self.client.force_authenticate(self.admin)

        detail = self.client.get(
            f"/api/digital-products/admin/catalog/games/{game.pk}/"
        )
        activation = self.client.post(
            f"/api/digital-products/admin/catalog/games/{game.pk}/activate-digital/",
            {},
            format="json",
        )

        self.assertFalse(detail.data["upcoming_readiness"]["ready_for_publication"])
        self.assertTrue(detail.data["upcoming_readiness"]["ready_for_authority"])
        self.assertFalse(detail.data["readiness"]["ready"])
        self.assertEqual(activation.status_code, 200)
        self.assertEqual(activation.data["game"]["commerce_authority"], "digital_game")
        self.assertTrue(
            activation.data["upcoming_readiness"]["ready_for_publication"]
        )
        self.assertFalse(activation.data["purchase_readiness"]["ready_for_purchase"])
        self.assertFalse(DigitalOffer.objects.filter(delivered_version__product=game).exists())

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

        self.metadata(
            game,
            release_date=date.today() + timedelta(days=20),
        )
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
