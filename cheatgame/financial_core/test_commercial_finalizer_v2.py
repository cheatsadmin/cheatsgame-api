from decimal import Decimal
from datetime import timedelta
from threading import Barrier, Thread
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from cheatgame.financial_core.models import (
    CommercialFinalization, CommercialFinalizationWorkItem, ConsiderationAllocation,
    FinancialAccount, FinancialAccountType, JournalEntry,
    FinalizationWorkStatus,
    PerformanceObligation, PerformanceObligationType, PostingDirection,
    RecognitionAllocationMethod, RecognitionPolicyVersion,
    RecognitionPrincipalAgentClassification, RecognitionProgressMethod,
    RecognitionSatisfactionPattern,
)
from cheatgame.financial_core.test_commercial_finalizer_phase1 import CommercialFinalizerFixture
from cheatgame.financial_core.services.commercial_finalization import finalize_commercial_work_item


@override_settings(REVENUE_RECOGNITION_V2_ENABLED=True)
class CommercialFinalizerV2Tests(CommercialFinalizerFixture, TransactionTestCase):
    reset_sequences = True

    def policy(
        self,
        authority,
        obligation_type,
        source_liability,
        *,
        shipping_treatment="included",
        policy_key=None,
        version=1,
    ):
        target = FinancialAccount.objects.create(
            key=f"contract-liability:{uuid4()}", name="Contract liability",
            account_type=FinancialAccountType.LIABILITY,
        )
        revenue = FinancialAccount.objects.create(
            key=f"future-revenue:{uuid4()}", name="Future revenue",
            account_type=FinancialAccountType.REVENUE,
        )
        policy = RecognitionPolicyVersion.objects.create(
            policy_key=policy_key or f"launch:{authority}:{obligation_type}", version=version,
            policy_contract_version="recognition-policy-v1", commerce_authority=authority,
            obligation_type=obligation_type,
            satisfaction_pattern=RecognitionSatisfactionPattern.POINT_IN_TIME,
            evidence_contract_version="fulfillment-satisfaction-v1",
            progress_measurement_method=RecognitionProgressMethod.NONE,
            allocation_method=RecognitionAllocationMethod.DIRECT_FROZEN_PRICE,
            principal_agent_classification=RecognitionPrincipalAgentClassification.PRINCIPAL,
            contract_liability_account=target, revenue_account=revenue,
            shipping_treatment=shipping_treatment, rounding_policy="irr_integer",
            maximum_recognition_basis="allocated_consideration",
            policy_fingerprint=uuid4().hex + uuid4().hex,
            active_for_new_obligations=True,
        )
        self.assertNotEqual(source_liability.pk, target.pk)
        return policy

    def test_standard_v2_creates_exact_deferred_graph(self):
        placement, commercial = self.ready_standard()
        policy = self.policy("standard_commerce", PerformanceObligationType.PHYSICAL_GOOD,
                             commercial.customer_unapplied_funds_account)
        result = self.finalize(placement)
        finalization = result.finalization
        self.assertEqual(finalization.recognition_accounting_contract,
                         "commercial-finalizer-v2-contract-liability")
        self.assertIsNone(finalization.accounting_policy_version_id)
        obligations = PerformanceObligation.objects.filter(finalization=finalization)
        self.assertEqual(obligations.count(), placement.order.order_items.count())
        self.assertEqual(sum(a.allocated_amount for a in ConsiderationAllocation.objects.filter(finalization=finalization)),
                         finalization.amount)
        postings = list(finalization.journal_entry.postings.order_by("line_number"))
        self.assertEqual(len(postings), 2)
        self.assertEqual(postings[0].direction, PostingDirection.DEBIT)
        self.assertEqual(postings[0].account_id, commercial.customer_unapplied_funds_account_id)
        self.assertEqual(postings[1].direction, PostingDirection.CREDIT)
        self.assertEqual(postings[1].account_id, policy.contract_liability_account_id)
        self.assertFalse(any(p.account.account_type == FinancialAccountType.REVENUE for p in postings))

    def test_digital_v2_is_one_combined_obligation_per_line(self):
        placement, _ = self.ready_digital()
        source_liability = placement.payment.financial_allocations.get().accounting_policy_version.customer_unapplied_funds_account
        self.policy("digital_products", PerformanceObligationType.DIGITAL_ACCESS_INSTALLATION, source_liability)
        finalization = self.finalize(placement).finalization
        obligation = PerformanceObligation.objects.get(finalization=finalization)
        self.assertEqual(obligation.obligation_type, PerformanceObligationType.DIGITAL_ACCESS_INSTALLATION)
        component = obligation.components.get()
        self.assertIsNotNone(component.digital_fulfillment_obligation_id)
        self.assertEqual(obligation.consideration_allocation.allocated_amount, finalization.amount)

    def test_completed_v2_replay_uses_frozen_policy_after_rotation(self):
        placement, commercial = self.ready_standard()
        policy = self.policy("standard_commerce", PerformanceObligationType.PHYSICAL_GOOD,
                             commercial.customer_unapplied_funds_account)
        key = uuid4()
        placement.payment.refresh_from_db()
        original_payment_version = placement.payment.version
        first = self.finalize(placement, key=key)
        policy.active_for_new_obligations = False
        policy.save()
        replacement = self.policy(
            "standard_commerce",
            PerformanceObligationType.PHYSICAL_GOOD,
            commercial.customer_unapplied_funds_account,
            policy_key="launch:standard_commerce:physical_good:v2",
            version=2,
        )
        replay = self.finalize(placement, key=key, expected_version=original_payment_version)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.finalization.pk, first.finalization.pk)
        self.assertNotEqual(replacement.pk, policy.pk)
        self.assertEqual(
            replay.finalization.performance_obligations.get().recognition_policy_version_id,
            policy.pk,
        )
        self.assertEqual(PerformanceObligation.objects.filter(finalization=first.finalization).count(), 1)

    def test_different_key_and_response_loss_return_authoritative_graph(self):
        placement, commercial = self.ready_standard()
        self.policy(
            "standard_commerce",
            PerformanceObligationType.PHYSICAL_GOOD,
            commercial.customer_unapplied_funds_account,
        )
        placement.payment.refresh_from_db()
        original_payment_version = placement.payment.version
        first = self.finalize(placement, key=uuid4())
        response_loss_replay = self.finalize(
            placement,
            key=first.finalization.application_idempotency_key,
            expected_version=original_payment_version,
        )
        different_key_replay = self.finalize(
            placement,
            key=uuid4(),
            expected_version=original_payment_version,
        )
        self.assertTrue(response_loss_replay.replayed)
        self.assertTrue(different_key_replay.replayed)
        self.assertEqual(
            {first.finalization.pk, response_loss_replay.finalization.pk, different_key_replay.finalization.pk},
            {first.finalization.pk},
        )
        self.assertEqual(CommercialFinalization.objects.count(), 1)
        self.assertEqual(PerformanceObligation.objects.count(), 1)
        self.assertEqual(ConsiderationAllocation.objects.count(), 1)

    def test_expired_stale_claim_is_reclaimed_without_duplicate_graph(self):
        placement, commercial = self.ready_standard()
        self.policy(
            "standard_commerce",
            PerformanceObligationType.PHYSICAL_GOOD,
            commercial.customer_unapplied_funds_account,
        )
        work = CommercialFinalizationWorkItem.objects.get(payment=placement.payment)
        stale_token = uuid4()
        claimed_at = timezone.now() - timedelta(minutes=10)
        CommercialFinalizationWorkItem.objects.filter(pk=work.pk).update(
            status=FinalizationWorkStatus.CLAIMED,
            claim_token=stale_token,
            claimed_at=claimed_at,
            claim_expires_at=claimed_at + timedelta(minutes=1),
            attempt_count=1,
            version=work.version + 1,
        )
        work.refresh_from_db()
        placement.payment.refresh_from_db()
        result = finalize_commercial_work_item(
            work_item_public_id=work.public_id,
            idempotency_key=uuid4(),
            expected_work_item_version=work.version,
            expected_payment_version=placement.payment.version,
            correlation_id=uuid4(),
        )
        work.refresh_from_db()
        self.assertEqual(work.status, FinalizationWorkStatus.COMPLETED)
        self.assertIsNone(work.claim_token)
        self.assertEqual(CommercialFinalization.objects.count(), 1)
        self.assertEqual(result.finalization.performance_obligations.count(), 1)

    def test_missing_policy_fails_before_any_commercial_effect(self):
        placement, _ = self.ready_standard()
        product = placement.order.order_items.get().product
        before = product.quantity
        with self.assertRaises(ValidationError):
            self.finalize(placement)
        product.refresh_from_db()
        placement.payment.refresh_from_db()
        self.assertEqual(product.quantity, before)
        self.assertFalse(hasattr(placement.payment, "commercial_finalization"))

    def test_allocation_failure_rolls_back_complete_v2_graph(self):
        placement, commercial = self.ready_standard()
        self.policy("standard_commerce", PerformanceObligationType.PHYSICAL_GOOD,
                    commercial.customer_unapplied_funds_account)
        product = placement.order.order_items.get().product
        before = product.quantity
        with patch.object(ConsiderationAllocation.objects, "create", side_effect=DatabaseError("fault")):
            with self.assertRaises(DatabaseError):
                self.finalize(placement)
        product.refresh_from_db()
        self.assertEqual(product.quantity, before)
        self.assertEqual(PerformanceObligation.objects.count(), 0)

    def test_distinct_shipping_policy_fails_closed_at_launch(self):
        placement, commercial = self.ready_standard()
        self.policy("standard_commerce", PerformanceObligationType.PHYSICAL_GOOD,
                    commercial.customer_unapplied_funds_account, shipping_treatment="distinct")
        with self.assertRaises(ValidationError):
            self.finalize(placement)
        self.assertFalse(CommercialFinalization.objects.exists())

    def test_postgresql_rejects_forged_policy_digest(self):
        placement, commercial = self.ready_standard()
        self.policy("standard_commerce", PerformanceObligationType.PHYSICAL_GOOD,
                    commercial.customer_unapplied_funds_account)
        original_create = CommercialFinalization.objects.create
        def forged_create(**kwargs):
            kwargs["recognition_policy_set_digest"] = "0" * 64
            return original_create(**kwargs)
        with patch.object(CommercialFinalization.objects, "create", side_effect=forged_create):
            with self.assertRaises(DatabaseError):
                self.finalize(placement)
        self.assertFalse(CommercialFinalization.objects.exists())

    def test_raw_sql_rejects_frozen_graph_mutation_and_deletion(self):
        placement, commercial = self.ready_standard()
        self.policy("standard_commerce", PerformanceObligationType.PHYSICAL_GOOD,
                    commercial.customer_unapplied_funds_account)
        finalization = self.finalize(placement).finalization
        obligation = finalization.performance_obligations.get()
        allocation = finalization.consideration_allocations.get()
        component = obligation.components.get()
        statements = (
            ("UPDATE financial_core_commercialfinalization SET recognition_policy_set_digest=%s WHERE id=%s", ["0"*64, finalization.pk]),
            ("UPDATE financial_core_commercialfinalization SET recognition_accounting_contract=%s WHERE id=%s", ["forged-contract", finalization.pk]),
            ("UPDATE financial_core_performanceobligation SET order_id=%s WHERE id=%s", [finalization.order_id + 999999, obligation.pk]),
            ("UPDATE financial_core_performanceobligation SET recognition_policy_version_id=%s WHERE id=%s", [obligation.recognition_policy_version_id + 999999, obligation.pk]),
            ("UPDATE financial_core_performanceobligationcomponent SET component_key=%s WHERE id=%s", ["forged", component.pk]),
            ("UPDATE financial_core_considerationallocation SET allocated_amount=allocated_amount+1 WHERE id=%s", [allocation.pk]),
            ("UPDATE financial_core_considerationallocation SET contract_liability_account_id=%s WHERE id=%s", [allocation.contract_liability_account_id + 999999, allocation.pk]),
            ("DELETE FROM financial_core_performanceobligation WHERE id=%s", [obligation.pk]),
            ("DELETE FROM financial_core_performanceobligationcomponent WHERE id=%s", [component.pk]),
            ("DELETE FROM financial_core_considerationallocation WHERE id=%s", [allocation.pk]),
        )
        for sql, params in statements:
            with self.assertRaises(DatabaseError), transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(sql, params)

    def test_raw_sql_rejects_hidden_or_revenue_journal_posting(self):
        placement, commercial = self.ready_standard()
        policy = self.policy("standard_commerce", PerformanceObligationType.PHYSICAL_GOOD,
                             commercial.customer_unapplied_funds_account)
        finalization = self.finalize(placement).finalization
        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO financial_core_journalposting (entry_id,line_number,account_id,direction,amount,currency,created_at) VALUES (%s,99,%s,'credit',1,'IRR',now())",
                    [finalization.journal_entry_id, policy.revenue_account_id],
                )

    def test_raw_sql_rejects_late_obligation_component_and_allocation_forgery(self):
        placement, commercial = self.ready_standard()
        self.policy(
            "standard_commerce",
            PerformanceObligationType.PHYSICAL_GOOD,
            commercial.customer_unapplied_funds_account,
        )
        finalization = self.finalize(placement).finalization
        obligation = finalization.performance_obligations.get()
        component = obligation.components.get()
        allocation = obligation.consideration_allocation

        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO financial_core_performanceobligation
                      (public_id,finalization_id,order_id,obligation_key,obligation_type,
                       commerce_authority,satisfaction_pattern,recognition_policy_version_id,
                       currency,quantity_basis,fulfillment_required,obligation_contract_version,
                       correlation_id,causation_id,created_at)
                    SELECT %s,finalization_id,order_id,%s,obligation_type,commerce_authority,
                           satisfaction_pattern,recognition_policy_version_id,currency,
                           quantity_basis,fulfillment_required,obligation_contract_version,
                           correlation_id,causation_id,now()
                      FROM financial_core_performanceobligation WHERE id=%s
                    """,
                    [str(uuid4()), "forged-obligation", obligation.pk],
                )

        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO financial_core_performanceobligationcomponent
                      (obligation_id,order_id,order_item_id,checkout_line_id,
                       standard_fulfillment_obligation_id,digital_fulfillment_obligation_id,
                       component_key,component_type,source_authority_identity,quantity,
                       commercial_snapshot_digest,sequence,component_contract_version,created_at)
                    SELECT obligation_id,order_id,order_item_id,checkout_line_id,
                           standard_fulfillment_obligation_id,digital_fulfillment_obligation_id,
                           %s,component_type,source_authority_identity,quantity,
                           commercial_snapshot_digest,sequence+100,component_contract_version,now()
                      FROM financial_core_performanceobligationcomponent WHERE id=%s
                    """,
                    ["forged-component", component.pk],
                )

        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO financial_core_performanceobligation
                      (public_id,finalization_id,order_id,obligation_key,obligation_type,
                       commerce_authority,satisfaction_pattern,recognition_policy_version_id,
                       currency,quantity_basis,fulfillment_required,obligation_contract_version,
                       correlation_id,causation_id,created_at)
                    SELECT %s,finalization_id,order_id,%s,obligation_type,commerce_authority,
                           satisfaction_pattern,recognition_policy_version_id,currency,
                           quantity_basis,fulfillment_required,obligation_contract_version,
                           correlation_id,causation_id,now()
                      FROM financial_core_performanceobligation WHERE id=%s RETURNING id
                    """,
                    [str(uuid4()), "forged-allocation-obligation", obligation.pk],
                )
                forged_obligation_id = cursor.fetchone()[0]
                cursor.execute(
                    """
                    INSERT INTO financial_core_considerationallocation
                      (public_id,finalization_id,obligation_id,payment_id,
                       recognition_policy_version_id,contract_liability_account_id,currency,
                       allocated_amount,standalone_selling_price,
                       standalone_selling_price_denominator,allocation_method,
                       discount_classification,shipping_classification,rounding_amount,
                       remainder_recipient,allocation_contract_version,allocation_fingerprint,
                       application_idempotency_key,correlation_id,causation_id,created_at)
                    SELECT %s,finalization_id,%s,payment_id,recognition_policy_version_id,%s,
                           currency,allocated_amount,standalone_selling_price,
                           standalone_selling_price_denominator,allocation_method,
                           discount_classification,shipping_classification,rounding_amount,
                           remainder_recipient,allocation_contract_version,%s,%s,
                           correlation_id,causation_id,now()
                      FROM financial_core_considerationallocation WHERE id=%s
                    """,
                    [
                        str(uuid4()),
                        forged_obligation_id,
                        commercial.customer_unapplied_funds_account_id,
                        uuid4().hex + uuid4().hex,
                        str(uuid4()),
                        allocation.pk,
                    ],
                )


@override_settings(REVENUE_RECOGNITION_V2_ENABLED=True)
class CommercialFinalizerV2ConcurrencyTests(CommercialFinalizerV2Tests):
    def test_concurrent_different_keys_converge_on_one_v2_graph(self):
        placement, commercial = self.ready_standard()
        self.policy("standard_commerce", PerformanceObligationType.PHYSICAL_GOOD,
                    commercial.customer_unapplied_funds_account)
        placement.payment.refresh_from_db()
        work = CommercialFinalizationWorkItem.objects.get(payment=placement.payment)
        barrier, outcomes = Barrier(2), []
        def runner():
            close_old_connections()
            try:
                barrier.wait()
                result = finalize_commercial_work_item(
                    work_item_public_id=work.public_id, idempotency_key=uuid4(),
                    expected_work_item_version=work.version,
                    expected_payment_version=placement.payment.version, correlation_id=uuid4(),
                )
                outcomes.append(("ok", result.finalization.pk))
            except Exception as exc:
                outcomes.append(("error", type(exc).__name__))
            finally:
                close_old_connections()
        threads = [Thread(target=runner) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(30)
        self.assertEqual([x[0] for x in outcomes].count("ok"), 2, outcomes)
        self.assertEqual(CommercialFinalization.objects.count(), 1)
        self.assertEqual(JournalEntry.objects.filter(source_type="commercial_reclassification").count(), 1)
        self.assertEqual(PerformanceObligation.objects.count(), 1)
        self.assertEqual(ConsiderationAllocation.objects.count(), 1)
