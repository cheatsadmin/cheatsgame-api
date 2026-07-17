from decimal import Decimal
from threading import Barrier, Thread
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.test import TransactionTestCase

from cheatgame.financial_core.models import (
    CANONICAL_CURRENCY,
    CommercialFinalizationWorkItem,
    FinancialAccount,
    FinancialAccountStatus,
    FinancialAccountType,
    FinancialAllocation,
    FinancialEvent,
    JournalEntry,
    JournalPosting,
    PaymentAttemptStatus,
    PaymentCollectionStatus,
    PaymentTransactionStatus,
    PostingDirection,
    ReceiptAccountingPolicyVersion,
    ReviewCase,
    ReviewCaseReason,
    VerificationApplicationState,
    VerificationEvidenceBasis,
    VerificationOutcome,
    VerificationTriggerSource,
)
from cheatgame.financial_core.services.funds_application import (
    FINALIZER_VERSION,
    FundsApplicationBlocked,
    apply_verified_funds,
)
from cheatgame.financial_core.services.idempotency import IdempotencyConflict
from cheatgame.financial_core.services.verification import apply_verification_result
from cheatgame.financial_core.test_c2b1 import C2B1Fixture
from cheatgame.shop.models import OrderStatus, StockReservationState


class ProviderExecutionPhase1Fixture(C2B1Fixture):
    def successful_verification(self, *, price=1000, result_overrides=None):
        placement, account, attempt, transaction_obj = self.make_pending_graph()
        _, claim = self.verification_claim(transaction_obj, account)
        result = self.normalized_result(
            transaction_obj,
            account,
            **(result_overrides or {}),
        )
        verification = apply_verification_result(
            claim_token=claim.claim.claim_token,
            result=result,
            result_idempotency_key=uuid4(),
            trigger_source=VerificationTriggerSource.CALLBACK,
        )
        placement.payment.refresh_from_db()
        attempt.refresh_from_db()
        transaction_obj.refresh_from_db()
        return placement, account, attempt, transaction_obj, verification

    def accounting_policy(self, account, *, active=True, clearing_currency=CANONICAL_CURRENCY):
        clearing = FinancialAccount.objects.create(
            key=f"provider-clearing:{uuid4()}",
            name="Synthetic provider clearing",
            account_type=FinancialAccountType.ASSET,
            currency=clearing_currency,
        )
        liability = FinancialAccount.objects.create(
            key=f"customer-unapplied:{uuid4()}",
            name="Synthetic customer unapplied funds",
            account_type=FinancialAccountType.LIABILITY,
            currency=CANONICAL_CURRENCY,
        )
        policy = ReceiptAccountingPolicyVersion.objects.create(
            merchant_account_version=account,
            policy_key="provider-receipt-v1",
            version=1,
            provider_clearing_account=clearing,
            customer_unapplied_funds_account=liability,
            active_for_new_applications=active,
        )
        return policy, clearing, liability

    def apply_success(self):
        graph = self.successful_verification()
        placement, account, _, _, verification = graph
        policy, clearing, liability = self.accounting_policy(account)
        result = apply_verified_funds(
            verification_id=verification.pk,
            idempotency_key=uuid4(),
            expected_payment_version=placement.payment.version,
            correlation_id=uuid4(),
        )
        return graph, policy, clearing, liability, result


