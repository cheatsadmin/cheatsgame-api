from datetime import timedelta
from decimal import Decimal
from io import StringIO
from threading import Barrier, Thread
from unittest import skipUnless
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from cheatgame.digital_products.models import (
    DigitalCartFulfillmentMethod,
    DigitalCheckoutLineSnapshot,
    DigitalInventoryReservation,
    DigitalInventoryReservationState,
    DigitalOffer,
    DigitalOfferCapacity,
    DigitalOfferSaleState,
    InventoryPool,
    InventoryPoolStatus,
)
from cheatgame.digital_products.services import (
    DigitalCartLockedError,
    DigitalCheckoutIdempotencyError,
    DigitalOfferUnavailableError,
    InsufficientDigitalAvailabilityError,
    MixedCommerceAuthorityError,
    StandardCartNotSupportedError,
)
from cheatgame.digital_products.services.cart import add_digital_offer_to_cart
from cheatgame.digital_products.services.checkout_preparation import prepare_digital_checkout
from cheatgame.digital_products.services.inventory import get_available_quantity
from cheatgame.product.models import (
    DeliveredVersion,
    NativeConsole,
    Product,
    ProductCommerceAuthority,
    ProductType,
)
from cheatgame.shop.models import (
    Cart,
    CartItem,
    CartState,
    Checkout,
    CheckoutLine,
    CheckoutStatus,
    CommerceEventType,
    Order,
    PaymentTransaction,
)
from cheatgame.shop.services.cart import (
    CartCommerceAuthorityConflict,
    CartMutationLocked,
    add_to_cart,
    update_cart_item,
)
from cheatgame.shop.services.checkout import CheckoutServiceError, cancel_checkout, create_or_reuse_checkout
from cheatgame.users.models import BaseUser, UserTypes


