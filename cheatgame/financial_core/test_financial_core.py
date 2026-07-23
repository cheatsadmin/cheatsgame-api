from datetime import timedelta
from decimal import Decimal
from threading import Barrier, Thread
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError, close_old_connections, connection, transaction
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from cheatgame.financial_core.models import (
    FinancialAccount,
    FinancialAccountType,
    FinancialEvent,
    IdempotencyRecord,
    JournalEntry,
    JournalPosting,
    Payment,
    PaymentAttempt,
    PaymentAttemptStatus,
    PaymentCollectionStatus,
    PaymentTenderType,
    PaymentTransaction,
    PaymentTransactionOperation,
    PaymentTransactionStatus,
    PostingDirection,
    ReconciliationFinding,
    ReconciliationRunStatus,
    ReviewAction,
    ReviewCaseReason,
    ReviewCaseSeverity,
    ReviewCaseStatus,
)
from cheatgame.financial_core.services.boundaries import ExternalIOInsideTransaction, assert_external_io_allowed
from cheatgame.financial_core.services.idempotency import (
    IdempotencyConflict,
    IdempotencyInProgress,
    begin_idempotent_command,
    canonical_request_hash,
    complete_idempotent_command,
)
from cheatgame.financial_core.services.journal import UnbalancedJournalEntry, post_balanced_journal_entry
from cheatgame.financial_core.services.locks import (
    LockOrderViolation,
    LockRank,
    lock_many,
    ordered_lock_scope,
    register_lock,
)
from cheatgame.financial_core.services.payments import (
    create_payment_attempt,
    create_payment_for_order,
    create_payment_transaction,
    transition_payment,
    transition_payment_attempt,
    transition_payment_transaction,
)
from cheatgame.financial_core.services.reconciliation import (
    create_reconciliation_run,
    record_reconciliation_finding,
    transition_reconciliation_run,
)
from cheatgame.financial_core.services.reviews import open_review_case, transition_review_case
from cheatgame.financial_core.services.state_machines import (
    InvalidFinancialTransition,
    assert_payment_transition,
    assert_payment_transaction_transition,
)
from cheatgame.shop.models import Order, OrderStatus, OrderUserStatus
from cheatgame.users.models import BaseUser


class FinancialCoreFixture:
    def make_user(self, phone="09121110000"):
        return BaseUser.objects.create_user(
            phone_number=phone,
            firstname="Financial",
            lastname="Core",
            password="StrongPass123!",
        )

    def make_order(self, user=None, amount=100000):
        return Order.objects.create(
            user=user or self.make_user(),
            payment_status=OrderStatus.PENDDING,
            user_status=OrderUserStatus.NOTCOMPLETED,
            total_price=Decimal(amount),
            total_price_discount=Decimal(amount),
        )

    def make_payment(self, order=None, amount=100000):
        return create_payment_for_order(
            order_id=(order or self.make_order()).pk,
            amount_due=Decimal(amount),
            currency="IRR",
            command_key=f"test:payment:{uuid4()}",
        )

    def make_attempt(self, payment=None, key=None):
        payment = payment or self.make_payment()
        key = key or uuid4()
        return create_payment_attempt(
            payment_id=payment.pk,
            requested_amount=payment.amount_due,
            currency=payment.currency,
            tender_type=PaymentTenderType.EXTERNAL_PROVIDER,
            provider="provider-neutral-test",
            merchant_account_ref="merchant-test-v1",
            idempotency_key=key,
            request_hash=canonical_request_hash({"payment": str(payment.public_id), "provider": "test"}),
            command_key=f"test:attempt:{key}",
        )

    def make_transaction(self, attempt=None, key=None):
        attempt = attempt or self.make_attempt()
        key = key or uuid4()
        return create_payment_transaction(
            attempt_id=attempt.pk,
            operation_type=PaymentTransactionOperation.SALE,
            provider=attempt.provider,
            merchant_account_ref=attempt.merchant_account_ref,
            merchant_reference=f"merchant-reference:{key}",
            amount=attempt.requested_amount,
            currency=attempt.currency,
            provider_amount=attempt.requested_amount,
            provider_unit="IRR",
            idempotency_key=key,
            command_key=f"test:transaction:{key}",
        )