class ProviderExecutionPhase1Tests(ProviderExecutionPhase1Fixture, TransactionTestCase):
    reset_sequences = True

    def test_exact_authenticated_success_is_recognized_atomically(self):
        (placement, _, attempt, transaction_obj, verification), policy, clearing, liability, result = self.apply_success()
        placement.payment.refresh_from_db()
        attempt.refresh_from_db()
        transaction_obj.refresh_from_db()
        allocation = result.allocation
        self.assertFalse(result.replayed)
        self.assertEqual(allocation.verification_id, verification.pk)
        self.assertEqual(allocation.accounting_policy_version_id, policy.pk)
        self.assertEqual(attempt.status, PaymentAttemptStatus.SUCCEEDED)
        self.assertEqual(transaction_obj.status, PaymentTransactionStatus.SUCCEEDED)
        self.assertEqual(placement.payment.confirmed_amount, placement.payment.amount_due)
        self.assertEqual(placement.payment.collection_status, PaymentCollectionStatus.PAID_PENDING_FINALIZATION)
        postings = list(allocation.journal_entry.postings.order_by("line_number"))
        self.assertEqual(len(postings), 2)
        self.assertEqual((postings[0].account_id, postings[0].direction), (clearing.pk, PostingDirection.DEBIT))
        self.assertEqual((postings[1].account_id, postings[1].direction), (liability.pk, PostingDirection.CREDIT))
        self.assertEqual(sum(item.amount if item.direction == PostingDirection.DEBIT else -item.amount for item in postings), 0)

    def test_commercial_state_inventory_and_holds_are_untouched(self):
        graph, _, _, _, _ = self.apply_success()
        placement = graph[0]
        placement.order.refresh_from_db()
        reservation = placement.order.stock_reservations.get()
        reservation.product.refresh_from_db()
        self.assertEqual(placement.order.payment_status, OrderStatus.PENDDING)
        self.assertEqual(reservation.state, StockReservationState.PAYMENT_HOLD)
        self.assertEqual(reservation.product.quantity, 20)

    def test_success_creates_one_dormant_finalization_work_and_review(self):
        graph, _, _, _, result = self.apply_success()
        payment = graph[0].payment
        work = CommercialFinalizationWorkItem.objects.get(payment=payment)
        self.assertEqual(work.finalizer_version, FINALIZER_VERSION)
        self.assertEqual(work.status, "pending")
        self.assertTrue(ReviewCase.objects.filter(payment=payment, reason=ReviewCaseReason.PAID_PENDING_FINALIZATION).exists())
        self.assertTrue(FinancialEvent.objects.filter(event_type="payment.paid_pending_finalization").exists())
        self.assertEqual(result.allocation.journal_entry.source_type, "provider_receipt")

    def test_duplicate_application_replays_without_double_counting(self):
        placement, account, _, _, verification = self.successful_verification()
        self.accounting_policy(account)
        key = uuid4()
        correlation = uuid4()
        first = apply_verified_funds(
            verification_id=verification.pk,
            idempotency_key=key,
            expected_payment_version=placement.payment.version,
            correlation_id=correlation,
        )
        second = apply_verified_funds(
            verification_id=verification.pk,
            idempotency_key=key,
            expected_payment_version=placement.payment.version,
            correlation_id=correlation,
        )
        placement.payment.refresh_from_db()
        self.assertTrue(second.replayed)
        self.assertEqual(first.allocation.pk, second.allocation.pk)
        self.assertEqual(FinancialAllocation.objects.count(), 1)
        self.assertEqual(JournalEntry.objects.filter(source_type="provider_receipt").count(), 1)
        self.assertEqual(placement.payment.confirmed_amount, placement.payment.amount_due)

    def test_idempotency_payload_mismatch_conflicts(self):
        placement, account, _, _, verification = self.successful_verification()
        self.accounting_policy(account)
        key = uuid4()
        correlation = uuid4()
        apply_verified_funds(
            verification_id=verification.pk,
            idempotency_key=key,
            expected_payment_version=placement.payment.version,
            correlation_id=correlation,
        )
        with self.assertRaises(IdempotencyConflict):
            apply_verified_funds(
                verification_id=verification.pk,
                idempotency_key=key,
                expected_payment_version=placement.payment.version + 99,
                correlation_id=correlation,
            )

    def test_missing_policy_preserves_success_blocker_and_opens_review(self):
        placement, _, attempt, transaction_obj, verification = self.successful_verification()
        with self.assertRaises(FundsApplicationBlocked):
            apply_verified_funds(
                verification_id=verification.pk,
                idempotency_key=uuid4(),
                expected_payment_version=placement.payment.version,
                correlation_id=uuid4(),
            )
        placement.payment.refresh_from_db()
        attempt.refresh_from_db()
        transaction_obj.refresh_from_db()
        self.assertEqual(placement.payment.confirmed_amount, 0)
        self.assertEqual(attempt.status, PaymentAttemptStatus.REVIEW)
        self.assertEqual(transaction_obj.status, PaymentTransactionStatus.REVIEW)
        self.assertEqual(FinancialAllocation.objects.count(), 0)
        self.assertEqual(JournalEntry.objects.count(), 0)
        self.assertTrue(ReviewCase.objects.filter(payment=placement.payment, reason=ReviewCaseReason.ACCOUNTING_POLICY_MISSING).exists())

    def test_journal_failure_rolls_back_every_financial_projection(self):
        placement, account, attempt, transaction_obj, verification = self.successful_verification()
        self.accounting_policy(account)
        with patch(
            "cheatgame.financial_core.services.funds_application.post_balanced_journal_entry_under_lock",
            side_effect=ValidationError("synthetic journal failure"),
        ):
            with self.assertRaises(FundsApplicationBlocked):
                apply_verified_funds(
                    verification_id=verification.pk,
                    idempotency_key=uuid4(),
                    expected_payment_version=placement.payment.version,
                    correlation_id=uuid4(),
                )
        placement.payment.refresh_from_db()
        attempt.refresh_from_db()
        transaction_obj.refresh_from_db()
        self.assertEqual(placement.payment.confirmed_amount, 0)
        self.assertEqual(attempt.status, PaymentAttemptStatus.REVIEW)
        self.assertEqual(transaction_obj.status, PaymentTransactionStatus.REVIEW)
        self.assertFalse(FinancialAllocation.objects.exists())
        self.assertFalse(JournalEntry.objects.exists())
        self.assertTrue(ReviewCase.objects.filter(reason=ReviewCaseReason.PROVIDER_RECEIPT_JOURNAL_FAILED).exists())

    def test_ineligible_or_unauthenticated_evidence_cannot_apply(self):
        placement, account, _, _, verification = self.successful_verification(
            result_overrides={"evidence_basis": VerificationEvidenceBasis.NONE}
        )
        self.accounting_policy(account)
        with self.assertRaises(FundsApplicationBlocked):
            apply_verified_funds(
                verification_id=verification.pk,
                idempotency_key=uuid4(),
                expected_payment_version=placement.payment.version,
                correlation_id=uuid4(),
            )
        self.assertFalse(FinancialAllocation.objects.exists())

    def test_pending_verification_cannot_apply(self):
        placement, account, _, transaction_obj = self.make_pending_graph()
        _, claim = self.verification_claim(transaction_obj, account)
        verification = apply_verification_result(
            claim_token=claim.claim.claim_token,
            result=self.normalized_result(transaction_obj, account, outcome=VerificationOutcome.PENDING),
            result_idempotency_key=uuid4(),
            trigger_source=VerificationTriggerSource.CALLBACK,
        )
        self.accounting_policy(account)
        placement.payment.refresh_from_db()
        with self.assertRaises(FundsApplicationBlocked):
            apply_verified_funds(
                verification_id=verification.pk,
                idempotency_key=uuid4(),
                expected_payment_version=placement.payment.version,
                correlation_id=uuid4(),
            )

    def test_policy_identity_and_allocation_are_immutable(self):
        _, policy, _, _, result = self.apply_success()
        with self.assertRaises(ValidationError):
            policy.delete()
        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE financial_core_financialallocation SET amount = amount + 1 WHERE id = %s",
                    [result.allocation.pk],
                )

    def test_raw_sql_confirmed_amount_forgery_is_rejected_at_commit(self):
        placement, _, _, _, _ = self.successful_verification()
        with self.assertRaises(DatabaseError):
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE financial_core_payment SET confirmed_amount = amount_due WHERE id = %s",
                        [placement.payment.pk],
                    )

    def test_raw_sql_paid_transition_is_rejected(self):
        graph, _, _, _, _ = self.apply_success()
        payment = graph[0].payment
        with self.assertRaises(DatabaseError):
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE financial_core_payment SET collection_status = 'paid' WHERE id = %s",
                        [payment.pk],
                    )

    def test_successful_attempt_and_transaction_cannot_downgrade(self):
        graph, _, _, _, _ = self.apply_success()
        _, _, attempt, transaction_obj, _ = graph
        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE financial_core_paymentattempt SET status = 'processing' WHERE id = %s",
                    [attempt.pk],
                )
        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE financial_core_paymenttransaction SET status = 'review' WHERE id = %s",
                    [transaction_obj.pk],
                )

    def test_payment_cannot_reopen_or_cancel_after_allocation(self):
        graph, _, _, _, _ = self.apply_success()
        payment = graph[0].payment
        for target in (PaymentCollectionStatus.OPEN, PaymentCollectionStatus.CANCELED):
            with self.assertRaises(DatabaseError):
                with transaction.atomic():
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "UPDATE financial_core_payment SET collection_status = %s WHERE id = %s",
                            [target, payment.pk],
                        )

    def test_wrong_account_state_blocks_without_partial_application(self):
        placement, account, _, _, verification = self.successful_verification()
        _, clearing, _ = self.accounting_policy(account)
        clearing.status = FinancialAccountStatus.FROZEN
        clearing.save(update_fields=("status", "updated_at"))
        with self.assertRaises(FundsApplicationBlocked):
            apply_verified_funds(
                verification_id=verification.pk,
                idempotency_key=uuid4(),
                expected_payment_version=placement.payment.version,
                correlation_id=uuid4(),
            )
        self.assertFalse(FinancialAllocation.objects.exists())
        self.assertFalse(JournalEntry.objects.exists())

    def test_receipt_journal_is_exactly_irr_and_source_linked(self):
        _, _, _, _, result = self.apply_success()
        allocation = result.allocation
        journal = allocation.journal_entry
        self.assertEqual(journal.source_id, str(allocation.public_id))
        self.assertEqual(set(journal.postings.values_list("currency", flat=True)), {CANONICAL_CURRENCY})
        self.assertEqual(JournalPosting.objects.filter(entry=journal).count(), 2)


