from decimal import Decimal
from uuid import uuid4

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from drf_spectacular.generators import SchemaGenerator
from rest_framework.test import APIClient

from cheatgame.digital_products.models import (
    DigitalCartFulfillmentMethod,
    DigitalInventoryReservation,
    DigitalInventoryReservationState,
    DigitalOffer,
    DigitalOfferCapacity,
    DigitalOfferSaleState,
    InventoryPool,
    InventoryPoolStatus,
)
from cheatgame.digital_products.services.cart import add_digital_offer_to_cart
from cheatgame.digital_products.services.checkout_preparation import prepare_digital_checkout
from cheatgame.product.models import (
    DeliveredVersion,
    NativeConsole,
    Product,
    ProductCommerceAuthority,
    ProductStatus,
    ProductType,
)
from cheatgame.shop.models import Cart
from cheatgame.users.models import BaseUser, UserTypes


class PublicDigitalCatalogApiTests(TestCase):
    list_url = "/api/digital-products/catalog/games/"

    def setUp(self):
        self.client = APIClient()

    def product(
        self,
        title,
        *,
        authority=ProductCommerceAuthority.DIGITAL_PRODUCTS,
        product_type=ProductType.GAME,
        status=ProductStatus.PUBLISHED,
        price=Decimal("1"),
        quantity=999,
    ):
        return Product.objects.create(
            product_type=product_type,
            commerce_authority=authority,
            title=title,
            status=status,
            main_image="tests/catalog-cover.png",
            description="tests/catalog-description.html",
            meta_description=f"{title} summary",
            price=price,
            off_price=price,
            quantity=quantity,
        )

    def offer(
        self,
        product,
        *,
        native_console=NativeConsole.PS4,
        customer_console=NativeConsole.PS4,
        capacity=DigitalOfferCapacity.CAPACITY_2,
        price=Decimal("450000"),
        quantity=3,
        sale_state=DigitalOfferSaleState.ACTIVE,
        pool=None,
    ):
        version = DeliveredVersion.objects.filter(
            product=product, native_console=native_console, is_active=True
        ).first() or DeliveredVersion.objects.create(
            product=product, native_console=native_console
        )
        pool = pool or InventoryPool.objects.create(
            sellable_quantity=quantity, status=InventoryPoolStatus.ENABLED
        )
        return DigitalOffer.objects.create(
            delivered_version=version,
            customer_console=customer_console,
            capacity=capacity,
            price=price,
            inventory_pool=pool,
            sale_state=sale_state,
        )

    def customer(self, suffix):
        user = BaseUser.objects.create_user(
            phone_number=f"0912000{suffix:04d}",
            firstname="Catalog",
            lastname=str(suffix),
            password="test-only",
            user_type=UserTypes.CUSTOMER,
        )
        user.phone_verified = True
        user.save(update_fields=("phone_verified", "updated_at"))
        return user

    def reserve(self, offer, *, suffix, state):
        user = self.customer(suffix)
        cart = Cart.objects.create(user=user)
        method = (
            DigitalCartFulfillmentMethod.IN_STORE
            if offer.capacity == DigitalOfferCapacity.CAPACITY_1
            else DigitalCartFulfillmentMethod.REMOTE
        )
        add_digital_offer_to_cart(cart=cart, offer=offer, fulfillment_method=method, actor=user)
        checkout, _ = prepare_digital_checkout(actor=user, client_checkout_uuid=uuid4())
        reservation = DigitalInventoryReservation.objects.get(checkout=checkout)
        if reservation.state != state:
            reservation.state = state
            reservation.save(update_fields=("state", "updated_at"))
        return reservation

    def test_public_eligibility_and_standard_catalog_isolation(self):
        eligible = self.product("Eligible Digital")
        self.offer(eligible)

        standard = self.product(
            "Standard Game", authority=ProductCommerceAuthority.STANDARD_COMMERCE
        )
        non_game = self.product(
            "Physical Product",
            authority=ProductCommerceAuthority.STANDARD_COMMERCE,
            product_type=ProductType.PHYSCIAL,
        )
        unpublished = self.product("Hidden Digital", status=ProductStatus.HIDDEN)
        self.offer(unpublished)
        without_offer = self.product("No Offer Digital")
        draft_offer_product = self.product("Draft Offer Digital")
        self.offer(draft_offer_product, sale_state=DigitalOfferSaleState.DRAFT)
        archived_version_product = self.product("Archived Version Digital")
        archived_offer = self.offer(archived_version_product)
        DeliveredVersion.objects.filter(pk=archived_offer.delivered_version_id).update(is_active=False)

        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["id"] for row in response.data["results"]], [eligible.pk])
        returned = str(response.data)
        for product in (standard, non_game, unpublished, without_offer, draft_offer_product, archived_version_product):
            self.assertNotIn(product.title, returned)

    def test_offer_price_is_authoritative_and_distinct_versions_remain_distinct(self):
        product = self.product("Version Matrix", price=Decimal("99999999"), quantity=700)
        ps4 = self.offer(
            product,
            native_console=NativeConsole.PS4,
            customer_console=NativeConsole.PS4,
            price=Decimal("450000"),
        )
        self.offer(
            product,
            native_console=NativeConsole.PS4,
            customer_console=NativeConsole.PS5,
            price=Decimal("475000"),
        )
        self.offer(
            product,
            native_console=NativeConsole.PS5,
            customer_console=NativeConsole.PS5,
            price=Decimal("650000"),
        )

        list_response = self.client.get(self.list_url)
        row = list_response.data["results"][0]
        self.assertEqual(Decimal(row["starting_price"]), ps4.price)
        self.assertEqual(row["currency"], "IRT")
        self.assertNotEqual(Decimal(row["starting_price"]), product.price)

        detail = self.client.get(f"{self.list_url}{product.slug}/")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(len(detail.data["offers"]), 3)
        self.assertEqual(
            {(row["customer_console"], row["native_console"], row["price"]) for row in detail.data["offers"]},
            {
                ("ps4", "ps4", Decimal("450000")),
                ("ps5", "ps4", Decimal("475000")),
                ("ps5", "ps5", Decimal("650000")),
            },
        )
        compatible = next(
            row for row in detail.data["offers"]
            if row["customer_console"] == "ps5" and row["native_console"] == "ps4"
        )
        self.assertEqual(compatible["compatibility_code"], "ps4_on_ps5_backward_compatible_v1")
        self.assertIn("Native PS5 نیست", compatible["compatibility_disclosure"])

    def test_capacity_methods_are_customer_safe_and_backend_authoritative(self):
        product = self.product("Capacity Matrix")
        self.offer(product, capacity=DigitalOfferCapacity.CAPACITY_1)
        self.offer(
            product,
            capacity=DigitalOfferCapacity.CAPACITY_2,
            customer_console=NativeConsole.PS5,
        )
        response = self.client.get(f"{self.list_url}{product.slug}/")
        by_capacity = {row["capacity"]: row for row in response.data["offers"]}
        self.assertEqual(
            [value["code"] for value in by_capacity["capacity_1"]["allowed_fulfillment_methods"]],
            ["in_store"],
        )
        self.assertEqual(
            [value["code"] for value in by_capacity["capacity_2"]["allowed_fulfillment_methods"]],
            ["in_store", "remote"],
        )

    def test_effective_reservations_and_shared_pool_drive_availability(self):
        product = self.product("Reserved Digital")
        shared_pool = InventoryPool.objects.create(
            sellable_quantity=3, status=InventoryPoolStatus.ENABLED
        )
        ps4 = self.offer(product, pool=shared_pool)
        ps5 = self.offer(
            product,
            pool=shared_pool,
            customer_console=NativeConsole.PS5,
        )
        for suffix, state in enumerate(
            (
                DigitalInventoryReservationState.ACTIVE,
                DigitalInventoryReservationState.PAYMENT_HOLD,
                DigitalInventoryReservationState.HELD_FOR_REVIEW,
            ),
            start=1,
        ):
            self.reserve(ps4, suffix=suffix, state=state)

        unrelated_product = self.product("Unrelated Reserved Digital")
        unrelated = self.offer(unrelated_product, quantity=1)
        self.reserve(
            unrelated,
            suffix=20,
            state=DigitalInventoryReservationState.ACTIVE,
        )

        response = self.client.get(f"{self.list_url}{product.slug}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["availability"], "SOLD_OUT")
        self.assertFalse(response.data["is_available"])
        self.assertEqual({row["availability"] for row in response.data["offers"]}, {"SOLD_OUT"})
        self.assertEqual({row["id"] for row in response.data["offers"]}, {ps4.pk, ps5.pk})

    def test_paused_pool_is_not_customer_visible(self):
        product = self.product("Paused Pool Digital")
        offer = self.offer(product)
        InventoryPool.objects.filter(pk=offer.inventory_pool_id).update(status=InventoryPoolStatus.PAUSED)
        response = self.client.get(self.list_url)
        self.assertEqual(response.data["results"], [])

    def test_filters_ordering_and_pagination(self):
        alpha = self.product("Alpha PS4")
        self.offer(alpha, customer_console=NativeConsole.PS4, capacity=DigitalOfferCapacity.CAPACITY_1, price=300)
        beta = self.product("Beta PS5")
        self.offer(beta, customer_console=NativeConsole.PS5, capacity=DigitalOfferCapacity.CAPACITY_3, price=100)
        gamma = self.product("Gamma PS4")
        self.offer(gamma, customer_console=NativeConsole.PS4, capacity=DigitalOfferCapacity.CAPACITY_2, price=200)

        self.assertEqual(
            self.client.get(self.list_url, {"search": "beta"}).data["results"][0]["id"],
            beta.pk,
        )
        console_rows = self.client.get(self.list_url, {"console": "ps4"}).data["results"]
        self.assertEqual({row["id"] for row in console_rows}, {alpha.pk, gamma.pk})
        capacity_rows = self.client.get(self.list_url, {"capacity": "capacity_3"}).data["results"]
        self.assertEqual([row["id"] for row in capacity_rows], [beta.pk])
        ordered = self.client.get(self.list_url, {"ordering": "minimum_price"}).data["results"]
        self.assertEqual([row["id"] for row in ordered], [beta.pk, gamma.pk, alpha.pk])
        paginated = self.client.get(self.list_url, {"ordering": "title", "limit": 1, "offset": 1})
        self.assertEqual((paginated.data["limit"], paginated.data["offset"], paginated.data["count"]), (1, 1, 3))
        self.assertEqual(paginated.data["results"][0]["id"], beta.pk)

    def test_invalid_filters_have_stable_errors(self):
        for parameters, field in (
            ({"console": "xbox"}, "console"),
            ({"ordering": "price_desc"}, "ordering"),
            ({"limit": 0}, "limit"),
            ({"unknown": "value"}, "unknown"),
        ):
            with self.subTest(parameters=parameters):
                response = self.client.get(self.list_url, parameters)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.data["code"], "invalid_request")
                self.assertIn(field, response.data["fields"])

    def test_detail_404_and_get_only_contract(self):
        standard = self.product(
            "Standard Detail", authority=ProductCommerceAuthority.STANDARD_COMMERCE
        )
        missing = self.client.get(f"{self.list_url}{standard.slug}/")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.data["code"], "digital_game_not_found")
        post = self.client.post(self.list_url, {}, format="json")
        self.assertEqual(post.status_code, 405)
        self.assertEqual(post.data["code"], "method_not_allowed")

    def test_public_privacy_contract_excludes_internal_and_legacy_truth(self):
        product = self.product("Private Fields", price=987654321, quantity=4321)
        self.offer(product)
        response = self.client.get(f"{self.list_url}{product.slug}/")
        payload = response.data
        serialized = str(payload).lower()
        for prohibited in (
            "inventory_pool",
            "sellable_quantity",
            "held_quantity",
            "reservation",
            "readiness",
            "sale_state",
            "off_price",
            "product.quantity",
            "attachment",
            "provider",
            "payment",
        ):
            self.assertNotIn(prohibited, serialized)
        self.assertNotIn("quantity", payload)
        self.assertNotIn("price", payload)

    def test_list_query_budget_is_constant_for_multiple_games_and_offers(self):
        for index in range(4):
            product = self.product(f"Budget Game {index}")
            self.offer(product, customer_console=NativeConsole.PS4, price=100 + index)
            self.offer(product, customer_console=NativeConsole.PS5, price=200 + index)
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(self.list_url, {"limit": 10})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 4)
        data_queries = [
            query["sql"] for query in queries
            if not query["sql"].startswith(("SAVEPOINT", "RELEASE SAVEPOINT"))
        ]
        self.assertLessEqual(len(data_queries), 4, data_queries)

    def test_openapi_has_explicit_public_catalog_operations(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        paths = schema["paths"]
        list_path = "/api/digital-products/catalog/games/"
        detail_path = "/api/digital-products/catalog/games/{slug}/"
        self.assertIn(list_path, paths)
        self.assertIn(detail_path, paths)
        self.assertEqual(set(paths[list_path]), {"get"})
        self.assertEqual(set(paths[detail_path]), {"get"})
        parameter_names = {parameter["name"] for parameter in paths[list_path]["get"]["parameters"]}
        self.assertTrue({"search", "console", "capacity", "ordering", "limit", "offset"}.issubset(parameter_names))
