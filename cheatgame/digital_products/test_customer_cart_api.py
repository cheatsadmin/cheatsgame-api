from decimal import Decimal
from threading import Barrier, Lock, Thread
from unittest import skipUnless
from unittest.mock import patch

from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from drf_spectacular.generators import SchemaGenerator
from rest_framework.test import APIClient

from cheatgame.digital_products.models import (
    DigitalCartFulfillmentMethod,
    DigitalCartSelection,
    DigitalFulfillmentItem,
    DigitalInventoryReservation,
    DigitalOffer,
    DigitalOfferCapacity,
    DigitalOfferSaleState,
    Entitlement,
    InventoryPool,
    InventoryPoolStatus,
)
from cheatgame.digital_products.services.cart import add_digital_offer_to_cart
from cheatgame.financial_core.models import (
    CommercialFinalization,
    DigitalFulfillmentObligation,
    Payment,
    PaymentAttempt,
)
from cheatgame.product.models import (
    DeliveredVersion,
    NativeConsole,
    Product,
    ProductCommerceAuthority,
    ProductStatus,
    ProductType,
)
from cheatgame.shop.models import (
    Cart,
    CartItem,
    CartState,
    Checkout,
    Order,
    OrderItem,
    PaymentTransaction,
    StockReservation,
)
from cheatgame.users.models import BaseUser, UserTypes


