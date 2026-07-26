from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase
from rest_framework.test import APIClient

from cheatgame.digital_products.models import (
    DigitalOfferCapacity,
    DigitalOfferSaleState,
    InventoryPoolStatus,
    PoolStockAdjustment,
    PoolStockAdjustmentReason,
)
from cheatgame.digital_products.services.offers import create_digital_offer
from cheatgame.product.models import (
    Attachment,
    AttachmentType,
    DeliveredVersion,
    NativeConsole,
    Product,
    ProductCommerceAuthority,
    ProductStatus,
    ProductType,
)
from cheatgame.users.models import BaseUser, UserTypes


class AdminDigitalCatalogContractTests(TestCase):
    root = "/api/digital-products/admin/catalog"

    def setUp(self):
        self.admin = self.user("09127777001", UserTypes.ADMIN)
        self.manager = self.user("09127777002", UserTypes.MANAGER)
        self.customer = self.user("09127777003", UserTypes.CUSTOMER)
        self.game = self.product("Admin Catalog Game")
        self.version = DeliveredVersion.objects.create(
            product=self.game,
            native_console=NativeConsole.PS4,
        )
        self.offer, self.pool = create_digital_offer(
            delivered_version_id=self.version.pk,
            customer_console=NativeConsole.PS4,
            capacity=DigitalOfferCapacity.CAPACITY_1,
            price="100000",
            initial_stock=5,
            actor=self.manager,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def user(self, phone, user_type):
        return BaseUser.objects.create_user(
            phone_number=phone,
            firstname="Catalog",
            lastname=UserTypes(user_type).name,
            password="Test-only-password-123",
            user_type=user_type,
        )

    def product(self, title, **overrides):
        values = {
            "product_type": ProductType.GAME,
            "title": title,
            "status": ProductStatus.PUBLISHED,
            "main_image": "product/main_images/test.jpg",
            "price": 50000,
            "off_price": 45000,
            "quantity": 2,
            "description": "product/descriptions/test.html",
        }
        values.update(overrides)
        return Product.objects.create(**values)

    def detail_url(self, product=None):
        return f"{self.root}/games/{(product or self.game).pk}/"

    def test_list_matches_admin_contract_and_supports_filters(self):
        other = self.product(
            "Digital Authority Game",
            commerce_authority=ProductCommerceAuthority.DIGITAL_PRODUCTS,
        )
        response = self.client.get(
            f"{self.root}/games/",
            {
                "search": "Admin",
                "commerce_authority": "standard_commerce",
                "offers": "has_offers",
                "readiness": "ready",
                "limit": 10,
                "offset": 0,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        row = response.data["results"][0]
        self.assertEqual(
            set(row),
            {
                "id",
                "title",
                "slug",
                "status",
                "commerce_authority",
                "delivered_version_count",
                "offer_count",
                "active_offer_count",
                "configured_options",
                "readiness",
                "release_metadata",
                "updated_at",
            },
        )
        self.assertEqual(row["commerce_authority"], "standard_commerce")
        self.assertTrue(row["readiness"]["ready"])
        digital = self.client.get(
            f"{self.root}/games/",
            {"commerce_authority": "digital_game"},
        )
        self.assertEqual(digital.data["results"][0]["id"], other.pk)

    def test_detail_composes_frozen_catalog_offer_and_inventory_authorities(self):
        Attachment.objects.create(
            product=self.game,
            attachment_type=AttachmentType.CAPACITY,
            title="Legacy Capacity",
            price=0,
        )
        response = self.client.get(self.detail_url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-store, private")
        self.assertEqual(response.data["game"]["title"], self.game.title)
        self.assertTrue(
            response.data["game"]["legacy_capacity_attachments_present"]
        )
        version = response.data["delivered_versions"][0]
        self.assertEqual(version["display_label"], "PS4")
        self.assertTrue(version["referenced_by_non_archived_offers"])
        offer = response.data["offers"][0]
        self.assertEqual(offer["price"], "100000")
        self.assertEqual(offer["compatibility"], "native_version_v1")
        self.assertEqual(offer["inventory"]["gross_sellable_quantity"], 5)
        self.assertEqual(offer["inventory"]["available_quantity"], 5)
        self.assertEqual(offer["inventory"]["mode"], "independent")
        self.assertCountEqual(
            offer["allowed_actions"],
            [
                "update_price",
                "adjust_stock",
                "change_state",
                "share_stock",
                "independent_stock",
            ],
        )

    def test_manager_and_admin_permissions_remain_service_authoritative(self):
        self.client.force_authenticate(self.customer)
        self.assertEqual(
            self.client.get(f"{self.root}/games/").status_code,
            403,
        )
        self.client.force_authenticate(self.manager)
        create_response = self.client.post(
            f"{self.root}/games/{self.game.pk}/versions/",
            {"native_console": "ps5"},
            format="json",
        )
        self.assertEqual(create_response.status_code, 200)
        forbidden = self.client.post(
            f"{self.root}/games/{self.game.pk}/activate-digital/",
            {},
            format="json",
        )
        self.assertEqual(forbidden.status_code, 403)

    def test_mutation_adapter_calls_existing_price_service(self):
        with patch(
            "cheatgame.digital_products.admin_catalog_apis.update_offer_price"
        ) as update:
            response = self.client.post(
                f"{self.root}/offers/{self.offer.pk}/price/",
                {"price": "120000"},
                format="json",
            )
        self.assertEqual(response.status_code, 200)
        update.assert_called_once_with(
            offer_id=self.offer.pk,
            price=120000,
            actor=self.admin,
        )

    def test_real_commands_return_refreshed_authoritative_detail(self):
        price = self.client.post(
            f"{self.root}/offers/{self.offer.pk}/price/",
            {"price": "120000"},
            format="json",
        )
        self.assertEqual(price.status_code, 200)
        self.assertEqual(price.data["offers"][0]["price"], "120000")

        key = uuid4()
        stock_url = (
            f"{self.root}/offers/{self.offer.pk}/stock-adjustments/"
        )
        payload = {
            "delta": 3,
            "reason": PoolStockAdjustmentReason.INVENTORY_RECEIVED,
            "idempotency_key": str(key),
        }
        first = self.client.post(stock_url, payload, format="json")
        second = self.client.post(stock_url, payload, format="json")
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertEqual(
            first.data["offers"][0]["inventory"],
            second.data["offers"][0]["inventory"],
        )
        self.assertEqual(
            PoolStockAdjustment.objects.filter(idempotency_key=key).count(),
            1,
        )

        activation = self.client.post(
            f"{self.root}/games/{self.game.pk}/activate-digital/",
            {},
            format="json",
        )
        self.assertEqual(activation.status_code, 200)
        self.assertEqual(
            activation.data["game"]["commerce_authority"],
            "digital_game",
        )
        active = self.client.post(
            f"{self.root}/offers/{self.offer.pk}/state/",
            {"sale_state": DigitalOfferSaleState.ACTIVE},
            format="json",
        )
        self.assertEqual(active.status_code, 200)
        self.assertEqual(active.data["offers"][0]["sale_state"], "active")

    def test_offer_create_rejects_foreign_version_and_share_uses_source_pool(self):
        other = self.product("Other Catalog Game")
        other_version = DeliveredVersion.objects.create(
            product=other,
            native_console=NativeConsole.PS4,
        )
        foreign = self.client.post(
            f"{self.root}/games/{self.game.pk}/offers/",
            {
                "delivered_version_id": other_version.pk,
                "customer_console": "ps4",
                "capacity": "capacity_2",
                "price": "90000",
                "initial_stock": 0,
            },
            format="json",
        )
        self.assertEqual(foreign.status_code, 400)

        second, second_pool = create_digital_offer(
            delivered_version_id=self.version.pk,
            customer_console=NativeConsole.PS5,
            capacity=DigitalOfferCapacity.CAPACITY_1,
            price="110000",
            actor=self.manager,
        )
        shared = self.client.post(
            f"{self.root}/offers/{second.pk}/share-stock/",
            {"source_offer_id": self.offer.pk},
            format="json",
        )
        self.assertEqual(shared.status_code, 200)
        second.refresh_from_db()
        second_pool.refresh_from_db()
        self.assertEqual(second.inventory_pool_id, self.pool.pk)
        self.assertEqual(second_pool.sellable_quantity, 0)
        rows = {row["id"]: row for row in shared.data["offers"]}
        self.assertEqual(rows[self.offer.pk]["inventory"]["mode"], "shared")
        self.assertEqual(
            rows[self.offer.pk]["inventory"]["shared_with"][0]["offer_id"],
            second.pk,
        )

    def test_validation_conflict_not_found_and_limit_are_stable(self):
        invalid = self.client.get(f"{self.root}/games/", {"limit": 51})
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.data["code"], "invalid_catalog_filters")
        missing = self.client.get(f"{self.root}/games/999999/")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.data["code"], "catalog_game_not_found")

        conflict = self.client.post(
            f"{self.root}/games/{self.game.pk}/activate-digital/",
            {},
            format="json",
        )
        self.assertEqual(conflict.status_code, 200)
        self.client.post(
            f"{self.root}/offers/{self.offer.pk}/state/",
            {"sale_state": "active"},
            format="json",
        )
        blocked = self.client.post(
            f"{self.root}/games/{self.game.pk}/deactivate-digital/",
            {},
            format="json",
        )
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(
            blocked.data["readiness"]["issues"],
            [
                {
                    "code": "ACTIVE_OFFER",
                    "label": "ابتدا همه گزینه‌های فروش فعال را متوقف کنید.",
                }
            ],
        )

    def test_public_catalog_remains_read_only_and_separate(self):
        public_write = self.client.post(
            "/api/digital-products/catalog/games/",
            {},
            format="json",
        )
        self.assertEqual(public_write.status_code, 405)
        self.assertEqual(
            public_write.data["code"],
            "method_not_allowed",
        )