class ProviderExecutionPhase1ConcurrencyTests(ProviderExecutionPhase1Fixture, TransactionTestCase):
    reset_sequences = True

    def test_two_concurrent_application_commands_create_one_allocation(self):
        placement, account, _, _, verification = self.successful_verification()
        self.accounting_policy(account)
        expected_version = placement.payment.version
        barrier = Barrier(2)
        outcomes = []

        def runner():
            close_old_connections()
            try:
                barrier.wait()
                result = apply_verified_funds(
                    verification_id=verification.pk,
                    idempotency_key=uuid4(),
                    expected_payment_version=expected_version,
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
        self.assertEqual(FinancialAllocation.objects.count(), 1)
        self.assertEqual(JournalEntry.objects.filter(source_type="provider_receipt").count(), 1)
        self.assertEqual(CommercialFinalizationWorkItem.objects.count(), 1)

    def test_new_attempt_remains_blocked_after_financial_application(self):
        graph, _, _, _, _ = self.apply_success()
        payment = graph[0].payment
        from cheatgame.financial_core.services.provider_requests import CollectionBlocked, create_or_replay_payment_attempt

        with self.assertRaises((CollectionBlocked, ValidationError)):
            create_or_replay_payment_attempt(
                payment_id=payment.pk,
                merchant_account_version_id=graph[1].pk,
                tender_type="external_provider",
                requested_amount=payment.amount_due,
                idempotency_key=uuid4(),
            )
