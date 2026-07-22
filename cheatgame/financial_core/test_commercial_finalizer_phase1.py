from threading import Barrier, Thread
from unittest import skipUnless
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.test import TransactionTestCase

from cheatgame.financial_core.models import (
    CommercialAccountingPolicyVersion,
    CommercialFinalization,
    CommercialFinalizationWorkItem,
    DigitalFulfillmentObligation,
    FinancialAccount,
    FinancialAccountType,
    JournalEntry,
    PaymentCollectionStatus,
    PaymentTenderType,
    PaymentTransactionOperation,
    ProviderRequestOutcome,
    StandardFulfillmentObligation,
)
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
from cheatgame.financial_core.services.commercial_finalization import (
    CommercialFinalizationBlocked,
    finalize_commercial_work_item,
)
from cheatgame.financial_core.services.funds_application import apply_verified_funds
from cheatgame.financial_core.services.idempotency import IdempotencyConflict
from cheatgame.financial_core.services.placement import place_order_and_create_payment_obligation
from cheatgame.financial_core.services.provider_requests import (
    apply_provider_request_result,
    claim_provider_request,
    create_or_replay_payment_attempt,
    create_or_replay_request_transaction,
)
from cheatgame.financial_core.services.verification import apply_verification_result
from cheatgame.financial_core.test_provider_execution_phase1 import ProviderExecutionPhase1Fixture
from cheatgame.financial_core.models import MoneyUnit, VerificationTriggerSource
from cheatgame.product.models import DeliveredVersion, NativeConsole, ProductCommerceAuthority
from cheatgame.shop.models import Cart, CartState, CheckoutStatus, FulfillmentStatus, OrderStatus, StockReservationState


class CommercialFinalizerFixture(ProviderExecutionPhase1Fixture):
    def ready_standard(self):
        graph, _, _, liability, _ = self.apply_success()
        placement = graph[0]
        merchandise = FinancialAccount.objects.create(
            key=f"merchandise-revenue:{uuid4()}",
            name="Synthetic merchandise revenue",
            account_type=FinancialAccountType.REVENUE,
        )
        shipping = FinancialAccount.objects.create(
            key=f"shipping-revenue:{uuid4()}",
            name="Synthetic shipping revenue",
            account_type=FinancialAccountType.REVENUE,
        )
        policy = CommercialAccountingPolicyVersion.objects.create(
            policy_key="commercial-standard-v1",
            version=1,
            commerce_authority="standard_commerce",
            customer_unapplied_funds_account=liability,
            merchandise_revenue_account=merchandise,
            shipping_revenue_account=shipping,
            active_for_new_finalizations=True,
        )
        return placement, policy

    def ready_digital(self):
        user = self.make_user()
        product = self.make_product(authority=ProductCommerceAuthority.DIGITAL_PRODUCTS, price=9000)
        version = DeliveredVersion.objects.create(product=product, native_console=NativeConsole.PS4)
        pool = InventoryPool.objects.create(sellable_quantity=2, status=InventoryPoolStatus.ENABLED)
        offer = DigitalOffer.objects.create(
            delivered_version=version,
            customer_console=NativeConsole.PS4,
            capacity=DigitalOfferCapacity.CAPACITY_1,
            price=9000,
            inventory_pool=pool,
            sale_state=DigitalOfferSaleState.ACTIVE,
        )
        cart = Cart.objects.create(user=user)
        add_digital_offer_to_cart(
            cart=cart,
            offer=offer,
            fulfillment_method=DigitalCartFulfillmentMethod.IN_STORE,
            actor=user,
        )
        checkout, _ = prepare_digital_checkout(actor=user, client_checkout_uuid=uuid4())
        placement = place_order_and_create_payment_obligation(
            checkout_id=checkout.pk,
            expected_user_id=user.pk,
            expected_checkout_version=checkout.version,
            source_unit=MoneyUnit.IRR,
            idempotency_key=uuid4(),
        )
        _, _, account = self.make_account()
        attempt = create_or_replay_payment_attempt(
            payment_id=placement.payment.pk,
            merchant_account_version_id=account.pk,
            tender_type=PaymentTenderType.EXTERNAL_PROVIDER,
            requested_amount=placement.payment.amount_due,
            idempotency_key=uuid4(),
        ).attempt
        transaction_obj = create_or_replay_request_transaction(
            attempt_id=attempt.pk,
            operation_type=PaymentTransactionOperation.SALE,
            idempotency_key=uuid4(),
        ).transaction
        request_claim = claim_provider_request(
            transaction_id=transaction_obj.pk, claim_idempotency_key=uuid4()
        )
        apply_provider_request_result(
            transaction_id=transaction_obj.pk,
            claim_token=request_claim.claim.claim_token,
            outcome=ProviderRequestOutcome.ACCEPTED_PENDING,
            evidence_hash="1" * 64,
            result_idempotency_key=uuid4(),
        )
        transaction_obj.refresh_from_db()
        _, verification_claim = self.verification_claim(transaction_obj, account)
        verification = apply_verification_result(
            claim_token=verification_claim.claim.claim_token,
            result=self.normalized_result(transaction_obj, account),
            result_idempotency_key=uuid4(),
            trigger_source=VerificationTriggerSource.CALLBACK,
        )
        _, _, liability = self.accounting_policy(account)
        placement.payment.refresh_from_db()
        apply_verified_funds(
            verification_id=verification.pk,
            idempotency_key=uuid4(),
            expected_payment_version=placement.payment.version,
            correlation_id=uuid4(),
        )
        merchandise = FinancialAccount.objects.create(
            key=f"digital-revenue:{uuid4()}", name="Synthetic digital revenue", account_type=FinancialAccountType.REVENUE
        )
        shipping = FinancialAccount.objects.create(
            key=f"digital-shipping:{uuid4()}", name="Unused digital shipping", account_type=FinancialAccountType.REVENUE
        )
        CommercialAccountingPolicyVersion.objects.create(
            policy_key="commercial-digital-v1",
            version=1,
            commerce_authority="digital_products",
            customer_unapplied_funds_account=liability,
            merchandise_revenue_account=merchandise,
            shipping_revenue_account=shipping,
            active_for_new_finalizations=True,
        )
        return placement, pool

    def finalize(self, placement, *, key=None, expected_version=None):
        placement.payment.refresh_from_db()
        work = CommercialFinalizationWorkItem.objects.get(payment=placement.payment)
        expected_work_version = (
            work.version - 2 if work.status == "completed" else work.version
        )
        return finalize_commercial_work_item(
            work_item_public_id=work.public_id,
            idempotency_key=key or uuid4(),
            expected_work_item_version=expected_work_version,
            expected_payment_version=(
                placement.payment.version if expected_version is None else expected_version
            ),
            correlation_id=uuid4(),
        )


