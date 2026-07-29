from datetime import timedelta
from io import StringIO
from threading import Barrier, Lock, Thread
from unittest.mock import patch
from uuid import uuid4

from django.db import DatabaseError, close_old_connections
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

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
from cheatgame.digital_products.services.checkout_preparation import (
    prepare_digital_checkout,
)
from cheatgame.financial_core.models import (
    CommercialAccountingPolicyVersion,
    CommercialFinalization,
    CommercialFinalizationWorkItem,
    DigitalFulfillmentObligation,
    FinancialAccount,
    FinancialAccountType,
    FinancialAllocation,
    FinalizationWorkStatus,
    MoneyUnit,
    PaymentTenderType,
    PaymentTransactionOperation,
    ProviderRequestOutcome,
    ReviewCase,
    VerificationWorkItem,
    VerificationWorkStatus,
    VerificationWorkType,
)
from cheatgame.financial_core.services.adapters import (
    ADAPTER_CONTRACT_VERSION,
    ProviderAdapterRegistry,
)
from cheatgame.financial_core.services.placement import (
    place_order_and_create_payment_obligation,
)
from cheatgame.financial_core.services.provider_requests import (
    apply_provider_request_result,
    claim_provider_request,
    create_or_replay_payment_attempt,
    create_or_replay_request_transaction,
)
from cheatgame.financial_core.services.runtime import (
    execute_runtime_work,
    make_runtime_work_due,
    run_runtime_batch,
    runtime_stats,
)
from cheatgame.financial_core.services.verification import (
    enqueue_verification_work,
)
from cheatgame.financial_core.test_commercial_finalizer_phase1 import (
    CommercialFinalizerFixture,
)
from cheatgame.financial_core.test_verification_worker_api import WorkerAdapter
from cheatgame.product.models import (
    DeliveredVersion,
    NativeConsole,
    ProductCommerceAuthority,
)
from cheatgame.shop.models import Cart


