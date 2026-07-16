from datetime import timedelta
from decimal import Decimal
from io import StringIO
from threading import Barrier, Thread
from unittest import skipUnless
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from cheatgame.product.models import Product, ProductType
from cheatgame.shop.apis.cart import OrderListCustomerAPIView
from cheatgame.shop.models import (
    Cart,
    CartLockReason,
    CartState,
    Checkout,
    CheckoutLine,
    CheckoutLineAttachment,
    CheckoutShippingSnapshot,
    CheckoutStatus,
    CommerceActorType,
    CommerceEventType,
    FulfillmentStatus,
    ManualReviewReason,
    Order,
    OrderUserStatus,
    PaymentTransaction,
    PaymentTransactionStatus,
    StockReservation,
    StockReservationState,
)
from cheatgame.shop.services.commerce_foundation import (
    CartLockConflict,
    append_commerce_event,
    build_cart_fingerprint,
    calculate_checkout_expiry,
    calculate_checkout_expiry_window,
    fulfillment_from_legacy_user_status,
    legacy_user_status_from_fulfillment,
    lock_for_checkout,
    sanitize_commerce_metadata,
    transition_to_manual_review,
    unlock_from_checkout,
)
from cheatgame.users.models import BaseUser


class CommerceFoundationTests(TestCase):
    def setUp(self):
        self.user = BaseUser.objects.create_user(
            phone_number="09120000001",
            firstname="Commerce",
            lastname="Test",
            password="StrongPass123!",
        )
        self.cart = Cart.objects.create(user=self.user)
        self.product = Product.objects.create(
            product_type=ProductType.PHYSCIAL,
            title="Foundation product",
            main_image="tests/product.png",
            price=Decimal("1000"),
            off_price=Decimal("900"),
            quantity=10,
            description="tests/product.html",
            order_limit=5,
        )

    def create_checkout(self, **overrides):
        now = timezone.now()
        defaults = {
            "user": self.user,
            "cart": self.cart,
            "client_checkout_uuid": uuid4(),
            "cart_fingerprint": "a" * 64,
            "status": CheckoutStatus.CHECKOUT_DRAFT,
            "expires_at": now + timedelta(minutes=30),
            "maximum_expires_at": now + timedelta(hours=2),
            "locked_at": now,
        }
        defaults.update(overrides)
        return Checkout.objects.create(**defaults)

    def create_line(self, checkout=None, **overrides):
        defaults = {
            "checkout": checkout or self.create_checkout(),
            "product_id": self.product.id,
            "product_name": self.product.title,
            "product_type": self.product.product_type,
            "unit_original_price": Decimal("1000"),
            "unit_payable_price": Decimal("900"),
            "quantity": 2,
            "line_original_total": Decimal("2000"),
            "line_payable_total": Decimal("1800"),
        }
        defaults.update(overrides)
        return CheckoutLine.objects.create(**defaults)

    def test_checkout_public_id_is_generated_unique_and_immutable(self):
        first = self.create_checkout()
        first.status = CheckoutStatus.CANCELED
        first.save(update_fields=["status", "updated_at"])
        second = self.create_checkout(cart=None)
        self.assertNotEqual(first.public_id, second.public_id)
        first.public_id = uuid4()
        with self.assertRaises(ValidationError):
            first.save()

    def test_same_user_client_uuid_is_unique(self):
        checkout_uuid = uuid4()
        self.create_checkout(client_checkout_uuid=checkout_uuid)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.create_checkout(client_checkout_uuid=checkout_uuid, cart=None)

    def test_only_one_active_checkout_per_cart(self):
        self.create_checkout()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.create_checkout()

    def test_terminal_checkouts_can_share_a_cart(self):
        first = self.create_checkout(status=CheckoutStatus.CANCELED)
        second = self.create_checkout(status=CheckoutStatus.EXPIRED)
        self.assertEqual(first.cart_id, self.cart.id)
        self.assertEqual(second.cart_id, self.cart.id)

    def test_existing_order_and_payment_without_checkout_remain_valid_and_serializable(self):
        order = Order.objects.create(
            user=self.user,
            total_price=Decimal("1000"),
            total_price_discount=Decimal("1000"),
        )
        payment = PaymentTransaction.objects.create(
            order=order,
            user=self.user,
            amount=Decimal("1000"),
            status=PaymentTransactionStatus.PENDING,
            idempotency_key=f"legacy:{uuid4()}",
        )
        self.assertIsNone(order.checkout_id)
        self.assertIsNone(payment.checkout_id)
        data = OrderListCustomerAPIView.OrderListCusotmerOutPutSerializer([order], many=True).data
        self.assertEqual(data[0]["id"], order.id)
        self.assertEqual(payment.status, "pending")

    def test_cart_lock_unlock_helpers_preserve_reason_and_owner(self):
        checkout = self.create_checkout()
        locked = lock_for_checkout(
            cart_id=self.cart.id,
            checkout_id=checkout.id,
            reason=CartLockReason.PAYMENT_IN_PROGRESS,
        )
        self.assertEqual(locked.state, CartState.LOCKED)
        self.assertEqual(locked.lock_reason, CartLockReason.PAYMENT_IN_PROGRESS)
        self.assertEqual(locked.active_checkout_id, checkout.id)
        unlocked = unlock_from_checkout(cart_id=self.cart.id, checkout_id=checkout.id)
        self.assertEqual(unlocked.state, CartState.OPEN)
        self.assertIsNone(unlocked.lock_reason)
        self.assertIsNone(unlocked.active_checkout_id)
        self.assertEqual(unlocked.lock_version, 2)

    def test_cart_lock_rejects_wrong_checkout(self):
        checkout = self.create_checkout()
        other_checkout = self.create_checkout(cart=None)
        lock_for_checkout(cart_id=self.cart.id, checkout_id=checkout.id)
        with self.assertRaises(CartLockConflict):
            unlock_from_checkout(cart_id=self.cart.id, checkout_id=other_checkout.id)

    def test_checkout_line_and_attachment_are_immutable_and_validate_totals(self):
        line = self.create_line()
        attachment = CheckoutLineAttachment.objects.create(
            checkout_line=line,
            attachment_id=10,
            attachment_type=1,
            name="Warranty",
            unit_price=Decimal("100"),
            quantity_basis=2,
            total_price=Decimal("200"),
        )
        line.product_name = "Changed"
        with self.assertRaises(ValidationError):
            line.save()
        attachment.total_price = Decimal("300")
        with self.assertRaises(ValidationError):
            attachment.save()
        with self.assertRaises(ValidationError):
            CheckoutLineAttachment.objects.create(
                checkout_line=self.create_line(checkout=line.checkout, product_id=self.product.id + 1),
                attachment_type=1,
                name="Invalid",
                unit_price=Decimal("100"),
                quantity_basis=2,
                total_price=Decimal("300"),
            )

    def test_shipping_snapshot_accepts_zero_delivery_cost(self):
        snapshot = CheckoutShippingSnapshot.objects.create(
            checkout=self.create_checkout(),
            delivery_method_name="Sandbox shipping",
            delivery_cost=0,
        )
        self.assertEqual(snapshot.delivery_cost, Decimal("0"))

    def test_manual_review_fields_and_transition_persist_safely(self):
        checkout = self.create_checkout()
        order = Order.objects.create(
            user=self.user,
            checkout=checkout,
            total_price=1000,
            total_price_discount=1000,
        )
        payment = PaymentTransaction.objects.create(
            order=order,
            checkout=checkout,
            user=self.user,
            amount=1000,
            idempotency_key=f"manual:{uuid4()}",
        )
        with CaptureQueriesContext(connection) as queries:
            transition_to_manual_review(
                checkout_id=checkout.id,
                payment_transaction_id=payment.id,
                reason=ManualReviewReason.STOCK_CONFLICT,
                message="Safe internal reason",
            )
        locking_sql = [query["sql"] for query in queries if "FOR UPDATE" in query["sql"]]
        cart_lock = next(index for index, sql in enumerate(locking_sql) if 'FROM "shop_cart"' in sql)
        checkout_lock = next(index for index, sql in enumerate(locking_sql) if 'FROM "shop_checkout"' in sql)
        transaction_lock = next(
            index for index, sql in enumerate(locking_sql) if 'FROM "shop_paymenttransaction"' in sql
        )
        self.assertLess(cart_lock, checkout_lock)
        self.assertLess(checkout_lock, transaction_lock)
        checkout.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(checkout.status, CheckoutStatus.REQUIRES_MANUAL_REVIEW)
        self.assertEqual(payment.status, PaymentTransactionStatus.REQUIRES_MANUAL_REVIEW)
        self.assertTrue(checkout.events.filter(event_type=CommerceEventType.MANUAL_REVIEW_REQUIRED).exists())

    def test_stock_reservation_requires_positive_unique_active_quantity(self):
        checkout = self.create_checkout()
        with self.assertRaises(IntegrityError), transaction.atomic():
            StockReservation.objects.create(
                checkout=checkout,
                product=self.product,
                quantity=0,
                expires_at=checkout.expires_at,
            )
        StockReservation.objects.create(
            checkout=checkout,
            product=self.product,
            quantity=1,
            expires_at=checkout.expires_at,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            StockReservation.objects.create(
                checkout=checkout,
                product=self.product,
                quantity=1,
                expires_at=checkout.expires_at,
            )
        StockReservation.objects.filter(checkout=checkout).update(state=StockReservationState.RELEASED)
        active = StockReservation.objects.create(
            checkout=checkout,
            product=self.product,
            quantity=1,
            expires_at=checkout.expires_at,
        )
        active.state = StockReservationState.CONSUMED
        active.save(update_fields=["state", "updated_at"])
        StockReservation.objects.create(
            checkout=checkout,
            product=self.product,
            quantity=1,
            expires_at=checkout.expires_at,
            state=StockReservationState.RELEASED,
        )

    def test_commerce_event_is_append_only_and_metadata_is_sanitized(self):
        event = append_commerce_event(
            checkout=self.create_checkout(),
            event_type=CommerceEventType.CHECKOUT_DRAFT_CREATED,
            actor_type=CommerceActorType.SYSTEM,
            metadata={
                "status": "created",
                "password": "must-not-persist",
                "authorization": "must-not-persist",
                "unknown": "must-not-persist",
            },
        )
        self.assertEqual(event.metadata, {"status": "created"})
        event.metadata = {"status": "changed"}
        with self.assertRaises(ValidationError):
            event.save()
        with self.assertRaises(ValidationError):
            event.delete()

    def test_sanitizer_removes_sensitive_nested_keys(self):
        clean = sanitize_commerce_metadata(
            {
                "reason_code": "test",
                "merchant_key": "secret",
                "details": {"provider_code": "100", "card_number": "1234", "otp": "9999"},
            }
        )
        self.assertEqual(clean, {"reason_code": "test", "details": {"provider_code": "100"}})

    def test_fingerprint_is_deterministic_and_changes_with_content(self):
        first = [
            {
                "product_id": 2,
                "variation_id": None,
                "quantity": 1,
                "unit_original_price": 1000,
                "unit_payable_price": 900,
                "attachments": [
                    {"id": 9, "type": 2, "unit_price": 100},
                    {"id": 3, "type": 1, "unit_price": 0},
                ],
            },
            {
                "product_id": 1,
                "variation_id": 4,
                "quantity": 2,
                "unit_original_price": 500,
                "unit_payable_price": 500,
                "attachments": [],
            },
        ]
        reordered = [first[1], {**first[0], "attachments": list(reversed(first[0]["attachments"]))}]
        self.assertEqual(build_cart_fingerprint(lines=first), build_cart_fingerprint(lines=reordered))
        changed = [{**first[0], "quantity": 2}, first[1]]
        self.assertNotEqual(build_cart_fingerprint(lines=first), build_cart_fingerprint(lines=changed))

    def test_expiry_calculation_respects_maximum(self):
        now = timezone.now()
        maximum = now + timedelta(minutes=10)
        self.assertEqual(
            calculate_checkout_expiry(now=now, ttl=timedelta(minutes=30), maximum_expires_at=maximum),
            maximum,
        )
        normal, maximum_lifetime = calculate_checkout_expiry_window(now=now)
        self.assertEqual(normal, now + timedelta(minutes=30))
        self.assertEqual(maximum_lifetime, now + timedelta(hours=2))

    def test_legacy_fulfillment_mapping(self):
        self.assertEqual(
            fulfillment_from_legacy_user_status(OrderUserStatus.SENDING.value),
            FulfillmentStatus.SENDING,
        )
        self.assertEqual(
            legacy_user_status_from_fulfillment(FulfillmentStatus.DELIVERED),
            OrderUserStatus.FINISHED.value,
        )

    def test_expire_checkouts_dry_run_changes_nothing(self):
        checkout = self.create_checkout(expires_at=timezone.now() - timedelta(minutes=1))
        lock_for_checkout(cart_id=self.cart.id, checkout_id=checkout.id)
        before_version = checkout.version
        output = StringIO()
        call_command("expire_checkouts", stdout=output)
        checkout.refresh_from_db()
        self.cart.refresh_from_db()
        self.assertIn(str(checkout.id), output.getvalue())
        self.assertEqual(checkout.status, CheckoutStatus.CHECKOUT_DRAFT)
        self.assertEqual(checkout.version, before_version)
        self.assertEqual(self.cart.state, CartState.LOCKED)

    def test_expire_checkouts_apply_expires_only_eligible_records(self):
        now = timezone.now()
        checkout = self.create_checkout(expires_at=now - timedelta(minutes=1))
        lock_for_checkout(cart_id=self.cart.id, checkout_id=checkout.id)
        reservation = StockReservation.objects.create(
            checkout=checkout,
            product=self.product,
            quantity=1,
            expires_at=checkout.expires_at,
        )

        protected_cart = Cart.objects.create(
            user=BaseUser.objects.create_user(
                phone_number="09120000002",
                firstname="Protected",
                lastname="Payment",
                password="StrongPass123!",
            )
        )
        protected = self.create_checkout(
            user=protected_cart.user,
            cart=protected_cart,
            status=CheckoutStatus.PENDING_PAYMENT,
            expires_at=now - timedelta(minutes=1),
        )
        protected_order = Order.objects.create(
            user=protected.user,
            checkout=protected,
            total_price=1000,
            total_price_discount=1000,
        )
        PaymentTransaction.objects.create(
            order=protected_order,
            checkout=protected,
            user=protected.user,
            amount=1000,
            status=PaymentTransactionStatus.PENDING,
            idempotency_key=f"protected:{uuid4()}",
        )
        manual_review = self.create_checkout(
            cart=None,
            status=CheckoutStatus.REQUIRES_MANUAL_REVIEW,
            expires_at=now - timedelta(minutes=1),
        )
        terminal = self.create_checkout(
            cart=None,
            status=CheckoutStatus.CANCELED,
            expires_at=now - timedelta(minutes=1),
        )

        output = StringIO()
        call_command("expire_checkouts", "--apply", stdout=output)

        checkout.refresh_from_db()
        self.cart.refresh_from_db()
        reservation.refresh_from_db()
        protected.refresh_from_db()
        manual_review.refresh_from_db()
        terminal.refresh_from_db()
        self.assertEqual(checkout.status, CheckoutStatus.EXPIRED)
        self.assertEqual(self.cart.state, CartState.OPEN)
        self.assertEqual(reservation.state, StockReservationState.RELEASED)
        self.assertTrue(checkout.events.filter(event_type=CommerceEventType.CHECKOUT_EXPIRED).exists())
        self.assertEqual(protected.status, CheckoutStatus.PENDING_PAYMENT)
        self.assertEqual(manual_review.status, CheckoutStatus.REQUIRES_MANUAL_REVIEW)
        self.assertEqual(terminal.status, CheckoutStatus.CANCELED)
        self.assertNotIn(str(protected.id), output.getvalue())

    def test_reconciliation_command_is_report_only(self):
        checkout = self.create_checkout(status=CheckoutStatus.PENDING_PAYMENT)
        order = Order.objects.create(user=self.user, checkout=checkout, total_price=1000, total_price_discount=1000)
        payment = PaymentTransaction.objects.create(
            order=order,
            checkout=checkout,
            user=self.user,
            amount=1000,
            status=PaymentTransactionStatus.CALLBACK_RECEIVED,
            idempotency_key=f"reconcile:{uuid4()}",
        )
        output = StringIO()
        call_command("reconcile_payment_transactions", stdout=output)
        payment.refresh_from_db()
        self.assertIn(str(payment.id), output.getvalue())
        self.assertEqual(payment.status, PaymentTransactionStatus.CALLBACK_RECEIVED)

    def test_audit_command_reports_intentional_fixture_inconsistency(self):
        checkout = self.create_checkout(cart=None)
        output = StringIO()
        call_command("audit_checkout_integrity", stdout=output)
        self.assertIn(str(checkout.id), output.getvalue())
        self.assertIn("active_without_cart", output.getvalue())


@skipUnless(connection.vendor == "postgresql", "PostgreSQL concurrency validation only")
class CommerceFoundationPostgreSQLConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.user = BaseUser.objects.create_user(
            phone_number="09120000011",
            firstname="PostgreSQL",
            lastname="Concurrency",
            password="StrongPass123!",
        )
        self.cart = Cart.objects.create(user=self.user)
        self.product = Product.objects.create(
            product_type=ProductType.PHYSCIAL,
            title="Concurrent product",
            main_image="tests/concurrent-product.png",
            price=Decimal("1000"),
            off_price=Decimal("900"),
            quantity=10,
            description="tests/concurrent-product.html",
            order_limit=5,
        )

    def run_concurrently(self, operation):
        barrier = Barrier(2)
        outcomes = []

        def worker():
            close_old_connections()
            try:
                barrier.wait(timeout=5)
                operation()
            except IntegrityError:
                outcomes.append("integrity_error")
            except Exception as exc:  # Preserve unexpected thread failures for the assertion.
                outcomes.append(f"unexpected:{type(exc).__name__}:{exc}")
            else:
                outcomes.append("created")
            finally:
                close_old_connections()

        threads = [Thread(target=worker), Thread(target=worker)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(any(thread.is_alive() for thread in threads), outcomes)
        self.assertCountEqual(outcomes, ["created", "integrity_error"])

    def test_concurrent_active_checkout_creation_is_database_serialized(self):
        now = timezone.now()

        def create_checkout():
            with transaction.atomic():
                Checkout.objects.create(
                    user_id=self.user.id,
                    cart_id=self.cart.id,
                    client_checkout_uuid=uuid4(),
                    cart_fingerprint="c" * 64,
                    expires_at=now + timedelta(minutes=30),
                    maximum_expires_at=now + timedelta(hours=2),
                    locked_at=now,
                )

        self.run_concurrently(create_checkout)
        self.assertEqual(Checkout.objects.filter(cart=self.cart, status=CheckoutStatus.CHECKOUT_DRAFT).count(), 1)

    def test_concurrent_active_reservation_creation_is_database_serialized(self):
        now = timezone.now()
        checkout = Checkout.objects.create(
            user=self.user,
            cart=self.cart,
            client_checkout_uuid=uuid4(),
            cart_fingerprint="d" * 64,
            expires_at=now + timedelta(minutes=30),
            maximum_expires_at=now + timedelta(hours=2),
            locked_at=now,
        )

        def create_reservation():
            with transaction.atomic():
                StockReservation.objects.create(
                    checkout_id=checkout.id,
                    product_id=self.product.id,
                    quantity=1,
                    expires_at=checkout.expires_at,
                )

        self.run_concurrently(create_reservation)
        self.assertEqual(
            StockReservation.objects.filter(
                checkout=checkout,
                product=self.product,
                state=StockReservationState.ACTIVE,
            ).count(),
            1,
        )