class CommercialFinalizerPhase1Tests(CommercialFinalizerFixture, TransactionTestCase):
    reset_sequences = True

    def test_standard_finalization_is_atomic_and_exactly_once(self):
        placement, policy = self.ready_standard()
        product = placement.order.order_items.get().product
        before = product.quantity
        result = self.finalize(placement)
        placement.payment.refresh_from_db()
        placement.order.refresh_from_db()
        placement.order.checkout.refresh_from_db()
        placement.order.checkout.cart.refresh_from_db()
        product.refresh_from_db()
        reservation = placement.order.stock_reservations.get()
        self.assertEqual(placement.payment.collection_status, PaymentCollectionStatus.PAID)
        self.assertEqual(placement.order.payment_status, OrderStatus.PAID)
        self.assertEqual(placement.order.fulfillment_status, FulfillmentStatus.PROCESSING)
        self.assertEqual(placement.order.checkout.status, CheckoutStatus.PAID)
        self.assertEqual(placement.order.checkout.cart.state, CartState.OPEN)
        self.assertIsNone(placement.order.checkout.cart.active_checkout_id)
        self.assertEqual(reservation.state, StockReservationState.CONSUMED)
        self.assertEqual(product.quantity, before - reservation.quantity)
        self.assertEqual(StandardFulfillmentObligation.objects.count(), 1)
        self.assertEqual(result.finalization.accounting_policy_version_id, policy.pk)

    def test_receipt_liability_is_reclassified_not_provider_clearing(self):
        placement, policy = self.ready_standard()
        result = self.finalize(placement)
        postings = list(result.finalization.journal_entry.postings.order_by("line_number"))
        self.assertEqual(result.finalization.journal_entry.source_type, "commercial_reclassification")
        self.assertEqual(postings[0].account_id, policy.customer_unapplied_funds_account_id)
        self.assertEqual(postings[0].direction, "debit")
        self.assertEqual(sum(p.amount for p in postings if p.direction == "debit"), placement.payment.amount_due)
        self.assertEqual(sum(p.amount for p in postings if p.direction == "credit"), placement.payment.amount_due)

    def test_duplicate_retry_replays_without_second_effect(self):
        placement, _ = self.ready_standard()
        placement.payment.refresh_from_db()
        version = placement.payment.version
        key = uuid4()
        first = self.finalize(placement, key=key, expected_version=version)
        second = self.finalize(placement, key=key, expected_version=version)
        self.assertTrue(second.replayed)
        self.assertEqual(first.finalization.pk, second.finalization.pk)
        self.assertEqual(CommercialFinalization.objects.count(), 1)
        self.assertEqual(JournalEntry.objects.filter(source_type="commercial_reclassification").count(), 1)

    def test_idempotency_mismatch_conflicts(self):
        placement, _ = self.ready_standard()
        placement.payment.refresh_from_db()
        version = placement.payment.version
        key = uuid4()
        self.finalize(placement, key=key, expected_version=version)
        with self.assertRaises(IdempotencyConflict):
            self.finalize(placement, key=key, expected_version=version + 1)

    def test_journal_failure_rolls_back_inventory_order_and_payment(self):
        placement, _ = self.ready_standard()
        product = placement.order.order_items.get().product
        before = product.quantity
        with patch(
            "cheatgame.financial_core.services.commercial_finalization.post_balanced_journal_entry_under_lock",
            side_effect=ValidationError("synthetic journal failure"),
        ):
            with self.assertRaises(ValidationError):
                self.finalize(placement)
        placement.payment.refresh_from_db()
        placement.order.refresh_from_db()
        product.refresh_from_db()
        reservation = placement.order.stock_reservations.get()
        self.assertEqual(placement.payment.collection_status, PaymentCollectionStatus.PAID_PENDING_FINALIZATION)
        self.assertEqual(placement.order.payment_status, OrderStatus.PENDDING)
        self.assertEqual(reservation.state, StockReservationState.PAYMENT_HOLD)
        self.assertEqual(product.quantity, before)
        self.assertFalse(CommercialFinalization.objects.exists())
        self.assertFalse(StandardFulfillmentObligation.objects.exists())

    def test_inventory_cannot_disappear_behind_payment_hold_or_lose_paid_truth(self):
        placement, _ = self.ready_standard()
        product = placement.order.order_items.get().product
        original_quantity = product.quantity
        with self.assertRaises(DatabaseError), transaction.atomic():
            product.quantity = 0
            product.save(update_fields=("quantity", "updated_at"))
        placement.payment.refresh_from_db()
        product.refresh_from_db()
        self.assertEqual(placement.payment.collection_status, PaymentCollectionStatus.PAID_PENDING_FINALIZATION)
        self.assertEqual(product.quantity, original_quantity)
        self.assertFalse(CommercialFinalization.objects.exists())

    @skipUnless(connection.vendor == "postgresql", "PostgreSQL trigger guards require PostgreSQL.")
    def test_raw_sql_paid_forgery_and_finalization_mutation_are_blocked(self):
        placement, _ = self.ready_standard()
        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE financial_core_payment SET collection_status = 'paid' WHERE id = %s",
                    [placement.payment.pk],
                )
        finalization = self.finalize(placement).finalization
        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE financial_core_commercialfinalization SET amount = amount + 1 WHERE id = %s",
                    [finalization.pk],
                )
        with self.assertRaises(DatabaseError), transaction.atomic():
            type(placement.order).objects.filter(pk=placement.order.pk).update(payment_status=OrderStatus.FAIDED)
        with self.assertRaises(DatabaseError), transaction.atomic():
            type(placement.order.checkout).objects.filter(pk=placement.order.checkout_id).update(
                status=CheckoutStatus.CANCELED
            )

    def test_no_digital_fulfillment_is_created_for_standard_order(self):
        placement, _ = self.ready_standard()
        self.finalize(placement)
        self.assertFalse(DigitalFulfillmentObligation.objects.exists())

    def test_digital_finalization_consumes_pool_and_creates_obligation(self):
        placement, pool = self.ready_digital()
        self.finalize(placement)
        pool.refresh_from_db()
        reservation = DigitalInventoryReservation.objects.get(order=placement.order)
        self.assertEqual(pool.sellable_quantity, 1)
        self.assertEqual(reservation.state, DigitalInventoryReservationState.CONSUMED)
        self.assertEqual(DigitalFulfillmentObligation.objects.filter(order=placement.order).count(), 1)
        self.assertFalse(StandardFulfillmentObligation.objects.exists())