class DurableFinancialRuntimeTests(CommercialFinalizerFixture, TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        super().setUp()
        self.adapter = WorkerAdapter()
        self.registry = ProviderAdapterRegistry(
            {("synthetic", ADAPTER_CONTRACT_VERSION): self.adapter}
        )

    def make_digital_runtime_graph(self):
        user = self.make_user()
        product = self.make_product(
            authority=ProductCommerceAuthority.DIGITAL_PRODUCTS,
            price=9000,
        )
        version = DeliveredVersion.objects.create(
            product=product,
            native_console=NativeConsole.PS4,
        )
        pool = InventoryPool.objects.create(
            sellable_quantity=2,
            status=InventoryPoolStatus.ENABLED,
        )
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
        checkout, _ = prepare_digital_checkout(
            actor=user,
            client_checkout_uuid=uuid4(),
        )
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
            transaction_id=transaction_obj.pk,
            claim_idempotency_key=uuid4(),
        )
        apply_provider_request_result(
            transaction_id=transaction_obj.pk,
            claim_token=request_claim.claim.claim_token,
            outcome=ProviderRequestOutcome.ACCEPTED_PENDING,
            evidence_hash="1" * 64,
            result_idempotency_key=uuid4(),
        )
        transaction_obj.refresh_from_db()
        work, _ = enqueue_verification_work(
            transaction_obj=transaction_obj,
            work_type=VerificationWorkType.POLL_PENDING_OPERATION,
            deterministic_identity=f"runtime-poll:{transaction_obj.public_id}",
            correlation_id=transaction_obj.correlation_id,
        )
        _, _, liability = self.accounting_policy(account)
        merchandise = FinancialAccount.objects.create(
            key=f"digital-runtime-revenue:{uuid4()}",
            name="Digital runtime revenue",
            account_type=FinancialAccountType.REVENUE,
        )
        shipping = FinancialAccount.objects.create(
            key=f"digital-runtime-shipping:{uuid4()}",
            name="Unused Digital runtime shipping",
            account_type=FinancialAccountType.REVENUE,
        )
        CommercialAccountingPolicyVersion.objects.create(
            policy_key="commercial-digital-runtime-v1",
            version=1,
            commerce_authority="digital_products",
            customer_unapplied_funds_account=liability,
            merchandise_revenue_account=merchandise,
            shipping_revenue_account=shipping,
            active_for_new_finalizations=True,
        )
        return placement, pool, work

    def run_verification(self, work):
        return execute_runtime_work(
            stage="verification",
            work_id=work.pk,
            adapter_registry=self.registry,
        )

    def recognition_work(self):
        return VerificationWorkItem.objects.get(
            work_type=VerificationWorkType.APPLY_VERIFIED_FUNDS
        )

    def test_bounded_batch_executes_mixed_work_in_authoritative_order(self):
        placement, pool, _ = self.make_digital_runtime_graph()
        result = run_runtime_batch(limit=3, adapter_registry=self.registry)
        self.assertEqual(
            [item.stage for item in result.results],
            ["verification", "recognition", "finalization"],
        )
        self.assertTrue(FinancialAllocation.objects.filter(payment=placement.payment).exists())
        self.assertTrue(CommercialFinalization.objects.filter(payment=placement.payment).exists())
        self.assertTrue(
            DigitalFulfillmentObligation.objects.filter(
                finalization__payment=placement.payment
            ).exists()
        )
        reservation = DigitalInventoryReservation.objects.get(order=placement.order)
        self.assertEqual(
            reservation.state,
            DigitalInventoryReservationState.CONSUMED,
        )
        pool.refresh_from_db()
        self.assertEqual(pool.sellable_quantity, 1)

    def test_completed_work_replays_without_duplicate_financial_effect(self):
        placement, _, verification_work = self.make_digital_runtime_graph()
        run_runtime_batch(limit=3, adapter_registry=self.registry)
        recognition = self.recognition_work()
        finalization = CommercialFinalizationWorkItem.objects.get(
            payment=placement.payment
        )
        results = (
            self.run_verification(verification_work),
            execute_runtime_work(stage="recognition", work_id=recognition.pk),
            execute_runtime_work(stage="finalization", work_id=finalization.pk),
        )
        self.assertEqual([item.outcome for item in results], ["replayed"] * 3)
        self.assertEqual(FinancialAllocation.objects.filter(payment=placement.payment).count(), 1)
        self.assertEqual(CommercialFinalization.objects.filter(payment=placement.payment).count(), 1)

    def test_expired_recognition_lease_is_recovered(self):
        placement, _, verification_work = self.make_digital_runtime_graph()
        self.run_verification(verification_work)
        work = self.recognition_work()
        expired = timezone.now() - timedelta(seconds=1)
        VerificationWorkItem.objects.filter(pk=work.pk).update(
            status=VerificationWorkStatus.CLAIMED,
            attempt_count=1,
            claim_token=uuid4(),
            claimed_at=expired - timedelta(seconds=60),
            claim_expires_at=expired,
        )
        result = execute_runtime_work(stage="recognition", work_id=work.pk)
        work.refresh_from_db()
        self.assertEqual(result.outcome, "completed")
        self.assertEqual(work.status, VerificationWorkStatus.COMPLETED)
        self.assertEqual(work.attempt_count, 2)
        self.assertEqual(FinancialAllocation.objects.filter(payment=placement.payment).count(), 1)

    @override_settings(
        FINANCIAL_RUNTIME_RETRY_BASE_SECONDS=60,
        FINANCIAL_RUNTIME_RETRY_MAX_SECONDS=60,
    )
    def test_operational_failure_backs_off_and_manual_retry_recovers(self):
        placement, _, verification_work = self.make_digital_runtime_graph()
        self.run_verification(verification_work)
        work = self.recognition_work()
        with patch(
            "cheatgame.financial_core.services.runtime.recognize_verified_funds",
            side_effect=DatabaseError("synthetic runtime interruption"),
        ):
            failed = execute_runtime_work(stage="recognition", work_id=work.pk)
        work.refresh_from_db()
        self.assertEqual(failed.outcome, "retry_scheduled")
        self.assertEqual(work.status, VerificationWorkStatus.WAITING)
        self.assertGreater(work.next_attempt_at, timezone.now())
        make_runtime_work_due(stage="recognition", work_id=work.pk)
        recovered = execute_runtime_work(stage="recognition", work_id=work.pk)
        self.assertEqual(recovered.outcome, "completed")
        self.assertEqual(FinancialAllocation.objects.filter(payment=placement.payment).count(), 1)

    def test_retry_exhaustion_cancels_work_and_opens_review(self):
        _, _, verification_work = self.make_digital_runtime_graph()
        self.run_verification(verification_work)
        work = self.recognition_work()
        VerificationWorkItem.objects.filter(pk=work.pk).update(
            attempt_count=work.max_attempts - 1
        )
        with patch(
            "cheatgame.financial_core.services.runtime.recognize_verified_funds",
            side_effect=DatabaseError("synthetic exhausted runtime"),
        ):
            result = execute_runtime_work(stage="recognition", work_id=work.pk)
        work.refresh_from_db()
        self.assertEqual(result.outcome, "review_required")
        self.assertEqual(work.status, VerificationWorkStatus.CANCELED)
        self.assertTrue(ReviewCase.objects.filter(payment=work.transaction.attempt.payment).exists())

    def test_concurrent_recognition_converges_on_one_allocation(self):
        placement, _, verification_work = self.make_digital_runtime_graph()
        self.run_verification(verification_work)
        work = self.recognition_work()
        barrier = Barrier(2)
        guard = Lock()
        outcomes = []
        errors = []

        def invoke():
            close_old_connections()
            try:
                barrier.wait()
                result = execute_runtime_work(stage="recognition", work_id=work.pk)
                with guard:
                    outcomes.append(result.outcome)
            except Exception as exc:
                with guard:
                    errors.append(type(exc).__name__)
            finally:
                close_old_connections()

        first = Thread(target=invoke)
        second = Thread(target=invoke)
        first.start()
        second.start()
        first.join()
        second.join()
        self.assertEqual(errors, [])
        self.assertIn("completed", outcomes)
        self.assertTrue(set(outcomes).issubset({"completed", "replayed", "skipped"}))
        self.assertEqual(FinancialAllocation.objects.filter(payment=placement.payment).count(), 1)

    def test_stats_expose_counts_age_retries_failures_and_reviews(self):
        _, _, work = self.make_digital_runtime_graph()
        VerificationWorkItem.objects.filter(pk=work.pk).update(attempt_count=1)
        stats = runtime_stats(now=timezone.now() + timedelta(seconds=5))
        self.assertEqual(stats["verification"]["count"], 1)
        self.assertEqual(stats["verification"]["retry_count"], 1)
        self.assertEqual(stats["verification"]["pending_count"], 1)
        self.assertEqual(stats["verification"]["claimed_count"], 0)
        self.assertEqual(stats["verification"]["due_count"], 1)
        self.assertEqual(stats["verification"]["retryable_failure_count"], 0)
        self.assertGreaterEqual(stats["verification"]["oldest_age_seconds"], 5)
        self.assertEqual(stats["recognition"]["count"], 0)
        self.assertEqual(stats["finalization"]["count"], 0)
        self.assertEqual(stats["failed_work"], 0)
        self.assertEqual(stats["review_required"], 0)
        self.assertEqual(stats["pending_total"], 1)
        self.assertEqual(stats["claimed_total"], 0)

    def test_command_returns_nonzero_for_unresolved_processing_failure(self):
        _, _, verification_work = self.make_digital_runtime_graph()
        self.run_verification(verification_work)
        work = self.recognition_work()
        output = StringIO()

        with patch(
            "cheatgame.financial_core.services.runtime.recognize_verified_funds",
            side_effect=DatabaseError("synthetic supervised failure"),
        ), self.assertRaises(CommandError):
            call_command(
                "financial_runtime",
                "run-one",
                "--stage",
                "recognition",
                "--work-id",
                str(work.pk),
                "--apply",
                stdout=output,
            )

        self.assertIn('"retry_scheduled": 1', output.getvalue())
        self.assertIn('"failed": 1', output.getvalue())
