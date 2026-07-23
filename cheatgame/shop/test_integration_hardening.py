from decimal import Decimal
from threading import Event, Lock, Thread
from unittest import skipUnless
from unittest.mock import patch
from uuid import uuid4

from django.db import close_old_connections, connection
from django.test import TransactionTestCase, override_settings

from cheatgame.product.models import Product, ProductType
from cheatgame.shop.models import Order, OrderItem, OrderStatus, PaymentTransaction, PaymentTransactionStatus
from cheatgame.shop.payments.providers import PaymentRequestResult, PaymentVerifyResult
from cheatgame.shop.payments.services import PaymentError, create_payment_request, verify_payment
from cheatgame.users.models import BaseUser


class OutsideAtomicProvider:
    name = "fake"

    def __init__(self):
        self.request_inside_atomic = None

    def create_payment_request(self, *, transaction, callback_url):
        self.request_inside_atomic = connection.in_atomic_block
        return PaymentRequestResult(
            authority=f"OUTSIDE-{transaction.pk}",
            payment_url=f"{callback_url}?authority=OUTSIDE-{transaction.pk}",
            payload={"provider": "synthetic"},
        )


class BlockingVerificationProvider:
    name = "fake"

    def __init__(self):
        self.entered = Event()
        self.release = Event()
        self.lock = Lock()
        self.calls = 0
        self.inside_atomic = None

    def verify(self, *, transaction):
        with self.lock:
            self.calls += 1
        self.inside_atomic = connection.in_atomic_block
        self.entered.set()
        if not self.release.wait(10):
            raise RuntimeError("test verification release timed out")
        return PaymentVerifyResult(
            is_paid=True,
            ref_id=f"REF-{transaction.pk}",
            trace_no=f"TRACE-{transaction.pk}",
            payload={"status": "paid"},
        )


@skipUnless(connection.vendor == "postgresql", "PostgreSQL concurrency validation only")
@override_settings(PAYMENT_FAKE_PROVIDER_ENABLED=True, PAYMENT_GATEWAY_PROVIDER="fake")
class LegacyPaymentHardeningConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.user = BaseUser.objects.create_user(
            phone_number="09129990001", firstname="Legacy", lastname="Race", password="StrongPass123!"
        )
        self.product = Product.objects.create(
            product_type=ProductType.GAME,
            title="Legacy race product",
            main_image="tests/legacy-race.png",
            price=Decimal("1000"),
            off_price=Decimal("1000"),
            quantity=1,
            description="tests/legacy-race.html",
            order_limit=1,
        )
        self.order = Order.objects.create(
            user=self.user,
            total_price=Decimal("1000"),
            total_price_discount=Decimal("1000"),
            is_game=True,
        )
        OrderItem.objects.create(order=self.order, product=self.product, quantity=1, price=Decimal("1000"))

    def test_request_transport_runs_after_claim_commit(self):
        provider = OutsideAtomicProvider()
        with patch("cheatgame.shop.payments.services.get_payment_provider", return_value=provider):
            result = create_payment_request(
                order_id=self.order.pk,
                user=self.user,
                callback_url="https://backend.test/callback",
            )
        self.assertFalse(provider.request_inside_atomic)
        self.assertEqual(result.status, PaymentTransactionStatus.PENDING)

    def test_two_verifiers_produce_one_provider_call_and_one_stock_effect(self):
        payment = PaymentTransaction.objects.create(
            order=self.order,
            user=self.user,
            provider="fake",
            amount=Decimal("1000"),
            status=PaymentTransactionStatus.CALLBACK_RECEIVED,
            gateway_authority="RACE-AUTHORITY",
            callback_payload={"status": "OK"},
            idempotency_key=f"race:{uuid4()}",
        )
        provider = BlockingVerificationProvider()
        outcomes = []

        def run_verification():
            close_old_connections()
            try:
                verify_payment(transaction_id=payment.pk, user=BaseUser.objects.get(pk=self.user.pk))
            except PaymentError:
                outcomes.append("blocked")
            else:
                outcomes.append("applied")
            finally:
                close_old_connections()

        with patch("cheatgame.shop.payments.services.get_payment_provider", return_value=provider):
            first = Thread(target=run_verification)
            first.start()
            self.assertTrue(provider.entered.wait(10))
            second = Thread(target=run_verification)
            second.start()
            second.join(10)
            provider.release.set()
            first.join(10)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertCountEqual(outcomes, ["applied", "blocked"])
        self.assertEqual(provider.calls, 1)
        self.assertFalse(provider.inside_atomic)
        payment.refresh_from_db()
        self.order.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(payment.status, PaymentTransactionStatus.PAID)
        self.assertEqual(self.order.payment_status, OrderStatus.PAID)
        self.assertEqual(self.product.quantity, 0)
