from datetime import timedelta
from threading import Barrier, Thread
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from cheatgame.financial_core.models import (
    FinancialAccount,
    FinancialAccountStatus,
    FinancialAccountType,
    PerformanceObligationType,
    PostingDirection,
    RecognitionAllocationMethod,
    RecognitionPolicyVersion,
    RecognitionPrincipalAgentClassification,
    RecognitionProgressMethod,
    RecognitionSatisfactionPattern,
    RevenueRecognition,
    RevenueRecognitionWorkItem,
    RevenueRecognitionWorkPurpose,
    RevenueRecognitionWorkStatus,
    SatisfactionEvidence,
    SatisfactionEvidenceAuthority,
    SatisfactionEvidenceClassification,
)
from cheatgame.financial_core.services.revenue_recognition import (
    REVENUE_RECOGNITION_CONTRACT,
    RevenueRecognitionConflict,
    RevenueRecognitionError,
    _load_launch_graph,
    _validate_launch_graph,
    claim_revenue_recognition_work,
    create_or_replay_recognition_work,
    recognize_revenue,
)
from cheatgame.financial_core.services.satisfaction_evidence import complete_standard_fulfillment
from cheatgame.financial_core.test_commercial_finalizer_phase1 import CommercialFinalizerFixture
from cheatgame.users.models import BaseUser, UserTypes