class FinancialCoreModelAndServiceTests(FinancialCoreFixture, TestCase):
    def test_order_owns_one_immutable_payment_obligation(self):
        order = self.make_order()
        payment = self.make_payment(order=order)
        replay = create_payment_for_order(
            order_id=order.pk,
            amount_due=payment.amount_due,
            currency=payment.currency,
            command_key=f"ignored-replay:{uuid4()}",
        )
        self.assertEqual(replay.pk, payment.pk)
        self.assertEqual(order.financial_payment.pk, payment.pk)
        self.assertEqual(payment.currency, "IRR")
        payment.amount_due += 1
        with self.assertRaises(ValidationError):
            payment.save()

    def test_attempt_and_provider_transaction_have_distinct_immutable_identity(self):
        attempt = self.make_attempt()
        transaction_obj = self.make_transaction(attempt=attempt)
        self.assertEqual(transaction_obj.attempt_id, attempt.pk)
        self.assertEqual(transaction_obj.operation_type, PaymentTransactionOperation.SALE)
        self.assertEqual(transaction_obj.provider_unit, "IRR")
        transaction_obj.merchant_reference = "changed"
        with self.assertRaises(ValidationError):
            transaction_obj.save()

    def test_c1_rejects_legacy_irt_without_automatic_conversion(self):
        order = self.make_order()
        with self.assertRaisesMessage(ValidationError, "legacy IRT compatibility bridge"):
            create_payment_for_order(
                order_id=order.pk,
                amount_due=Decimal("1000"),
                currency="IRT",
                command_key=f"test:irt-payment:{uuid4()}",
            )
        with self.assertRaisesMessage(ValidationError, "legacy IRT compatibility bridge"):
            post_balanced_journal_entry(
                source_type="legacy-irt",
                source_id=str(uuid4()),
                idempotency_key=uuid4(),
                postings=[
                    {"account_id": 1, "direction": PostingDirection.DEBIT, "amount": 10, "currency": "IRT"},
                    {"account_id": 2, "direction": PostingDirection.CREDIT, "amount": 10, "currency": "IRT"},
                ],
            )

    def test_c1_cannot_write_confirmed_funds(self):
        payment = self.make_payment()
        with self.assertRaisesMessage(ValidationError, "verified journaled finalizer"):
            transition_payment(
                payment_id=payment.pk,
                target_status=PaymentCollectionStatus.PROCESSING,
                confirmed_amount=payment.amount_due,
                command_key=f"test:confirmed-blocked:{uuid4()}",
            )
        payment.refresh_from_db()
        self.assertEqual(payment.confirmed_amount, 0)

    def test_transaction_idempotency_payload_mismatch_is_rejected(self):
        attempt = self.make_attempt()
        key = uuid4()
        transaction_obj = self.make_transaction(attempt=attempt, key=key)
        with self.assertRaisesMessage(ValidationError, "idempotency key conflicts"):
            create_payment_transaction(
                attempt_id=attempt.pk,
                operation_type=transaction_obj.operation_type,
                provider=transaction_obj.provider,
                merchant_account_ref=transaction_obj.merchant_account_ref,
                merchant_reference=transaction_obj.merchant_reference,
                amount=transaction_obj.amount - 1,
                currency=transaction_obj.currency,
                provider_amount=transaction_obj.provider_amount,
                provider_unit=transaction_obj.provider_unit,
                idempotency_key=key,
                command_key=f"test:transaction-conflict:{uuid4()}",
            )

    def test_failed_attempt_is_not_reopened_and_retry_creates_new_attempt(self):
        payment = self.make_payment()
        first = self.make_attempt(payment=payment)
        transition_payment_attempt(
            attempt_id=first.pk,
            target_status=PaymentAttemptStatus.DEFINITIVE_FAILED,
            command_key=f"test:attempt-failed:{uuid4()}",
        )
        with self.assertRaises(InvalidFinancialTransition):
            transition_payment_attempt(
                attempt_id=first.pk,
                target_status=PaymentAttemptStatus.PROCESSING,
                command_key=f"test:attempt-reopen:{uuid4()}",
            )
        second = self.make_attempt(payment=payment)
        self.assertEqual(second.sequence, 2)
        self.assertNotEqual(first.pk, second.pk)

    def test_live_or_unknown_attempt_blocks_customer_retry(self):
        payment = self.make_payment()
        first = self.make_attempt(payment=payment)
        transition_payment_attempt(
            attempt_id=first.pk,
            target_status=PaymentAttemptStatus.OUTCOME_UNKNOWN,
            command_key=f"test:attempt-unknown:{uuid4()}",
        )
        with self.assertRaisesMessage(ValidationError, "live, successful, unknown, or review"):
            self.make_attempt(payment=payment)

    def test_paid_state_machine_exists_but_c1_service_cannot_declare_paid(self):
        payment = self.make_payment()
        transition_payment(
            payment_id=payment.pk,
            target_status=PaymentCollectionStatus.PROCESSING,
            command_key=f"test:payment-processing:{uuid4()}",
        )
        assert_payment_transition(
            PaymentCollectionStatus.PROCESSING,
            PaymentCollectionStatus.PAID_PENDING_FINALIZATION,
        )
        assert_payment_transition(
            PaymentCollectionStatus.PAID_PENDING_FINALIZATION,
            PaymentCollectionStatus.PAID,
        )
        with self.assertRaisesMessage(ValidationError, "verified journaled finalizer"):
            transition_payment(
                payment_id=payment.pk,
                target_status=PaymentCollectionStatus.PAID_PENDING_FINALIZATION,
                confirmed_amount=payment.amount_due,
                command_key=f"test:payment-confirmed:{uuid4()}",
            )
        payment.refresh_from_db()
        self.assertEqual(payment.collection_status, PaymentCollectionStatus.PROCESSING)

    def test_transaction_state_machine_preserves_unknown_and_c1_cannot_declare_success(self):
        transaction_obj = self.make_transaction()
        transition_payment_transaction(
            transaction_id=transaction_obj.pk,
            target_status=PaymentTransactionStatus.REQUESTING,
            command_key=f"test:tx-requesting:{uuid4()}",
        )
        transition_payment_transaction(
            transaction_id=transaction_obj.pk,
            target_status=PaymentTransactionStatus.OUTCOME_UNKNOWN,
            command_key=f"test:tx-unknown:{uuid4()}",
        )
        assert_payment_transaction_transition(
            PaymentTransactionStatus.OUTCOME_UNKNOWN,
            PaymentTransactionStatus.SUCCEEDED,
        )
        with self.assertRaisesMessage(ValidationError, "provider verification"):
            transition_payment_transaction(
                transaction_id=transaction_obj.pk,
                target_status=PaymentTransactionStatus.SUCCEEDED,
                command_key=f"test:tx-succeeded:{uuid4()}",
            )
        reviewed = transition_payment_transaction(
            transaction_id=transaction_obj.pk,
            target_status=PaymentTransactionStatus.REVIEW,
            command_key=f"test:tx-review:{uuid4()}",
        )
        self.assertEqual(reviewed.status, PaymentTransactionStatus.REVIEW)

    def test_financial_events_are_safe_and_append_only(self):
        payment = self.make_payment()
        event = FinancialEvent.objects.get(aggregate_id=str(payment.public_id))
        self.assertNotIn("secret", event.metadata)
        with self.assertRaises(ValidationError):
            FinancialEvent.objects.filter(pk=event.pk).update(event_type="rewritten")
        with self.assertRaises(ValidationError):
            event.delete()

    def test_balanced_journal_is_idempotent_and_append_only(self):
        cash = FinancialAccount.objects.create(
            key="provider-clearing:irr", name="Provider clearing", account_type=FinancialAccountType.ASSET
        )
        receivable = FinancialAccount.objects.create(
            key="customer-receivable:irr", name="Customer receivable", account_type=FinancialAccountType.ASSET
        )
        key = uuid4()
        postings = [
            {"account_id": cash.pk, "direction": PostingDirection.DEBIT, "amount": 1000, "currency": "IRR"},
            {
                "account_id": receivable.pk,
                "direction": PostingDirection.CREDIT,
                "amount": 1000,
                "currency": "IRR",
            },
        ]
        entry = post_balanced_journal_entry(
            source_type="payment_transaction", source_id="test-1", idempotency_key=key, postings=postings
        )
        replay = post_balanced_journal_entry(
            source_type="payment_transaction", source_id="test-1", idempotency_key=key, postings=postings
        )
        self.assertEqual(entry.pk, replay.pk)
        self.assertEqual(entry.postings.count(), 2)
        with self.assertRaises(ValidationError):
            JournalPosting.objects.filter(entry=entry).update(amount=999)
        with self.assertRaises(UnbalancedJournalEntry):
            post_balanced_journal_entry(
                source_type="payment_transaction",
                source_id="test-2",
                idempotency_key=uuid4(),
                postings=[
                    {"account_id": cash.pk, "direction": PostingDirection.DEBIT, "amount": 1, "currency": "IRR"},
                    {
                        "account_id": receivable.pk,
                        "direction": PostingDirection.CREDIT,
                        "amount": 2,
                        "currency": "IRR",
                    },
                ],
            )

    def test_review_case_is_first_class_and_consistent(self):
        transaction_obj = self.make_transaction()
        key = uuid4()
        review = open_review_case(
            reason=ReviewCaseReason.PROVIDER_STATE_UNCLEAR,
            severity=ReviewCaseSeverity.CRITICAL,
            summary="Provider outcome requires reconciliation.",
            idempotency_key=key,
            command_key=f"test:review:{key}",
            transaction_id=transaction_obj.pk,
        )
        replay = open_review_case(
            reason=ReviewCaseReason.PROVIDER_STATE_UNCLEAR,
            severity=ReviewCaseSeverity.CRITICAL,
            summary="Provider outcome requires reconciliation.",
            idempotency_key=key,
            command_key=f"ignored:{uuid4()}",
            transaction_id=transaction_obj.pk,
        )
        self.assertEqual(review.pk, replay.pk)
        self.assertEqual(review.payment_id, transaction_obj.attempt.payment_id)
        self.assertEqual(review.order_id, transaction_obj.attempt.payment.order_id)

    def test_review_case_transitions_create_append_only_actions(self):
        transaction_obj = self.make_transaction()
        actor = self.make_user("09121110001")
        review = open_review_case(
            reason=ReviewCaseReason.PROVIDER_STATE_UNCLEAR,
            severity=ReviewCaseSeverity.HIGH,
            summary="Controlled review transition.",
            idempotency_key=uuid4(),
            command_key=f"test:review-open:{uuid4()}",
            transaction_id=transaction_obj.pk,
        )
        action_key = uuid4()
        transitioned = transition_review_case(
            review_case_id=review.pk,
            target_status=ReviewCaseStatus.INVESTIGATING,
            actor_id=actor.pk,
            reason_code="investigation_started",
            idempotency_key=action_key,
            command_key=f"test:review-investigating:{uuid4()}",
        )
        replay = transition_review_case(
            review_case_id=review.pk,
            target_status=ReviewCaseStatus.INVESTIGATING,
            actor_id=actor.pk,
            reason_code="investigation_started",
            idempotency_key=action_key,
            command_key=f"test:review-investigating-replay:{uuid4()}",
        )
        self.assertEqual(transitioned.pk, replay.pk)
        self.assertEqual(transitioned.status, ReviewCaseStatus.INVESTIGATING)
        self.assertEqual(ReviewAction.objects.filter(review_case=review).count(), 1)

    def test_idempotency_hash_conflict_and_replay(self):
        key = uuid4()
        record, created = begin_idempotent_command(
            scope="financial:test", key=key, request_payload={"amount": Decimal("1000"), "currency": "IRR"}
        )
        self.assertTrue(created)
        with self.assertRaises(IdempotencyInProgress):
            begin_idempotent_command(
                scope="financial:test", key=key, request_payload={"amount": Decimal("1000"), "currency": "IRR"}
            )
        complete_idempotent_command(record_id=record.pk, result_type="Payment", result_id="1")
        replay, created = begin_idempotent_command(
            scope="financial:test", key=key, request_payload={"amount": Decimal("1000"), "currency": "IRR"}
        )
        self.assertFalse(created)
        self.assertEqual(replay.result_id, "1")
        with self.assertRaises(IdempotencyConflict):
            begin_idempotent_command(
                scope="financial:test", key=key, request_payload={"amount": Decimal("1001"), "currency": "IRR"}
            )

    def test_lock_order_enforcement_rejects_descending_rank_and_key(self):
        with ordered_lock_scope():
            register_lock(LockRank.PAYABLE, "0002")
            register_lock(LockRank.PAYMENT, "0001")
            with self.assertRaises(LockOrderViolation):
                register_lock(LockRank.CHECKOUT, "0001")
        with ordered_lock_scope():
            register_lock(LockRank.PAYMENT_TRANSACTION, "0002")
            with self.assertRaises(LockOrderViolation):
                register_lock(LockRank.PAYMENT_TRANSACTION, "0001")

    def test_lock_many_sorts_collection_primary_keys(self):
        first = self.make_payment()
        second = self.make_payment(order=self.make_order(user=self.make_user("09121110002")))
        with transaction.atomic(), ordered_lock_scope():
            rows = lock_many(
                queryset=Payment.objects.all(),
                rank=LockRank.PAYMENT,
                pks=[second.pk, first.pk, second.pk],
            )
        self.assertEqual([row.pk for row in rows], sorted([first.pk, second.pk]))

    def test_reconciliation_foundation_deduplicates_findings(self):
        now = timezone.now()
        run = create_reconciliation_run(
            run_type="local_financial",
            period_start=now - timedelta(days=1),
            period_end=now,
            idempotency_key=uuid4(),
        )
        transition_reconciliation_run(run_id=run.pk, target_status=ReconciliationRunStatus.RUNNING)
        first = record_reconciliation_finding(
            run_id=run.pk,
            finding_key="payment:missing-journal:1",
            finding_type="payment_missing_journal",
            severity=ReviewCaseSeverity.HIGH,
            expected={"journal_entries": 1},
            actual={"journal_entries": 0},
        )
        replay = record_reconciliation_finding(
            run_id=run.pk,
            finding_key="payment:missing-journal:1",
            finding_type="payment_missing_journal",
            severity=ReviewCaseSeverity.HIGH,
        )
        self.assertEqual(first.pk, replay.pk)
        self.assertEqual(ReconciliationFinding.objects.filter(run=run).count(), 1)

    def test_dormancy_has_no_routes_signals_or_legacy_mutation(self):
        from config.urls import urlpatterns

        route_text = " ".join(str(item.pattern) for item in urlpatterns)
        self.assertNotIn("financial", route_text.lower())
        self.assertEqual(Payment.objects.count(), 0)
        self.assertEqual(PaymentAttempt.objects.count(), 0)
        self.assertEqual(PaymentTransaction.objects.count(), 0)
        self.assertEqual(IdempotencyRecord.objects.count(), 0)