class BatchBCheckoutTests(TestCase):
    def setUp(self):
        self.user = BaseUser.objects.create_user(
            phone_number="09123334441",
            firstname="Batch",
            lastname="B",
            password="test-only",
            user_type=UserTypes.CUSTOMER,
        )
        self.user.phone_verified = True
        self.user.save(update_fields=["phone_verified", "updated_at"])
        self.digital_product = self.product(
            "Digital", ProductType.GAME, ProductCommerceAuthority.DIGITAL_PRODUCTS, price=1, quantity=77
        )
        self.version = DeliveredVersion.objects.create(product=self.digital_product, native_console=NativeConsole.PS4)
        self.pool = InventoryPool.objects.create(sellable_quantity=2, status=InventoryPoolStatus.ENABLED)
        self.offer = DigitalOffer.objects.create(
            delivered_version=self.version,
            customer_console=NativeConsole.PS4,
            capacity=DigitalOfferCapacity.CAPACITY_1,
            price=Decimal("9000"),
            inventory_pool=self.pool,
            sale_state=DigitalOfferSaleState.ACTIVE,
        )
        self.cart = Cart.objects.create(user=self.user)

    def product(self, title, kind=ProductType.PHYSCIAL, authority=ProductCommerceAuthority.STANDARD_COMMERCE, **extra):
        values = {
            "product_type": kind,
            "commerce_authority": authority,
            "title": title,
            "main_image": "tests/product.png",
            "price": Decimal("1000"),
            "off_price": Decimal("900"),
            "quantity": 10,
            "description": "tests/product.html",
            "order_limit": 5,
        }
        values.update(extra)
        return Product.objects.create(**values)

    def add_digital(self):
        return add_digital_offer_to_cart(
            cart=self.cart,
            offer=self.offer,
            fulfillment_method=DigitalCartFulfillmentMethod.IN_STORE,
            actor=self.user,
        )

    def prepare(self, key=None):
        return prepare_digital_checkout(actor=self.user, client_checkout_uuid=key or uuid4())

    def test_authority_defaults_are_standard(self):
        standard = self.product("Standard")
        item = CartItem.objects.create(cart=self.cart, product=standard, quantity=1, price=standard.price)
        now = timezone.now()
        checkout = Checkout.objects.create(
            user=self.user, cart=self.cart, client_checkout_uuid=uuid4(), cart_fingerprint="a" * 64,
            expires_at=now + timedelta(minutes=20), maximum_expires_at=now + timedelta(hours=1), locked_at=now,
        )
        line = CheckoutLine.objects.create(
            checkout=checkout, source_cart_item_id=item.id, product_id=standard.id, product_name=standard.title,
            product_type=standard.product_type, unit_original_price=1000, unit_payable_price=900, quantity=1,
            line_original_total=1000, line_payable_total=900,
        )
        self.assertEqual(item.commerce_authority, ProductCommerceAuthority.STANDARD_COMMERCE)
        self.assertEqual(line.commerce_authority, ProductCommerceAuthority.STANDARD_COMMERCE)

    def test_database_rejects_invalid_authority_and_digital_quantity(self):
        standard = self.product("Constraint Standard")
        item = CartItem.objects.create(cart=self.cart, product=standard, quantity=1, price=standard.price)
        with self.assertRaises(IntegrityError), transaction.atomic():
            CartItem.objects.filter(pk=item.pk).update(commerce_authority="invalid")
        with self.assertRaises(IntegrityError), transaction.atomic():
            CartItem.objects.filter(pk=item.pk).update(
                commerce_authority=ProductCommerceAuthority.DIGITAL_PRODUCTS,
                quantity=2,
            )

    def test_digital_add_uses_offer_price_and_never_product_quantity(self):
        item, _ = self.add_digital()
        self.assertEqual(item.price, self.offer.price)
        self.digital_product.refresh_from_db()
        self.assertEqual((self.digital_product.price, self.digital_product.quantity), (Decimal("1"), 77))

    def test_mixed_adds_are_rejected_without_mutation(self):
        standard = self.product("Standard")
        CartItem.objects.create(cart=self.cart, product=standard, quantity=1, price=standard.price)
        with self.assertRaises(MixedCommerceAuthorityError):
            self.add_digital()
        self.assertEqual(self.cart.cartitem_set.count(), 1)

        self.cart.cartitem_set.all().delete()
        self.add_digital()
        with self.assertRaises(CartCommerceAuthorityConflict):
            add_to_cart(attachment=[], quantity=1, product=standard, user=self.user)
        self.assertEqual(self.cart.cartitem_set.count(), 1)

    def test_standard_checkout_rejects_digital_and_mixed(self):
        self.add_digital()
        with self.assertRaises(CheckoutServiceError) as digital:
            create_or_reuse_checkout(user=self.user, client_checkout_uuid=uuid4())
        self.assertEqual(digital.exception.code, "DIGITAL_CART_REQUIRES_DIGITAL_CHECKOUT")
        standard = self.product("Standard")
        CartItem.objects.create(cart=self.cart, product=standard, quantity=1, price=standard.price)
        with self.assertRaises(CheckoutServiceError) as mixed:
            create_or_reuse_checkout(user=self.user, client_checkout_uuid=uuid4())
        self.assertEqual(mixed.exception.code, "MIXED_COMMERCE_AUTHORITY_NOT_SUPPORTED")
        self.cart.refresh_from_db()
        self.assertEqual(self.cart.state, CartState.OPEN)

    def test_digital_checkout_rejects_standard_and_mixed(self):
        standard = self.product("Standard")
        CartItem.objects.create(cart=self.cart, product=standard, quantity=1, price=standard.price)
        with self.assertRaises(StandardCartNotSupportedError):
            self.prepare()
        CartItem.objects.create(
            cart=self.cart, product=self.digital_product, quantity=1, price=self.offer.price,
            commerce_authority=ProductCommerceAuthority.DIGITAL_PRODUCTS,
        )
        with self.assertRaises(MixedCommerceAuthorityError):
            self.prepare()
        self.cart.refresh_from_db()
        self.assertEqual(self.cart.state, CartState.OPEN)

    def test_prepare_creates_offer_owned_snapshot_and_reservation_only(self):
        item, _ = self.add_digital()
        checkout, created = self.prepare()
        self.assertTrue(created)
        line = checkout.lines.get()
        snapshot = line.digital_snapshot
        reservation = line.digital_inventory_reservation
        self.assertEqual((line.unit_payable_price, snapshot.unit_price), (self.offer.price, self.offer.price))
        self.assertEqual(snapshot.inventory_pool_id, self.pool.id)
        self.assertEqual(reservation.state, DigitalInventoryReservationState.ACTIVE)
        self.pool.refresh_from_db()
        self.digital_product.refresh_from_db()
        self.assertEqual((self.pool.sellable_quantity, get_available_quantity(pool_id=self.pool.id)), (2, 1))
        self.assertEqual((self.digital_product.price, self.digital_product.quantity), (Decimal("1"), 77))
        self.assertEqual(item.price, self.offer.price)
        self.assertFalse(PaymentTransaction.objects.filter(checkout=checkout).exists())
        self.assertFalse(Order.objects.filter(user=self.user).exists())

    def test_database_rejects_invalid_reservation_quantity(self):
        self.add_digital()
        checkout, _ = self.prepare()
        reservation = DigitalInventoryReservation.objects.get(checkout=checkout)
        with self.assertRaises(IntegrityError), transaction.atomic():
            DigitalInventoryReservation.objects.filter(pk=reservation.pk).update(quantity=2)

    def test_snapshot_is_application_immutable(self):
        self.add_digital()
        checkout, _ = self.prepare()
        snapshot = checkout.lines.get().digital_snapshot
        snapshot.product_name = "Changed"
        with self.assertRaises(ValidationError):
            snapshot.save()
        with self.assertRaises(ValidationError):
            snapshot.delete()

    def test_same_uuid_reuses_and_changed_offer_price_conflicts(self):
        self.add_digital()
        key = uuid4()
        first, created = self.prepare(key)
        second, reused_created = self.prepare(key)
        self.assertTrue(created)
        self.assertFalse(reused_created)
        self.assertEqual(first.id, second.id)
        self.assertEqual(
            first.events.filter(event_type=CommerceEventType.CHECKOUT_DRAFT_REUSED).count(), 1
        )
        DigitalOffer.objects.filter(pk=self.offer.pk).update(price=Decimal("9100"))
        with self.assertRaises(DigitalCheckoutIdempotencyError):
            self.prepare(key)

    def test_different_uuid_on_locked_cart_returns_safe_resume_only(self):
        self.add_digital()
        checkout, _ = self.prepare()
        with self.assertRaises(DigitalCartLockedError) as error:
            self.prepare(uuid4())
        self.assertEqual(set(error.exception.details), {"public_id", "status", "resume_route"})
        self.assertEqual(error.exception.details["public_id"], str(checkout.public_id))

    def test_insufficient_availability_rolls_back_checkout_and_lock(self):
        self.add_digital()
        InventoryPool.objects.filter(pk=self.pool.pk).update(sellable_quantity=0)
        with self.assertRaises(InsufficientDigitalAvailabilityError):
            self.prepare()
        self.cart.refresh_from_db()
        self.assertEqual(self.cart.state, CartState.OPEN)
        self.assertFalse(Checkout.objects.filter(cart=self.cart).exists())
        self.assertFalse(DigitalInventoryReservation.objects.exists())

    @override_settings(COMMERCE_CHECKOUT_V2_ENABLED=False)
    def test_locked_digital_cart_cannot_use_standard_mutation(self):
        item, _ = self.add_digital()
        self.prepare()
        with self.assertRaises(CartMutationLocked):
            update_cart_item(cart_item=item, quantity=1)

    def test_cancellation_releases_only_reservation_and_is_idempotent(self):
        self.add_digital()
        checkout, _ = self.prepare()
        before = self.pool.sellable_quantity
        canceled = cancel_checkout(user=self.user, public_id=checkout.public_id)
        again = cancel_checkout(user=self.user, public_id=checkout.public_id)
        self.assertEqual((canceled.status, again.status), (CheckoutStatus.CANCELED, CheckoutStatus.CANCELED))
        reservation = DigitalInventoryReservation.objects.get(checkout=checkout)
        self.assertEqual(reservation.state, DigitalInventoryReservationState.RELEASED)
        self.pool.refresh_from_db()
        self.cart.refresh_from_db()
        self.assertEqual(self.pool.sellable_quantity, before)
        self.assertEqual(self.cart.state, CartState.OPEN)

    def test_expiry_releases_digital_reservation_without_changing_pool(self):
        self.add_digital()
        checkout, _ = self.prepare()
        Checkout.objects.filter(pk=checkout.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        out = StringIO()
        call_command("expire_checkouts", stdout=out)
        checkout.refresh_from_db()
        self.assertEqual(checkout.status, CheckoutStatus.CHECKOUT_DRAFT)
        call_command("expire_checkouts", apply=True, stdout=out)
        checkout.refresh_from_db()
        self.assertEqual(checkout.status, CheckoutStatus.EXPIRED)
        self.assertEqual(
            DigitalInventoryReservation.objects.get(checkout=checkout).state,
            DigitalInventoryReservationState.EXPIRED,
        )
        self.pool.refresh_from_db()
        self.assertEqual(self.pool.sellable_quantity, 2)

    def test_event_order_contains_no_internal_pool_or_reservation_ids(self):
        self.add_digital()
        checkout, _ = self.prepare()
        self.assertEqual(
            list(checkout.events.values_list("event_type", flat=True)),
            [
                CommerceEventType.CHECKOUT_DRAFT_CREATED,
                CommerceEventType.STOCK_RESERVATION_CREATED,
                CommerceEventType.CART_LOCKED,
            ],
        )
        serialized = str(list(checkout.events.values_list("metadata", flat=True))).lower()
        self.assertNotIn("pool", serialized)
        self.assertNotIn("reservation_id", serialized)


@skipUnless(connection.vendor == "postgresql", "PostgreSQL row-lock semantics required")
class BatchBConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.pool = InventoryPool.objects.create(sellable_quantity=1, status=InventoryPoolStatus.ENABLED)
        self.product = Product.objects.create(
            product_type=ProductType.GAME,
            commerce_authority=ProductCommerceAuthority.DIGITAL_PRODUCTS,
            title="Concurrent Digital",
            main_image="tests/product.png",
            price=1,
            off_price=1,
            quantity=999,
            description="tests/product.html",
        )
        version = DeliveredVersion.objects.create(product=self.product, native_console=NativeConsole.PS4)
        self.offer = DigitalOffer.objects.create(
            delivered_version=version,
            customer_console=NativeConsole.PS4,
            capacity=DigitalOfferCapacity.CAPACITY_1,
            price=100,
            inventory_pool=self.pool,
            sale_state=DigitalOfferSaleState.ACTIVE,
        )

    def make_user_cart(self, suffix):
        user = BaseUser.objects.create_user(
            phone_number=f"09124445{suffix:03d}",
            firstname="Concurrent",
            lastname="B",
            password="test-only",
            user_type=UserTypes.CUSTOMER,
        )
        user.phone_verified = True
        user.save(update_fields=["phone_verified", "updated_at"])
        cart = Cart.objects.create(user=user)
        add_digital_offer_to_cart(
            cart=cart,
            offer=self.offer,
            fulfillment_method=DigitalCartFulfillmentMethod.IN_STORE,
            actor=user,
        )
        return user, cart

    def run_prepare(self, barrier, user_id, key, results):
        close_old_connections()
        try:
            barrier.wait()
            user = BaseUser.objects.get(pk=user_id)
            checkout, created = prepare_digital_checkout(actor=user, client_checkout_uuid=key)
            results.append(("ok", checkout.id, created))
        except Exception as exc:  # asserted by type below
            results.append(("error", type(exc).__name__, getattr(exc, "code", None)))
        finally:
            close_old_connections()

    def test_concurrent_shared_pool_reservations_cannot_oversell(self):
        first, _ = self.make_user_cart(1)
        second, _ = self.make_user_cart(2)
        barrier = Barrier(2)
        results = []
        threads = [
            Thread(target=self.run_prepare, args=(barrier, user.id, uuid4(), results))
            for user in (first, second)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(sum(result[0] == "ok" for result in results), 1)
        self.assertEqual(sum(result[1] == "InsufficientDigitalAvailabilityError" for result in results if result[0] == "error"), 1)
        self.assertEqual(
            DigitalInventoryReservation.objects.filter(state=DigitalInventoryReservationState.ACTIVE).count(), 1
        )
        self.pool.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual((self.pool.sellable_quantity, self.product.quantity), (1, 999))

    def test_concurrent_prepare_same_cart_has_one_owner_without_deadlock(self):
        user, cart = self.make_user_cart(3)
        barrier = Barrier(2)
        results = []
        threads = [
            Thread(target=self.run_prepare, args=(barrier, user.id, uuid4(), results))
            for _ in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(sum(result[0] == "ok" for result in results), 1)
        self.assertEqual(sum(result[1] == "DigitalCartLockedError" for result in results if result[0] == "error"), 1)
        cart.refresh_from_db()
        self.assertEqual(cart.state, CartState.LOCKED)
        self.assertEqual(Checkout.objects.filter(cart=cart, status__in=Checkout.ACTIVE_STATUSES).count(), 1)
