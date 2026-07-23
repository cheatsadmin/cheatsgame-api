from threading import Barrier, Thread
from unittest.mock import patch
from uuid import uuid4

from django.db import DatabaseError, close_old_connections, connection, transaction
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext

from cheatgame.financial_core.models import (
    CommercialFinalizationWorkItem,
    FinancialActorType,
    FinancialAllocation,
    FinancialOutboxMessage,
    JournalEntry,
    PaymentAttemptStatus,
    PaymentCollectionStatus,
    PaymentTransactionStatus,
)
from cheatgame.financial_core.services.funds_application import (
    FINALIZER_VERSION,
    FundsApplicationBlocked,
    recognize_verified_funds,
)
from cheatgame.financial_core.test_provider_execution_phase1 import (
    ProviderExecutionPhase1Fixture,
)


class FundsRecognitionTests(ProviderExecutionPhase1Fixture, TransactionTestCase):
    reset_sequences = True

    def _recognize(self, placement, verification, **overrides):
        values = {
            "verification_id": verification.pk,
            "idempotency_key": uuid4(),
            "expected_payment_version": placement.payment.version,
            "correlation_id": uuid4(),
        }
        values.update(overrides)
        return recognize_verified_funds(**values)

    def _ready_graph(self):
        graph = self.successful_verification()
        placement, account, _, _, _ = graph
        self.accounting_policy(account)
        placement.payment.refresh_from_db()
        return graph

    def _assert_no_partial_recognition(self, placement):
        placement.payment.refresh_from_db()
        self.assertEqual(placement.payment.confirmed_amount, 0)
        self.assertNotEqual(
            placement.payment.collection_status,
            PaymentCollectionStatus.PAID_PENDING_FINALIZATION,
        )
        self.assertFalse(FinancialAllocation.objects.exists())
        self.assertFalse(JournalEntry.objects.filter(source_type="provider_receipt").exists())
        self.assertFalse(CommercialFinalizationWorkItem.objects.exists())
        self.assertFalse(
            FinancialOutboxMessage.objects.filter(
                topic="commercial.finalization.requested"
            ).exists()
        )

    def test_canonical_command_creates_exact_recognition_graph(self):
        placement, _, attempt, transaction_obj, verification = self._ready_graph()
        result = self._recognize(placement, verification)

        placement.payment.refresh_from_db()
        attempt.refresh_from_db()
        transaction_obj.refresh_from_db()
        self.assertEqual(result.allocation.verification_id, verification.pk)
        self.assertEqual(transaction_obj.status, PaymentTransactionStatus.SUCCEEDED)
        self.assertEqual(attempt.status, PaymentAttemptStatus.SUCCEEDED)
        self.assertEqual(
            placement.payment.collection_status,
            PaymentCollectionStatus.PAID_PENDING_FINALIZATION,
        )
        self.assertEqual(
            CommercialFinalizationWorkItem.objects.filter(
                payment=placement.payment,
                finalizer_version=FINALIZER_VERSION,
            ).count(),
            1,
        )
        self.assertEqual(
            FinancialOutboxMessage.objects.filter(
                topic="commercial.finalization.requested",
                aggregate_id=str(placement.payment.public_id),
            ).count(),
            1,
        )

    def test_only_controlled_financial_actors_are_accepted(self):
        for actor_type, actor_id in (
            (FinancialActorType.CUSTOMER, None),
            (FinancialActorType.ADMIN, uuid4()),
            (FinancialActorType.SYSTEM, uuid4()),
            (FinancialActorType.RECONCILIATION, None),
        ):
            placement, _, _, _, verification = self._ready_graph()
            with self.assertRaises(FundsApplicationBlocked):
                self._recognize(
                    placement,
                    verification,
                    actor_type=actor_type,
                    actor_id=actor_id,
                )
            self._assert_no_partial_recognition(placement)

    def test_reconciliation_actor_requires_and_records_accountable_identity(self):
        placement, _, _, _, verification = self._ready_graph()
        result = self._recognize(
            placement,
            verification,
            actor_type=FinancialActorType.RECONCILIATION,
            actor_id=42,
        )
        self.assertEqual(result.allocation.verification_id, verification.pk)

    def test_missing_finalizer_work_rolls_back_the_complete_graph(self):
        placement, _, _, _, verification = self._ready_graph()
        with patch.object(
            CommercialFinalizationWorkItem.objects,
            "get_or_create",
            return_value=(object(), False),
        ):
            with self.assertRaises(DatabaseError):
                self._recognize(placement, verification)
        self._assert_no_partial_recognition(placement)

    def test_missing_transactional_outbox_rolls_back_the_complete_graph(self):
        placement, _, _, _, verification = self._ready_graph()
        with patch(
            "cheatgame.financial_core.services.funds_application.append_outbox_message",
            return_value=None,
        ):
            with self.assertRaises(DatabaseError):
                self._recognize(placement, verification)
        self._assert_no_partial_recognition(placement)

    def test_database_rejects_finalizer_work_before_recognition(self):
        placement, _, _, _, _ = self._ready_graph()
        with self.assertRaises(DatabaseError):
            with transaction.atomic():
                CommercialFinalizationWorkItem.objects.create(
                    payment=placement.payment,
                    finalizer_version=FINALIZER_VERSION,
                    deterministic_identity=f"premature:{placement.payment.public_id}",
                    correlation_id=uuid4(),
                )

    def test_database_rejects_finalizer_outbox_before_recognition(self):
        placement, _, _, _, _ = self._ready_graph()
        with self.assertRaises(DatabaseError):
            with transaction.atomic():
                FinancialOutboxMessage.objects.create(
                    topic="commercial.finalization.requested",
                    aggregate_type="financial_core.payment",
                    aggregate_id=str(placement.payment.public_id),
                    idempotency_key=f"premature:{placement.payment.public_id}",
                    correlation_id=uuid4(),
                    safe_payload={},
                )

    def test_database_rejects_second_finalizer_work_for_recognized_payment(self):
        placement, _, _, _, verification = self._ready_graph()
        self._recognize(placement, verification)
        with self.assertRaises(DatabaseError):
            with transaction.atomic():
                CommercialFinalizationWorkItem.objects.create(
                    payment=placement.payment,
                    finalizer_version="unexpected-finalizer-v2",
                    deterministic_identity=f"duplicate:{placement.payment.public_id}",
                    correlation_id=uuid4(),
                )

    def test_database_rejects_second_finalizer_outbox_for_recognized_payment(self):
        placement, _, _, _, verification = self._ready_graph()
        self._recognize(placement, verification)
        with self.assertRaises(DatabaseError):
            with transaction.atomic():
                FinancialOutboxMessage.objects.create(
                    topic="commercial.finalization.requested",
                    aggregate_type="financial_core.payment",
                    aggregate_id=str(placement.payment.public_id),
                    idempotency_key=f"duplicate:{placement.payment.public_id}",
                    correlation_id=uuid4(),
                    safe_payload={},
                )

    def test_recognition_query_budget_is_bounded(self):
        placement, _, _, _, verification = self._ready_graph()
        with CaptureQueriesContext(connection) as captured:
            self._recognize(placement, verification)
        self.assertLessEqual(len(captured), 125)

    def test_concurrent_same_key_returns_one_authoritative_allocation(self):
        placement, _, _, _, verification = self._ready_graph()
        key = uuid4()
        correlation_id = uuid4()
        expected_version = placement.payment.version
        barrier = Barrier(2)
        outcomes = []

        def runner():
            close_old_connections()
            try:
                barrier.wait()
                result = recognize_verified_funds(
                    verification_id=verification.pk,
                    idempotency_key=key,
                    expected_payment_version=expected_version,
                    correlation_id=correlation_id,
                )
                outcomes.append(("ok", result.allocation.pk, result.replayed))
            except Exception as exc:
                outcomes.append(("error", type(exc).__name__, False))
            finally:
                close_old_connections()

        threads = [Thread(target=runner) for _ in range(2)]
        for item in threads:
            item.start()
        for item in threads:
            item.join(timeout=30)

        self.assertTrue(all(not item.is_alive() for item in threads))
        self.assertEqual(len(outcomes), 2)
        self.assertEqual({item[0] for item in outcomes}, {"ok"})
        self.assertEqual(len({item[1] for item in outcomes}), 1)
        self.assertEqual(sorted(item[2] for item in outcomes), [False, True])
        self.assertEqual(FinancialAllocation.objects.count(), 1)
        self.assertEqual(JournalEntry.objects.filter(source_type="provider_receipt").count(), 1)
        self.assertEqual(CommercialFinalizationWorkItem.objects.count(), 1)