class FinancialCorePostgreSQLConcurrencyTests(FinancialCoreFixture, TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL-specific concurrency validation")

    def test_concurrent_attempt_creation_allows_only_one_live_attempt(self):
        payment = self.make_payment()
        barrier = Barrier(2)
        outcomes = []

        def worker(index):
            close_old_connections()
            try:
                barrier.wait()
                attempt = create_payment_attempt(
                    payment_id=payment.pk,
                    requested_amount=payment.amount_due,
                    currency=payment.currency,
                    tender_type=PaymentTenderType.EXTERNAL_PROVIDER,
                    provider="provider-neutral-test",
                    merchant_account_ref="merchant-test-v1",
                    idempotency_key=uuid4(),
                    request_hash=canonical_request_hash({"worker": index}),
                    command_key=f"test:concurrent-attempt:{index}:{uuid4()}",
                )
                outcomes.append(("created", attempt.pk))
            except ValidationError:
                outcomes.append(("blocked", index))
            finally:
                close_old_connections()

        threads = [Thread(target=worker, args=(index,)) for index in range(2)]
        for item in threads:
            item.start()
        for item in threads:
            item.join(timeout=10)
        self.assertEqual(len(outcomes), 2)
        self.assertEqual(sum(1 for result, _ in outcomes if result == "created"), 1)
        self.assertEqual(sum(1 for result, _ in outcomes if result == "blocked"), 1)
        self.assertEqual(PaymentAttempt.objects.filter(payment=payment).count(), 1)

    def test_attempt_creation_and_review_creation_share_canonical_locks(self):
        payment = self.make_payment()
        barrier = Barrier(2)
        outcomes = []

        def create_attempt():
            close_old_connections()
            try:
                barrier.wait(timeout=5)
                self.make_attempt(payment=Payment.objects.get(pk=payment.pk), key=uuid4())
            except Exception as exc:
                outcomes.append(f"attempt:{type(exc).__name__}:{exc}")
            else:
                outcomes.append("attempt:created")
            finally:
                close_old_connections()

        def create_review():
            close_old_connections()
            try:
                barrier.wait(timeout=5)
                open_review_case(
                    reason=ReviewCaseReason.PROVIDER_STATE_UNCLEAR,
                    severity=ReviewCaseSeverity.HIGH,
                    summary="Concurrent review.",
                    idempotency_key=uuid4(),
                    command_key=f"test:concurrent-review:{uuid4()}",
                    payment_id=payment.pk,
                )
            except Exception as exc:
                outcomes.append(f"review:{type(exc).__name__}:{exc}")
            else:
                outcomes.append("review:created")
            finally:
                close_old_connections()

        threads = [Thread(target=create_attempt), Thread(target=create_review)]
        for item in threads:
            item.start()
        for item in threads:
            item.join(timeout=10)
        self.assertFalse(any(item.is_alive() for item in threads), outcomes)
        self.assertCountEqual(outcomes, ["attempt:created", "review:created"])

    def test_journal_posting_and_reconciliation_finding_do_not_reverse_locks(self):
        payment = self.make_payment()
        cash = FinancialAccount.objects.create(
            key="concurrent-clearing:irr",
            name="Concurrent clearing",
            account_type=FinancialAccountType.ASSET,
        )
        receivable = FinancialAccount.objects.create(
            key="concurrent-receivable:irr",
            name="Concurrent receivable",
            account_type=FinancialAccountType.ASSET,
        )
        now = timezone.now()
        run = create_reconciliation_run(
            run_type="concurrent-local",
            period_start=now - timedelta(days=1),
            period_end=now,
            idempotency_key=uuid4(),
        )
        barrier = Barrier(2)
        outcomes = []

        def post_journal():
            close_old_connections()
            try:
                barrier.wait(timeout=5)
                post_balanced_journal_entry(
                    source_type="concurrency-test",
                    source_id=str(payment.public_id),
                    idempotency_key=uuid4(),
                    postings=[
                        {"account_id": cash.pk, "direction": PostingDirection.DEBIT, "amount": 1, "currency": "IRR"},
                        {"account_id": receivable.pk, "direction": PostingDirection.CREDIT, "amount": 1, "currency": "IRR"},
                    ],
                )
            except Exception as exc:
                outcomes.append(f"journal:{type(exc).__name__}:{exc}")
            else:
                outcomes.append("journal:created")
            finally:
                close_old_connections()

        def record_finding():
            close_old_connections()
            try:
                barrier.wait(timeout=5)
                record_reconciliation_finding(
                    run_id=run.pk,
                    finding_key="concurrent-payment-check",
                    finding_type="concurrency_test",
                    severity=ReviewCaseSeverity.LOW,
                    payment_id=payment.pk,
                )
            except Exception as exc:
                outcomes.append(f"reconciliation:{type(exc).__name__}:{exc}")
            else:
                outcomes.append("reconciliation:created")
            finally:
                close_old_connections()

        threads = [Thread(target=post_journal), Thread(target=record_finding)]
        for item in threads:
            item.start()
        for item in threads:
            item.join(timeout=10)
        self.assertFalse(any(item.is_alive() for item in threads), outcomes)
        self.assertCountEqual(outcomes, ["journal:created", "reconciliation:created"])

    def test_external_io_guard_allows_outside_and_rejects_atomic_context(self):
        assert_external_io_allowed()
        with transaction.atomic(), self.assertRaises(ExternalIOInsideTransaction):
            assert_external_io_allowed()

    def test_database_append_only_trigger_blocks_raw_update(self):
        payment = self.make_payment()
        event = FinancialEvent.objects.get(aggregate_id=str(payment.public_id))
        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE financial_core_financialevent SET event_type = %s WHERE id = %s",
                ["tampered", event.pk],
            )
        event.refresh_from_db()
        self.assertEqual(event.event_type, "payment.opened")

    def test_database_rejects_irt_and_unsupported_confirmed_funds(self):
        payment = self.make_payment()
        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE financial_core_payment SET currency = %s WHERE id = %s",
                ["IRT", payment.pk],
            )
        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE financial_core_payment SET confirmed_amount = amount_due WHERE id = %s",
                [payment.pk],
            )

    def test_database_protects_terminal_attempt_and_transaction_identity(self):
        attempt = self.make_attempt()
        transaction_obj = self.make_transaction(attempt=attempt)
        transition_payment_attempt(
            attempt_id=attempt.pk,
            target_status=PaymentAttemptStatus.DEFINITIVE_FAILED,
            command_key=f"test:db-terminal-attempt:{uuid4()}",
        )
        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE financial_core_paymentattempt SET status = %s WHERE id = %s",
                [PaymentAttemptStatus.PROCESSING, attempt.pk],
            )
        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE financial_core_paymenttransaction SET merchant_reference = %s WHERE id = %s",
                ["tampered", transaction_obj.pk],
            )
        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM financial_core_paymenttransaction WHERE id = %s",
                [transaction_obj.pk],
            )
        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE financial_core_payment SET amount_due = amount_due + 1 WHERE id = %s",
                [attempt.payment_id],
            )
        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM financial_core_paymentattempt WHERE id = %s",
                [attempt.pk],
            )

    def test_database_review_resolution_requires_append_only_action(self):
        review = open_review_case(
            reason=ReviewCaseReason.INVARIANT_VIOLATION,
            severity=ReviewCaseSeverity.HIGH,
            summary="Database resolution guard.",
            idempotency_key=uuid4(),
            command_key=f"test:db-review-open:{uuid4()}",
            payment_id=self.make_payment().pk,
        )
        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE financial_core_reviewcase
                   SET status = %s, resolution_code = %s, resolved_at = CURRENT_TIMESTAMP
                 WHERE id = %s
                """,
                [ReviewCaseStatus.RESOLVED, "unsafe_direct_resolution", review.pk],
            )

    def test_database_deferred_guard_rejects_unbalanced_journal(self):
        account = FinancialAccount.objects.create(
            key="raw-test:irr", name="Raw test", account_type=FinancialAccountType.ASSET
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                entry = JournalEntry.objects.create(
                    source_type="raw-test", source_id=str(uuid4()), idempotency_key=uuid4()
                )
                JournalPosting.objects.create(
                    entry=entry,
                    line_number=1,
                    account=account,
                    direction=PostingDirection.DEBIT,
                    amount=1,
                    currency="IRR",
                )
                with connection.cursor() as cursor:
                    cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
