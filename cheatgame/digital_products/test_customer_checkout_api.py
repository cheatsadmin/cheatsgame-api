from datetime import timedelta
from decimal import Decimal
from threading import Barrier, Lock, Thread
from unittest import skipUnless
from uuid import uuid4

from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from drf_spectacular.generators import SchemaGenerator
from rest_framework.test import APIClient

from cheatgame.digital_products.models import (
    DigitalCartFulfillmentMethod,
    DigitalCartSelection,
    DigitalFulfillmentItem,
    DigitalInventoryReservation,
    DigitalInventoryReservationState,
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
    CheckoutLine,
    CommerceEvent,
    Order,
    OrderItem,
    PaymentTransaction,
    StockReservation,
)
from cheatgame.users.models import BaseUser, UserTypes


class CustomerDigitalCheckoutApiTests(TestCase):
    prepare_url = "/api/digital-products/customer/checkout/prepare/"
    active_url = "/api/digital-products/customer/checkout/active/"

    def setUp(self):
        self.client = APIClient()
        self.customer = self.user("09126660001")
        self.client.force_authenticate(self.customer)
        self.cart = Cart.objects.create(user=self.customer)
        self.product = self.make_product("Checkout Game")
        self.offer = self.make_offer(self.product)

    def user(self, phone, *, user_type=UserTypes.CUSTOMER, verified=True, active=True):
        user = BaseUser.objects.create_user(
            phone_number=phone,
            firstname="Checkout",
            lastname="Customer",
            password="test-only",
            user_type=user_type,
            is_active=active,
        )
        user.phone_verified = verified
        user.save(update_fields=("phone_verified", "updated_at"))
        return user

    def make_product(self, title, *, authority=ProductCommerceAuthority.DIGITAL_PRODUCTS):
        return Product.objects.create(
            product_type=ProductType.GAME,
            commerce_authority=authority,
            status=ProductStatus.PUBLISHED,
            title=title,
            slug=title.lower().replace(" ", "-"),
            main_image="tests/checkout.png",
            description="tests/checkout.html",
            price=Decimal("9999999"),
            off_price=Decimal("9999999"),
            quantity=999,
            order_limit=5,
        )

    def make_offer(self, product, *, pool=None, price=Decimal("450000"), console=NativeConsole.PS4):
        version = DeliveredVersion.objects.create(product=product, native_console=NativeConsole.PS4)
        pool = pool or InventoryPool.objects.create(
            sellable_quantity=4,
            status=InventoryPoolStatus.ENABLED,
        )
        return DigitalOffer.objects.create(
            delivered_version=version,
            customer_console=console,
            capacity=DigitalOfferCapacity.CAPACITY_2,
            price=price,
            inventory_pool=pool,
            sale_state=DigitalOfferSaleState.ACTIVE,
        )

    def add_offer(self, offer=None, *, cart=None, user=None):
        return add_digital_offer_to_cart(
            cart=cart or self.cart,
            offer=offer or self.offer,
            fulfillment_method=DigitalCartFulfillmentMethod.REMOTE,
            actor=user or self.customer,
        )[0]

    def prepare(self, key=None, **extra):
        payload = {"checkout_uuid": str(key or uuid4()), **extra}
        return self.client.post(self.prepare_url, payload, format="json")

    def detail_url(self, checkout):
        return f"/api/digital-products/customer/checkout/{checkout.public_id}/"

    def cancel_url(self, checkout):
        return f"{self.detail_url(checkout)}cancel/"

    def test_permission_method_and_strict_input_boundary(self):
        self.add_offer()
        self.client.force_authenticate(None)
        self.assertEqual(self.prepare().status_code, 401)
        unverified = self.user("09126660002", verified=False)
        self.client.force_authenticate(unverified)
        self.assertEqual(self.prepare().status_code, 403)
        admin = self.user("09126660003", user_type=UserTypes.ADMIN)
        self.client.force_authenticate(admin)
        self.assertEqual(self.prepare().status_code, 403)
        self.client.force_authenticate(self.customer)
        invalid = self.prepare(amount=1)
        self.assertEqual((invalid.status_code, invalid.data["code"]), (400, "invalid_request"))
        self.assertIn("amount", invalid.data["fields"])
        method = self.client.get(self.prepare_url)
        self.assertEqual((method.status_code, method.data["code"]), (405, "method_not_allowed"))

    def test_prepare_creates_immutable_graph_and_derived_readiness_only(self):
        item = self.add_offer()
        response = self.prepare()
        self.assertEqual(response.status_code, 201, response.data)
        checkout = Checkout.objects.get(public_id=response.data["public_id"])
        line = checkout.lines.get()
        snapshot = line.digital_snapshot
        reservation = line.digital_inventory_reservation
        self.cart.refresh_from_db()
        self.assertEqual(self.cart.state, CartState.LOCKED)
        self.assertEqual(self.cart.active_checkout_id, checkout.pk)
        self.assertEqual(line.source_cart_item_id, item.pk)
        self.assertEqual(line.snapshot["commercial_revision"], 1)
        self.assertEqual(response.data["commercial_revision"], 1)
        self.assertTrue(response.data["is_commercially_ready"])
        self.assertTrue(response.data["is_payment_ready"])
        self.assertEqual(response.data["readiness_code"], "READY")
        self.assertEqual(Decimal(response.data["totals"]["total"]), self.offer.price)
        self.assertEqual(snapshot.unit_price, self.offer.price)
        self.assertEqual(reservation.state, DigitalInventoryReservationState.ACTIVE)
        self.assertEqual(reservation.expires_at, checkout.expires_at)
        self.offer.inventory_pool.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(self.offer.inventory_pool.sellable_quantity, 4)
        self.assertEqual(self.product.quantity, 999)
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(OrderItem.objects.count(), 0)
        self.assertEqual(Payment.objects.count(), 0)
        self.assertEqual(PaymentAttempt.objects.count(), 0)
        self.assertEqual(PaymentTransaction.objects.count(), 0)
        self.assertEqual(CommercialFinalization.objects.count(), 0)
        self.assertEqual(DigitalFulfillmentObligation.objects.count(), 0)
        self.assertEqual(DigitalFulfillmentItem.objects.count(), 0)
        self.assertEqual(Entitlement.objects.count(), 0)

    def test_prepare_idempotency_browser_retry_and_multiple_device_conflict(self):
        self.add_offer()
        key = uuid4()
        first = self.prepare(key)
        second = self.prepare(key)
        other_device = self.prepare(uuid4())
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.data["public_id"], second.data["public_id"])
        self.assertEqual((other_device.status_code, other_device.data["code"]), (409, "digital_cart_locked"))
        self.assertEqual(Checkout.objects.count(), 1)
        self.assertEqual(CheckoutLine.objects.count(), 1)
        self.assertEqual(DigitalInventoryReservation.objects.count(), 1)

    def test_stale_price_product_offer_version_and_pool_fail_without_partial_graph(self):
        scenarios = (
            ("price", lambda: DigitalOffer.objects.filter(pk=self.offer.pk).update(price=Decimal("460000"))),
            ("product", lambda: Product.objects.filter(pk=self.product.pk).update(status=ProductStatus.HIDDEN)),
            ("offer", lambda: DigitalOffer.objects.filter(pk=self.offer.pk).update(sale_state=DigitalOfferSaleState.PAUSED)),
            ("version", lambda: DeliveredVersion.objects.filter(pk=self.offer.delivered_version_id).update(is_active=False)),
            ("pool", lambda: InventoryPool.objects.filter(pk=self.offer.inventory_pool_id).update(status=InventoryPoolStatus.PAUSED)),
        )
        for label, mutate in scenarios:
            with self.subTest(label=label):
                self.add_offer()
                mutate()
                response = self.prepare()
                self.assertEqual(response.status_code, 409, response.data)
                self.assertIn(response.data["code"], {"digital_cart_stale", "digital_availability_unavailable"})
                self.assertFalse(Checkout.objects.exists())
                self.assertFalse(DigitalInventoryReservation.objects.exists())
                self.cart.refresh_from_db()
                self.assertEqual(self.cart.state, CartState.OPEN)
                self.cart.cartitem_set.all().delete()
                Product.objects.filter(pk=self.product.pk).update(status=ProductStatus.PUBLISHED)
                DigitalOffer.objects.filter(pk=self.offer.pk).update(
                    price=Decimal("450000"), sale_state=DigitalOfferSaleState.ACTIVE
                )
                DeliveredVersion.objects.filter(pk=self.offer.delivered_version_id).update(is_active=True)
                InventoryPool.objects.filter(pk=self.offer.inventory_pool_id).update(status=InventoryPoolStatus.ENABLED)

    def test_sold_out_and_shared_pool_aggregate_demand_roll_back_atomically(self):
        shared = self.offer.inventory_pool
        shared.sellable_quantity = 1
        shared.save(update_fields=("sellable_quantity", "updated_at"))
        second = DigitalOffer.objects.create(
            delivered_version=self.offer.delivered_version,
            customer_console=NativeConsole.PS5,
            capacity=self.offer.capacity,
            price=Decimal("470000"),
            inventory_pool=shared,
            sale_state=DigitalOfferSaleState.ACTIVE,
        )
        self.add_offer()
        self.add_offer(second)
        response = self.prepare()
        self.assertEqual((response.status_code, response.data["code"]), (409, "digital_availability_unavailable"))
        self.assertFalse(Checkout.objects.exists())
        self.assertFalse(DigitalInventoryReservation.objects.exists())
        self.cart.refresh_from_db()
        self.assertEqual(self.cart.state, CartState.OPEN)

    def test_standard_and_mixed_carts_are_rejected_without_local_split(self):
        standard = self.make_product("Standard", authority=ProductCommerceAuthority.STANDARD_COMMERCE)
        CartItem.objects.create(cart=self.cart, product=standard, price=100, quantity=1)
        standard_response = self.prepare()
        self.assertEqual(standard_response.data["code"], "standard_cart_requires_standard_checkout")
        self.assertFalse(Checkout.objects.exists())
        digital_item = CartItem.objects.create(
            cart=self.cart,
            product=self.product,
            price=self.offer.price,
            quantity=1,
            commerce_authority=ProductCommerceAuthority.DIGITAL_PRODUCTS,
        )
        DigitalCartSelection.objects.create(
            cart_item=digital_item,
            offer=self.offer,
            fulfillment_method=DigitalCartFulfillmentMethod.REMOTE,
        )
        mixed = self.prepare()
        self.assertEqual((mixed.status_code, mixed.data["code"]), (409, "mixed_commerce_authority"))
        self.assertFalse(Checkout.objects.exists())

    def test_active_detail_ownership_and_no_lease_renewal(self):
        self.add_offer()
        prepared = self.prepare()
        checkout = Checkout.objects.get(public_id=prepared.data["public_id"])
        original_expiry = checkout.expires_at
        active = self.client.get(self.active_url)
        detail = self.client.get(self.detail_url(checkout))
        self.assertEqual((active.status_code, detail.status_code), (200, 200))
        checkout.refresh_from_db()
        self.assertEqual(checkout.expires_at, original_expiry)
        other = self.user("09126660004")
        self.client.force_authenticate(other)
        hidden = self.client.get(self.detail_url(checkout))
        self.assertEqual((hidden.status_code, hidden.data["code"]), (404, "digital_checkout_not_found"))

    def test_expiration_is_backend_owned_releases_reservation_and_unlocks_cart(self):
        self.add_offer()
        prepared = self.prepare()
        checkout = Checkout.objects.get(public_id=prepared.data["public_id"])
        Checkout.objects.filter(pk=checkout.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        response = self.client.get(self.detail_url(checkout))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "expired")
        self.assertFalse(response.data["is_payment_ready"])
        checkout.refresh_from_db()
        self.cart.refresh_from_db()
        self.assertEqual(checkout.status, "expired")
        self.assertEqual(self.cart.state, CartState.OPEN)
        self.assertEqual(
            DigitalInventoryReservation.objects.get(checkout=checkout).state,
            DigitalInventoryReservationState.EXPIRED,
        )
        self.assertEqual(self.client.get(self.active_url).status_code, 404)

    def test_expired_same_uuid_retry_releases_before_returning_conflict(self):
        self.add_offer()
        key = uuid4()
        prepared = self.prepare(key)
        checkout = Checkout.objects.get(public_id=prepared.data["public_id"])
        Checkout.objects.filter(pk=checkout.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        retry = self.prepare(key)
        self.assertEqual((retry.status_code, retry.data["code"]), (409, "digital_checkout_expired"))
        checkout.refresh_from_db()
        self.cart.refresh_from_db()
        self.assertEqual(checkout.status, "expired")
        self.assertEqual(self.cart.state, CartState.OPEN)
        self.assertEqual(
            DigitalInventoryReservation.objects.get(checkout=checkout).state,
            DigitalInventoryReservationState.EXPIRED,
        )

    def test_cancel_is_idempotent_releases_only_active_reservations_and_rejects_fields(self):
        self.add_offer()
        prepared = self.prepare()
        checkout = Checkout.objects.get(public_id=prepared.data["public_id"])
        invalid = self.client.post(self.cancel_url(checkout), {"reason": "client"}, format="json")
        self.assertEqual(invalid.status_code, 400)
        canceled = self.client.post(self.cancel_url(checkout), {}, format="json")
        replay = self.client.post(self.cancel_url(checkout), {}, format="json")
        self.assertEqual((canceled.status_code, replay.status_code), (200, 200))
        self.assertEqual(canceled.data["status"], "canceled")
        self.assertFalse(canceled.data["is_payment_ready"])
        self.cart.refresh_from_db()
        self.assertEqual(self.cart.state, CartState.OPEN)
        self.assertEqual(
            DigitalInventoryReservation.objects.get(checkout=checkout).state,
            DigitalInventoryReservationState.RELEASED,
        )
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(Payment.objects.count(), 0)

    @override_settings(
        COMMERCE_CHECKOUT_TTL_SECONDS=90,
        COMMERCE_CHECKOUT_MAXIMUM_LIFETIME_SECONDS=300,
    )
    def test_expiration_policy_is_configurable_finite_and_bounded(self):
        self.add_offer()
        before = timezone.now()
        checkout = Checkout.objects.get(public_id=self.prepare().data["public_id"])
        self.assertLessEqual(checkout.expires_at, before + timedelta(seconds=92))
        self.assertGreaterEqual(checkout.maximum_expires_at, before + timedelta(seconds=298))
        self.assertLessEqual(checkout.expires_at, checkout.maximum_expires_at)

    def test_projection_is_private_bounded_and_openapi_explicit(self):
        self.add_offer()
        checkout = Checkout.objects.get(public_id=self.prepare().data["public_id"])
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(self.detail_url(checkout))
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(len(queries), 10)
        serialized = str(response.data).lower()
        for forbidden in (
            "inventory_pool",
            "reservation",
            "sellable_quantity",
            "cart_fingerprint",
            "payment_transaction",
            "journal",
            "provider",
        ):
            self.assertNotIn(forbidden, serialized)
        schema = SchemaGenerator().get_schema(request=None, public=True)
        for path in (
            self.prepare_url,
            self.active_url,
            "/api/digital-products/customer/checkout/{checkout_id}/",
            "/api/digital-products/customer/checkout/{checkout_id}/cancel/",
        ):
            self.assertIn(path, schema["paths"])


@skipUnless(connection.vendor == "postgresql", "PostgreSQL row-lock semantics required")
class CustomerDigitalCheckoutConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.pool = InventoryPool.objects.create(sellable_quantity=2, status=InventoryPoolStatus.ENABLED)
        self.product = Product.objects.create(
            product_type=ProductType.GAME,
            commerce_authority=ProductCommerceAuthority.DIGITAL_PRODUCTS,
            status=ProductStatus.PUBLISHED,
            title="Concurrent Checkout",
            slug="concurrent-checkout",
            main_image="tests/concurrent.png",
            description="tests/concurrent.html",
            price=1,
            off_price=1,
            quantity=999,
        )
        version = DeliveredVersion.objects.create(product=self.product, native_console=NativeConsole.PS4)
        self.offer = DigitalOffer.objects.create(
            delivered_version=version,
            customer_console=NativeConsole.PS4,
            capacity=DigitalOfferCapacity.CAPACITY_2,
            price=Decimal("500000"),
            inventory_pool=self.pool,
            sale_state=DigitalOfferSaleState.ACTIVE,
        )
        self.user = BaseUser.objects.create_user(
            phone_number="09126660100",
            firstname="Concurrent",
            lastname="Checkout",
            password="test-only",
            user_type=UserTypes.CUSTOMER,
        )
        self.user.phone_verified = True
        self.user.save(update_fields=("phone_verified", "updated_at"))
        cart = Cart.objects.create(user=self.user)
        add_digital_offer_to_cart(
            cart=cart,
            offer=self.offer,
            fulfillment_method=DigitalCartFulfillmentMethod.REMOTE,
            actor=self.user,
        )

    def _request(self, barrier, key, results, lock):
        close_old_connections()
        try:
            client = APIClient()
            client.force_authenticate(BaseUser.objects.get(pk=self.user.pk))
            barrier.wait()
            response = client.post(
                "/api/digital-products/customer/checkout/prepare/",
                {"checkout_uuid": str(key)},
                format="json",
            )
            value = (response.status_code, response.data.get("code"), response.data.get("public_id"))
            with lock:
                results.append(value)
        finally:
            close_old_connections()

    def test_concurrent_duplicate_prepare_is_one_atomic_graph(self):
        barrier = Barrier(2)
        lock = Lock()
        results = []
        key = uuid4()
        threads = [Thread(target=self._request, args=(barrier, key, results, lock)) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(sorted(result[0] for result in results), [200, 201])
        self.assertEqual(len({result[2] for result in results}), 1)
        self.assertEqual(Checkout.objects.count(), 1)
        self.assertEqual(CheckoutLine.objects.count(), 1)
        self.assertEqual(DigitalInventoryReservation.objects.count(), 1)
        self.assertEqual(CommerceEvent.objects.filter(event_type="checkout_draft_created").count(), 1)
        self.assertEqual(CommerceEvent.objects.filter(event_type="cart_locked").count(), 1)

    def test_concurrent_different_keys_have_one_owner_and_no_partial_graph(self):
        barrier = Barrier(2)
        lock = Lock()
        results = []
        threads = [Thread(target=self._request, args=(barrier, uuid4(), results, lock)) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(sum(result[0] == 201 for result in results), 1)
        self.assertEqual(sum(result[0] == 409 for result in results), 1)
        self.assertEqual(Checkout.objects.count(), 1)
        self.assertEqual(CheckoutLine.objects.count(), 1)
        self.assertEqual(DigitalInventoryReservation.objects.count(), 1)