class CommercialFinalizerConcurrencyTests(CommercialFinalizerFixture, TransactionTestCase):
    reset_sequences = True

    def test_two_concurrent_finalizers_create_one_result(self):
        placement, _ = self.ready_standard()
        placement.payment.refresh_from_db()
        version = placement.payment.version
        work = CommercialFinalizationWorkItem.objects.get(payment=placement.payment)
        work_version = work.version
        barrier = Barrier(2)
        outcomes = []

        def runner():
            close_old_connections()
            try:
                barrier.wait()
                result = finalize_commercial_work_item(
                    work_item_public_id=work.public_id,
                    idempotency_key=uuid4(),
                    expected_work_item_version=work_version,
                    expected_payment_version=version,
                    correlation_id=uuid4(),
                )
                outcomes.append(("ok", result.replayed))
            except Exception as exc:
                outcomes.append(("error", type(exc).__name__))
            finally:
                close_old_connections()

        threads = [Thread(target=runner) for _ in range(2)]
        for item in threads:
            item.start()
        for item in threads:
            item.join(timeout=30)
        self.assertTrue(all(not item.is_alive() for item in threads))
        self.assertEqual(len(outcomes), 2)
        self.assertEqual(CommercialFinalization.objects.count(), 1)
        self.assertEqual(StandardFulfillmentObligation.objects.count(), 1)
        self.assertEqual(JournalEntry.objects.filter(source_type="commercial_reclassification").count(), 1)