class CustomerDigitalCartApiTests(TestCase):
    add_url = "/api/digital-products/customer/cart/items/"
    cart_url = "/api/shop/cart-item-list/"

    def setUp(self):
        self.client = APIClient()
        self.customer = self.user("09125550001")
        self.client.force_authenticate(self.customer)

    def user(self, phone, *, user_type=UserTypes.CUSTOMER, active=True, verified=True):
        user = BaseUser.objects.create_user(
            phone_number=phone,
            firstname="Digital",
            lastname="Customer",
            password="test-only",
            user_type=user_type,
            is_active=active,
        )
        user.phone_verified = verified
        user.save(update_fields=("phone_verified", "updated_at"))
        return user

    def product(
        self,
        title,
        *,
        authority=ProductCommerceAuthority.DIGITAL_PRODUCTS,
        product_type=ProductType.GAME,
        status=ProductStatus.PUBLISHED,
        price=Decimal("999999"),
    ):
        return Product.objects.create(
            product_type=product_type,
            commerce_authority=authority,
            status=status,
            title=title,
            main_image="tests/digital-cart-cover.png",
            description="tests/digital-cart-description.html",
            price=price,
            off_price=price,
            quantity=777,
            order_limit=5,
        )

    def offer(
        self,
        product,
        *,
        customer_console=NativeConsole.PS4,
        native_console=NativeConsole.PS4,
        capacity=DigitalOfferCapacity.CAPACITY_2,
        price=Decimal("450000"),
        quantity=3,
        pool=None,
        sale_state=DigitalOfferSaleState.ACTIVE,
    ):
        version = DeliveredVersion.objects.filter(
            product=product,
            native_console=native_console,
            is_active=True,
        ).first() or DeliveredVersion.objects.create(
            product=product,
            native_console=native_console,
        )
        pool = pool or InventoryPool.objects.create(
            sellable_quantity=quantity,
            status=InventoryPoolStatus.ENABLED,
        )
        return DigitalOffer.objects.create(
            delivered_version=version,
            customer_console=customer_console,
            capacity=capacity,
            price=price,
            inventory_pool=pool,
            sale_state=sale_state,
        )

    def add(self, offer, method=DigitalCartFulfillmentMethod.REMOTE, **extra):
        payload = {"offer_id": offer.pk, "fulfillment_method": method, **extra}
        return self.client.post(self.add_url, payload, format="json")

    def method_url(self, item):
        return f"{self.add_url}{item.pk}/fulfillment-method/"

    def item_url(self, item):
        return f"{self.add_url}{item.pk}/"

    def test_authentication_active_customer_and_method_boundary(self):
        product = self.product("Permission Game")
        offer = self.offer(product)
        self.client.force_authenticate(user=None)
        self.assertEqual(self.add(offer).status_code, 401)

        inactive = self.user("09125550002", active=False)
        self.client.force_authenticate(inactive)
        self.assertEqual(self.add(offer).status_code, 403)

        unverified = self.user("09125550004", verified=False)
        self.client.force_authenticate(unverified)
        self.assertEqual(self.add(offer).status_code, 403)

        manager = self.user("09125550003", user_type=UserTypes.MANAGER)
        self.client.force_authenticate(manager)
        denied = self.add(offer)
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.data["code"], "digital_cart_permission_denied")

        admin = self.user("09125550005", user_type=UserTypes.ADMIN)
        self.client.force_authenticate(admin)
        self.assertEqual(self.add(offer).status_code, 403)

        self.client.force_authenticate(self.customer)
        rejected = self.client.get(self.add_url)
        self.assertEqual(rejected.status_code, 405)
        self.assertEqual(rejected.data["code"], "method_not_allowed")

    def test_add_exact_offer_uses_offer_price_and_has_no_downstream_side_effect(self):
        product = self.product("Exact Offer", price=Decimal("9000000"))
        offer = self.offer(product, price=Decimal("510000"))
        response = self.add(offer)
        self.assertEqual(response.status_code, 201, response.data)
        item = CartItem.objects.get(pk=response.data["id"])
        selection = DigitalCartSelection.objects.get(cart_item=item)
        self.assertEqual(item.commerce_authority, ProductCommerceAuthority.DIGITAL_PRODUCTS)
        self.assertEqual((item.quantity, item.price), (1, offer.price))
        self.assertEqual((selection.offer_id, selection.fulfillment_method), (offer.pk, "remote"))
        self.assertEqual(response.data["commerce_authority"], "DIGITAL_PRODUCTS")
        self.assertEqual(Decimal(response.data["digital_selection"]["unit_price"]), offer.price)
        self.assertNotEqual(item.price, product.price)
        self.assertEqual(DigitalInventoryReservation.objects.count(), 0)
        self.assertEqual(StockReservation.objects.count(), 0)
        self.assertEqual(Checkout.objects.count(), 0)
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(OrderItem.objects.count(), 0)
        self.assertEqual(PaymentTransaction.objects.count(), 0)
        self.assertEqual(Payment.objects.count(), 0)
        self.assertEqual(PaymentAttempt.objects.count(), 0)
        self.assertEqual(CommercialFinalization.objects.count(), 0)
        self.assertEqual(DigitalFulfillmentObligation.objects.count(), 0)
        self.assertEqual(DigitalFulfillmentItem.objects.count(), 0)
        self.assertEqual(Entitlement.objects.count(), 0)
        offer.inventory_pool.refresh_from_db()
        self.assertEqual(offer.inventory_pool.sellable_quantity, 3)

    def test_locked_add_revalidates_product_publication_after_public_lookup(self):
        product = self.product("Publication Drift")
        offer = self.offer(product)

        def unpublish_then_run_locked_command(**kwargs):
            Product.objects.filter(pk=product.pk).update(status=ProductStatus.HIDDEN)
            return add_digital_offer_to_cart(**kwargs)

        with patch(
            "cheatgame.digital_products.customer_cart_apis.add_digital_offer_to_cart",
            side_effect=unpublish_then_run_locked_command,
        ):
            response = self.add(offer)

        self.assertEqual(
            (response.status_code, response.data["code"]),
            (409, "digital_offer_unavailable"),
        )
        self.assertFalse(CartItem.objects.exists())
        self.assertFalse(DigitalCartSelection.objects.exists())
        self.assertFalse(DigitalInventoryReservation.objects.exists())
        self.assertFalse(Checkout.objects.exists())
        self.assertFalse(Order.objects.exists())
        self.assertFalse(Payment.objects.exists())
        self.assertFalse(DigitalFulfillmentItem.objects.exists())
        self.assertFalse(Entitlement.objects.exists())

    def test_add_rejects_unknown_and_client_authority_fields(self):
        product = self.product("Strict Input")
        offer = self.offer(product)
        for field, value in (
            ("price", 1),
            ("quantity", 9),
            ("product_id", product.pk),
            ("customer_console", "ps5"),
            ("capacity", "capacity_3"),
            ("delivered_version", "ps5"),
            ("inventory_pool", offer.inventory_pool_id),
            ("customer_id", self.customer.pk),
        ):
            with self.subTest(field=field):
                response = self.add(offer, **{field: value})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.data["code"], "invalid_request")
                self.assertIn(field, response.data["fields"])
        self.assertFalse(CartItem.objects.exists())

    def test_add_revalidates_public_offer_graph_and_availability(self):
        sold_out = self.offer(self.product("Sold Out"), quantity=0)
        response = self.add(sold_out)
        self.assertEqual((response.status_code, response.data["code"]), (409, "digital_offer_unavailable"))

        draft = self.offer(
            self.product("Draft Offer"),
            sale_state=DigitalOfferSaleState.DRAFT,
        )
        self.assertEqual(self.add(draft).status_code, 404)

        hidden_product = self.product("Hidden Game", status=ProductStatus.HIDDEN)
        hidden = self.offer(hidden_product)
        self.assertEqual(self.add(hidden).status_code, 404)

        inactive_version = self.offer(self.product("Inactive Version"))
        DeliveredVersion.objects.filter(pk=inactive_version.delivered_version_id).update(is_active=False)
        self.assertEqual(self.add(inactive_version).status_code, 404)

        paused_pool = self.offer(self.product("Paused Pool"))
        InventoryPool.objects.filter(pk=paused_pool.inventory_pool_id).update(
            status=InventoryPoolStatus.PAUSED
        )
        self.assertEqual(self.add(paused_pool).status_code, 404)

        incoherent = self.offer(self.product("Incoherent Version"))
        DeliveredVersion.objects.filter(pk=incoherent.delivered_version_id).update(
            native_console=NativeConsole.PS5
        )
        self.assertEqual(self.add(incoherent).status_code, 404)

    def test_capacity_one_and_invalid_methods_are_rejected(self):
        offer = self.offer(
            self.product("Capacity One"),
            capacity=DigitalOfferCapacity.CAPACITY_1,
        )
        remote = self.add(offer)
        self.assertEqual(remote.status_code, 409)
        self.assertEqual(remote.data["code"], "digital_fulfillment_method_not_allowed")
        invalid = self.client.post(
            self.add_url,
            {"offer_id": offer.pk, "fulfillment_method": "credentials"},
            format="json",
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertIn("fulfillment_method", invalid.data["fields"])
        allowed = self.add(offer, DigitalCartFulfillmentMethod.IN_STORE)
        self.assertEqual(allowed.status_code, 201)

    def test_duplicate_and_distinct_offer_policy(self):
        product = self.product("Offer Matrix")
        shared = InventoryPool.objects.create(
            sellable_quantity=5,
            status=InventoryPoolStatus.ENABLED,
        )
        first = self.offer(product, pool=shared)
        second = self.offer(
            product,
            pool=shared,
            customer_console=NativeConsole.PS5,
            price=Decimal("550000"),
        )
        created = self.add(first)
        self.assertEqual(created.status_code, 201)
        identical = self.add(first)
        self.assertEqual((identical.status_code, identical.data["code"]), (409, "digital_cart_selection_conflict"))
        different_method = self.add(first, DigitalCartFulfillmentMethod.IN_STORE)
        self.assertEqual(different_method.status_code, 409)
        distinct = self.add(second)
        self.assertEqual(distinct.status_code, 201)
        self.assertEqual(CartItem.objects.count(), 2)
        self.assertEqual({item.quantity for item in CartItem.objects.all()}, {1})

    def test_mixed_and_locked_cart_add_are_rejected(self):
        standard = self.product(
            "Standard",
            authority=ProductCommerceAuthority.STANDARD_COMMERCE,
            product_type=ProductType.PHYSCIAL,
        )
        cart = Cart.objects.create(user=self.customer)
        CartItem.objects.create(cart=cart, product=standard, price=standard.price, quantity=1)
        offer = self.offer(self.product("Digital in Mixed"))
        mixed = self.add(offer)
        self.assertEqual((mixed.status_code, mixed.data["code"]), (409, "mixed_commerce_authority"))

        CartItem.objects.all().delete()
        cart.state = CartState.LOCKED
        cart.save(update_fields=("state", "updated_at"))
        locked = self.add(offer)
        self.assertEqual((locked.status_code, locked.data["code"]), (409, "digital_cart_locked"))

    def test_method_change_is_owned_idempotent_and_identity_preserving(self):
        offer = self.offer(self.product("Method Change"), price=Decimal("630000"))
        created = self.add(offer)
        item = CartItem.objects.get(pk=created.data["id"])
        original = (item.product_id, item.price, item.quantity)
        changed = self.client.patch(
            self.method_url(item),
            {"fulfillment_method": "in_store"},
            format="json",
        )
        self.assertEqual(changed.status_code, 200, changed.data)
        replay = self.client.patch(
            self.method_url(item),
            {"fulfillment_method": "in_store"},
            format="json",
        )
        self.assertEqual(replay.status_code, 200)
        item.refresh_from_db()
        self.assertEqual((item.product_id, item.price, item.quantity), original)
        self.assertEqual(item.digital_selection.fulfillment_method, "in_store")
        invalid = self.client.patch(
            self.method_url(item),
            {"fulfillment_method": "invalid"},
            format="json",
        )
        self.assertEqual(invalid.status_code, 400)

    def test_method_change_capacity_one_locked_and_cross_customer(self):
        offer = self.offer(
            self.product("Method Restrictions"),
            capacity=DigitalOfferCapacity.CAPACITY_1,
        )
        item_id = self.add(offer, DigitalCartFulfillmentMethod.IN_STORE).data["id"]
        item = CartItem.objects.get(pk=item_id)
        remote = self.client.patch(
            self.method_url(item),
            {"fulfillment_method": "remote"},
            format="json",
        )
        self.assertEqual(remote.status_code, 409)

        other = self.user("09125550009")
        self.client.force_authenticate(other)
        hidden = self.client.patch(
            self.method_url(item),
            {"fulfillment_method": "in_store"},
            format="json",
        )
        self.assertEqual(hidden.status_code, 404)

        self.client.force_authenticate(self.customer)
        item.cart.state = CartState.LOCKED
        item.cart.save(update_fields=("state", "updated_at"))
        locked = self.client.patch(
            self.method_url(item),
            {"fulfillment_method": "in_store"},
            format="json",
        )
        self.assertEqual((locked.status_code, locked.data["code"]), (409, "digital_cart_locked"))

    def test_delete_owned_standard_cross_customer_locked_and_repeat_contract(self):
        offer = self.offer(self.product("Delete Digital"))
        item = CartItem.objects.get(pk=self.add(offer).data["id"])
        other = self.user("09125550010")
        self.client.force_authenticate(other)
        self.assertEqual(self.client.delete(self.item_url(item)).status_code, 404)

        self.client.force_authenticate(self.customer)
        removed = self.client.delete(self.item_url(item))
        self.assertEqual((removed.status_code, removed.data["removed"]), (200, True))
        self.assertEqual(self.client.delete(self.item_url(item)).status_code, 404)

        standard = self.product(
            "Delete Standard",
            authority=ProductCommerceAuthority.STANDARD_COMMERCE,
            product_type=ProductType.PHYSCIAL,
        )
        cart = Cart.objects.get(user=self.customer)
        standard_item = CartItem.objects.create(
            cart=cart,
            product=standard,
            price=standard.price,
            quantity=1,
        )
        self.assertEqual(self.client.delete(self.item_url(standard_item)).status_code, 409)

        standard_item.delete()
        locked_offer = self.offer(self.product("Locked Delete"))
        locked_item = CartItem.objects.get(pk=self.add(locked_offer).data["id"])
        cart.state = CartState.LOCKED
        cart.save(update_fields=("state", "updated_at"))
        locked = self.client.delete(self.item_url(locked_item))
        self.assertEqual((locked.status_code, locked.data["code"]), (409, "digital_cart_locked"))
        self.assertTrue(CartItem.objects.filter(pk=locked_item.pk).exists())

    def test_authority_aware_cart_projection_preserves_standard_and_hides_digital_legacy_truth(self):
        digital_product = self.product("Projected Digital", price=Decimal("99999999"))
        offer = self.offer(digital_product, price=Decimal("440000"))
        digital_item = CartItem.objects.get(pk=self.add(offer).data["id"])
        standard = self.product(
            "Projected Standard",
            authority=ProductCommerceAuthority.STANDARD_COMMERCE,
            product_type=ProductType.PHYSCIAL,
            price=Decimal("120000"),
        )
        standard_item = CartItem.objects.create(
            cart=digital_item.cart,
            product=standard,
            quantity=2,
            price=Decimal("240000"),
        )
        response = self.client.get(self.cart_url)
        self.assertEqual(response.status_code, 200, response.data)
        rows = {row["id"]: row for row in response.data}
        standard_row = rows[standard_item.pk]
        self.assertEqual(standard_row["commerce_authority"], "STANDARD_COMMERCE")
        self.assertIsNone(standard_row["digital_selection"])
        self.assertEqual(Decimal(standard_row["product"]["price"]), standard.price)
        self.assertEqual(standard_row["product"]["quantity"], standard.quantity)

        digital_row = rows[digital_item.pk]
        self.assertEqual(digital_row["commerce_authority"], "DIGITAL_PRODUCTS")
        self.assertNotIn("price", digital_row["product"])
        self.assertNotIn("off_price", digital_row["product"])
        self.assertNotIn("quantity", digital_row["product"])
        selection = digital_row["digital_selection"]
        self.assertEqual(selection["offer_id"], offer.pk)
        self.assertEqual(Decimal(selection["unit_price"]), offer.price)
        serialized = str(selection).lower()
        for prohibited in (
            "inventory_pool",
            "sellable_quantity",
            "held_quantity",
            "reservation",
            "sale_state",
            "readiness",
            "provider",
            "payment",
            "entitlement",
        ):
            self.assertNotIn(prohibited, serialized)

    def test_cart_projection_fails_closed_for_contradictory_digital_item(self):
        product = self.product("Contradictory Digital")
        cart = Cart.objects.create(user=self.customer)
        CartItem.objects.create(
            cart=cart,
            product=product,
            quantity=1,
            price=Decimal("1"),
            commerce_authority=ProductCommerceAuthority.DIGITAL_PRODUCTS,
        )
        response = self.client.get(self.cart_url)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "digital_cart_integrity_conflict")

    def test_mixed_cart_projection_query_budget_is_constant(self):
        cart = Cart.objects.create(user=self.customer)
        for index in range(3):
            standard = self.product(
                f"Budget Standard {index}",
                authority=ProductCommerceAuthority.STANDARD_COMMERCE,
                product_type=ProductType.PHYSCIAL,
            )
            CartItem.objects.create(cart=cart, product=standard, price=standard.price, quantity=1)
        for index in range(3):
            product = self.product(f"Budget Digital {index}")
            offer = self.offer(product, price=Decimal(500000 + index))
            item = CartItem.objects.create(
                cart=cart,
                product=product,
                price=offer.price,
                quantity=1,
                commerce_authority=ProductCommerceAuthority.DIGITAL_PRODUCTS,
            )
            DigitalCartSelection.objects.create(
                cart_item=item,
                offer=offer,
                fulfillment_method=DigitalCartFulfillmentMethod.REMOTE,
            )
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(self.cart_url)
        self.assertEqual((response.status_code, len(response.data)), (200, 6))
        data_queries = [
            query["sql"]
            for query in queries
            if not query["sql"].startswith(("SAVEPOINT", "RELEASE SAVEPOINT"))
        ]
        self.assertLessEqual(len(data_queries), 4, data_queries)

    def test_openapi_declares_customer_cart_mutations_and_adapted_cart_read(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        paths = schema["paths"]
        add_path = "/api/digital-products/customer/cart/items/"
        item_path = "/api/digital-products/customer/cart/items/{cart_item_id}/"
        method_path = f"{item_path}fulfillment-method/"
        self.assertEqual(set(paths[add_path]), {"post"})
        self.assertEqual(set(paths[item_path]), {"delete"})
        self.assertEqual(set(paths[method_path]), {"patch"})
        self.assertEqual(
            paths[add_path]["post"]["operationId"],
            "digital_customer_cart_item_add",
        )
        self.assertIn("/api/shop/cart-item-list/", paths)


@skipUnless(connection.vendor == "postgresql", "Cart acquisition locking requires PostgreSQL.")
class CustomerDigitalCartConcurrencyTests(TransactionTestCase):
    reset_sequences = True
    add_url = "/api/digital-products/customer/cart/items/"
    standard_add_url = "/api/shop/add-to-cart/"

    def setUp(self):
        self.customer = BaseUser.objects.create_user(
            phone_number="09125550991",
            firstname="Concurrent",
            lastname="Customer",
            password="test-only",
            user_type=UserTypes.CUSTOMER,
            is_active=True,
        )
        self.customer.phone_verified = True
        self.customer.save(update_fields=("phone_verified", "updated_at"))
        self.digital_product = Product.objects.create(
            product_type=ProductType.GAME,
            commerce_authority=ProductCommerceAuthority.DIGITAL_PRODUCTS,
            status=ProductStatus.PUBLISHED,
            title="Concurrent Digital",
            main_image="tests/concurrent-digital.png",
            description="tests/concurrent-digital.html",
            price=Decimal("999999"),
            off_price=Decimal("999999"),
            quantity=777,
            order_limit=5,
        )
        version = DeliveredVersion.objects.create(
            product=self.digital_product,
            native_console=NativeConsole.PS4,
        )
        pool = InventoryPool.objects.create(
            sellable_quantity=5,
            status=InventoryPoolStatus.ENABLED,
        )
        self.offer = DigitalOffer.objects.create(
            delivered_version=version,
            customer_console=NativeConsole.PS4,
            capacity=DigitalOfferCapacity.CAPACITY_2,
            price=Decimal("450000"),
            inventory_pool=pool,
            sale_state=DigitalOfferSaleState.ACTIVE,
        )
        self.standard_product = Product.objects.create(
            product_type=ProductType.PHYSCIAL,
            commerce_authority=ProductCommerceAuthority.STANDARD_COMMERCE,
            status=ProductStatus.PUBLISHED,
            title="Concurrent Standard",
            main_image="tests/concurrent-standard.png",
            description="tests/concurrent-standard.html",
            price=Decimal("120000"),
            off_price=Decimal("120000"),
            quantity=10,
            order_limit=5,
        )

    def _parallel(self, operations):
        barrier = Barrier(len(operations))
        mutex = Lock()
        outcomes = []

        def worker(operation):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                response = operation()
                result = (
                    "response",
                    response.status_code,
                    getattr(response, "data", {}).get("code"),
                )
            except Exception as exc:  # pragma: no cover - asserted below
                result = ("exception", type(exc).__name__, str(exc))
            finally:
                close_old_connections()
            with mutex:
                outcomes.append(result)

        threads = [Thread(target=worker, args=(operation,)) for operation in operations]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertTrue(all(not thread.is_alive() for thread in threads), outcomes)
        return outcomes

    def _digital_add(self):
        client = APIClient()
        client.force_authenticate(self.customer)
        return client.post(
            self.add_url,
            {"offer_id": self.offer.pk, "fulfillment_method": "remote"},
            format="json",
        )

    def _standard_add(self):
        client = APIClient()
        client.force_authenticate(self.customer)
        return client.post(
            self.standard_add_url,
            {"product": self.standard_product.pk, "quantity": 1, "attachment": []},
            format="json",
        )

    def _assert_exact_digital_result(self, outcomes):
        self.assertTrue(all(outcome[0] == "response" for outcome in outcomes), outcomes)
        self.assertEqual(sorted(outcome[1] for outcome in outcomes), [201, 409])
        conflict = next(outcome for outcome in outcomes if outcome[1] == 409)
        self.assertEqual(conflict[2], "digital_cart_selection_conflict")
        self.assertEqual(Cart.objects.filter(user=self.customer).count(), 1)
        self.assertEqual(CartItem.objects.count(), 1)
        self.assertEqual(DigitalCartSelection.objects.count(), 1)
        self.assertEqual(CartItem.objects.get().quantity, 1)

    def test_concurrent_first_digital_add_uses_one_cart_and_one_selection(self):
        outcomes = self._parallel((self._digital_add, self._digital_add))
        self._assert_exact_digital_result(outcomes)

    def test_concurrent_exact_add_with_existing_cart_has_one_selection(self):
        Cart.objects.create(user=self.customer)
        outcomes = self._parallel((self._digital_add, self._digital_add))
        self._assert_exact_digital_result(outcomes)

    def test_concurrent_first_standard_and_digital_add_share_one_cart(self):
        outcomes = self._parallel((self._standard_add, self._digital_add))
        self.assertTrue(all(outcome[0] == "response" for outcome in outcomes), outcomes)
        statuses = [outcome[1] for outcome in outcomes]
        self.assertEqual(statuses.count(409), 1, outcomes)
        self.assertEqual(len([value for value in statuses if value in (200, 201)]), 1, outcomes)
        self.assertEqual(Cart.objects.filter(user=self.customer).count(), 1)
        self.assertEqual(CartItem.objects.count(), 1)
        item = CartItem.objects.get()
        self.assertIn(
            item.commerce_authority,
            (
                ProductCommerceAuthority.STANDARD_COMMERCE,
                ProductCommerceAuthority.DIGITAL_PRODUCTS,
            ),
        )
        self.assertEqual(
            DigitalCartSelection.objects.count(),
            int(item.commerce_authority == ProductCommerceAuthority.DIGITAL_PRODUCTS),
        )