@override_settings(REVENUE_RECOGNITION_V2_ENABLED=True)
class RevenueRecognitionEngineTests(CommercialFinalizerFixture, TransactionTestCase):
    reset_sequences = True

    def operator(self, suffix="1"):
        return BaseUser.objects.create_user(
            phone_number=f"0912777700{suffix}", firstname="Revenue", lastname="Operator",
            user_type=UserTypes.MANAGER,
        )

    def policy(self, source_liability, *, key=None):
        liability = FinancialAccount.objects.create(
            key=f"recognition-liability:{uuid4()}", name="Contract liability",
            account_type=FinancialAccountType.LIABILITY,
        )
        revenue = FinancialAccount.objects.create(
            key=f"recognition-revenue:{uuid4()}", name="Earned revenue",
            account_type=FinancialAccountType.REVENUE,
        )
        policy = RecognitionPolicyVersion.objects.create(
            policy_key=key or f"launch:standard:{uuid4()}", version=1,
            policy_contract_version="recognition-policy-v1",
            commerce_authority="standard_commerce",
            obligation_type=PerformanceObligationType.PHYSICAL_GOOD,
            satisfaction_pattern=RecognitionSatisfactionPattern.POINT_IN_TIME,
            evidence_contract_version="fulfillment-satisfaction-v1",
            progress_measurement_method=RecognitionProgressMethod.NONE,
            allocation_method=RecognitionAllocationMethod.DIRECT_FROZEN_PRICE,
            principal_agent_classification=RecognitionPrincipalAgentClassification.PRINCIPAL,
            contract_liability_account=liability,
            revenue_account=revenue,
            shipping_treatment="included",
            rounding_policy="irr_integer",
            maximum_recognition_basis="allocated_consideration",
            policy_fingerprint=uuid4().hex + uuid4().hex,
            active_for_new_obligations=True,
        )
        self.assertNotEqual(source_liability.pk, liability.pk)
        return policy

    def ready_graph(self):
        placement, commercial = self.ready_standard()
        policy = self.policy(commercial.customer_unapplied_funds_account)
        finalization = self.finalize(placement).finalization
        fulfillment = finalization.standard_fulfillment_obligations.get()
        evidence = complete_standard_fulfillment(
            fulfillment_obligation_public_id=fulfillment.public_id,
            operator=self.operator(), idempotency_key=uuid4(), correlation_id=uuid4(),
        ).evidence
        work_result = create_or_replay_recognition_work(
            evidence_public_id=evidence.public_id,
            idempotency_key=uuid4(), correlation_id=uuid4(),
        )
        return placement, finalization, policy, evidence, work_result.work_item

    def claimed_graph(self):
        placement, finalization, policy, evidence, work = self.ready_graph()
        claim = claim_revenue_recognition_work(
            work_item_public_id=work.public_id,
            claim_idempotency_key=uuid4(), expected_work_version=work.version,
        )
        return placement, finalization, policy, evidence, claim.work_item, claim.claim_token

    def recognize(self, work, token, *, key=None):
        return recognize_revenue(
            work_item_public_id=work.public_id, claim_token=token,
            idempotency_key=key or uuid4(), correlation_id=uuid4(),
        )

    def test_normal_recognition_posts_exact_liability_to_revenue_journal(self):
        placement, _, policy, evidence, work, token = self.claimed_graph()
        result = self.recognize(work, token)
        recognition = result.recognition
        work.refresh_from_db()
        postings = list(recognition.journal_entry.postings.order_by("line_number"))
        self.assertFalse(result.replayed)
        self.assertEqual(recognition.amount, placement.payment.amount_due)
        self.assertEqual(recognition.evidence_set_digest, evidence.source_evidence_hash)
        self.assertEqual(recognition.command_contract_version, REVENUE_RECOGNITION_CONTRACT)
        self.assertEqual(work.status, RevenueRecognitionWorkStatus.COMPLETED)
        self.assertEqual(len(postings), 2)
        self.assertEqual(
            (postings[0].account_id, postings[0].direction, postings[0].amount),
            (policy.contract_liability_account_id, PostingDirection.DEBIT, recognition.amount),
        )
        self.assertEqual(
            (postings[1].account_id, postings[1].direction, postings[1].amount),
            (policy.revenue_account_id, PostingDirection.CREDIT, recognition.amount),
        )

    def test_work_and_completed_recognition_replay_without_duplicates(self):
        _, _, _, evidence, work, token = self.claimed_graph()
        key = uuid4()
        first = self.recognize(work, token, key=key)
        replay = self.recognize(work, token, key=key)
        different_key = self.recognize(work, token, key=uuid4())
        repeated_work = create_or_replay_recognition_work(
            evidence_public_id=evidence.public_id,
            idempotency_key=uuid4(), correlation_id=uuid4(),
        )
        completed_claim = claim_revenue_recognition_work(
            work_item_public_id=work.public_id, claim_idempotency_key=uuid4(),
            expected_work_version=work.version,
        )
        self.assertTrue(replay.replayed)
        self.assertTrue(different_key.replayed)
        self.assertTrue(repeated_work.replayed)
        self.assertTrue(completed_claim.replayed)
        self.assertEqual({first.recognition.pk}, {
            replay.recognition.pk, different_key.recognition.pk, completed_claim.recognition.pk,
        })
        self.assertEqual(RevenueRecognition.objects.count(), 1)
        self.assertEqual(first.recognition.journal_entry.postings.count(), 2)

    def test_wrong_or_contradictory_evidence_and_wrong_accounts_fail_closed(self):
        _, _, policy, evidence, work = self.ready_graph()
        obligation = evidence.obligation
        SatisfactionEvidence.objects.create(
            obligation=obligation,
            evidence_classification=SatisfactionEvidenceClassification.CONTRADICTORY,
            source_domain="test", source_aggregate_type="test",
            source_aggregate_id=str(uuid4()), source_event_id=str(uuid4()),
            source_event_version=1, evidence_contract_version="test-contradiction",
            satisfied_quantity=0, occurred_at=timezone.now(),
            evidence_authority=SatisfactionEvidenceAuthority.SYSTEM,
            actor_type="system", source_evidence_hash="a" * 64,
            request_fingerprint="b" * 64, idempotency_key=uuid4(), correlation_id=uuid4(),
        )
        with self.assertRaises(RevenueRecognitionError):
            claim = claim_revenue_recognition_work(
                work_item_public_id=work.public_id, claim_idempotency_key=uuid4(),
                expected_work_version=work.version,
            )
            self.recognize(claim.work_item, claim.claim_token)

    def test_wrong_allocation_wrong_policy_and_wrong_accounts_fail_closed(self):
        _, _, policy, evidence, _ = self.ready_graph()
        evidence_row, obligation, allocation, frozen_policy = _load_launch_graph(evidence.public_id)
        allocation.obligation_id += 999999
        with self.assertRaises(RevenueRecognitionError):
            _validate_launch_graph(
                evidence=evidence_row, obligation=obligation,
                allocation=allocation, policy=frozen_policy,
            )
        _, obligation, allocation, frozen_policy = _load_launch_graph(evidence.public_id)
        frozen_policy.pk += 999999
        with self.assertRaises(RevenueRecognitionError):
            _validate_launch_graph(
                evidence=evidence_row, obligation=obligation,
                allocation=allocation, policy=frozen_policy,
            )
        policy.revenue_account.status = FinancialAccountStatus.CLOSED
        policy.revenue_account.save(update_fields=("status", "updated_at"))
        with self.assertRaises(RevenueRecognitionError):
            create_or_replay_recognition_work(
                evidence_public_id=evidence.public_id,
                idempotency_key=uuid4(), correlation_id=uuid4(),
            )

    def test_stale_claim_is_rejected_and_expired_lease_is_reclaimable(self):
        _, _, _, _, work, token = self.claimed_graph()
        stale_token = token
        work.claim_expires_at = timezone.now() - timedelta(seconds=1)
        work.save(update_fields=("claim_expires_at", "updated_at"))
        work.refresh_from_db()
        reclaim = claim_revenue_recognition_work(
            work_item_public_id=work.public_id, claim_idempotency_key=uuid4(),
            expected_work_version=work.version,
        )
        with self.assertRaises(RevenueRecognitionError):
            self.recognize(reclaim.work_item, stale_token)
        result = self.recognize(reclaim.work_item, reclaim.claim_token)
        self.assertFalse(result.replayed)
        self.assertEqual(result.recognition.work_item_id, work.pk)

    def test_postgresql_rejects_mutation_deletion_extra_posting_and_over_recognition(self):
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL guard proof")
        _, _, policy, _, work, token = self.claimed_graph()
        recognition = self.recognize(work, token).recognition
        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE financial_core_revenuerecognition SET amount=amount+1 WHERE id=%s",
                    [recognition.pk],
                )
        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM financial_core_revenuerecognition WHERE id=%s", [recognition.pk])
        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO financial_core_journalposting
                      (entry_id,line_number,account_id,direction,amount,currency,memo,created_at)
                    VALUES (%s,3,%s,'credit',%s,'IRR','forged',now())
                    """,
                    [recognition.journal_entry_id, policy.revenue_account_id, recognition.amount],
                )
        with self.assertRaises((DatabaseError, ValidationError)), transaction.atomic():
            RevenueRecognition.objects.create(
                public_id=uuid4(), obligation=recognition.obligation,
                consideration_allocation=recognition.consideration_allocation,
                work_item=RevenueRecognitionWorkItem.objects.create(
                    obligation=recognition.obligation,
                    purpose=RevenueRecognitionWorkPurpose.RECOGNIZE_SATISFACTION,
                    evidence_set_digest=recognition.evidence_set_digest,
                    recognition_policy_version=policy,
                    recognition_contract_version=REVENUE_RECOGNITION_CONTRACT,
                    recognition_period_key="point-in-time",
                    cumulative_target_amount=recognition.amount + 1,
                    deterministic_identity=uuid4().hex + uuid4().hex,
                    correlation_id=uuid4(),
                ),
                recognition_policy_version=policy,
                journal_entry=recognition.journal_entry,
                effect="earn", amount=recognition.amount + 1, currency="IRR",
                cumulative_net_recognized_amount=recognition.amount + 1,
                evidence_set_digest=recognition.evidence_set_digest,
                recognition_period_key="point-in-time",
                command_contract_version=REVENUE_RECOGNITION_CONTRACT,
                idempotency_key=uuid4(), application_fingerprint=uuid4().hex + uuid4().hex,
                actor_type="system", correlation_id=uuid4(),
            )

    def test_concurrent_recognition_converges_on_one_result_and_journal(self):
        _, _, _, _, work, token = self.claimed_graph()
        barrier = Barrier(2)
        outcomes = []

        def runner(key):
            close_old_connections()
            try:
                barrier.wait()
                result = recognize_revenue(
                    work_item_public_id=work.public_id, claim_token=token,
                    idempotency_key=key, correlation_id=uuid4(),
                )
                outcomes.append(("ok", result.recognition.pk, result.replayed))
            except Exception as exc:
                outcomes.append(("error", type(exc).__name__, str(exc)))
            finally:
                close_old_connections()

        threads = [Thread(target=runner, args=(uuid4(),)) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual([item[0] for item in outcomes].count("ok"), 2, outcomes)
        self.assertEqual(len({item[1] for item in outcomes}), 1, outcomes)
        recognition = RevenueRecognition.objects.get()
        self.assertEqual(recognition.journal_entry.postings.count(), 2)
        self.assertEqual(RevenueRecognition.objects.count(), 1)
