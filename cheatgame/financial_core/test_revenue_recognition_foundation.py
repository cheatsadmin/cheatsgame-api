from decimal import Decimal
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, transaction
from django.test import TransactionTestCase
from django.utils import timezone

from cheatgame.financial_core.models import (
    ConsiderationAllocation,
    FinancialAccount,
    FinancialAccountType,
    FinancialActorType,
    PerformanceObligation,
    PerformanceObligationComponent,
    PerformanceObligationComponentType,
    PerformanceObligationType,
    PostingDirection,
    RecognitionAllocationMethod,
    RecognitionPrincipalAgentClassification,
    RecognitionProgressMethod,
    RecognitionSatisfactionPattern,
    RecognitionPolicyVersion,
    RevenueRecognition,
    RevenueRecognitionEffect,
    RevenueRecognitionWorkItem,
    RevenueRecognitionWorkPurpose,
    SatisfactionEvidence,
    SatisfactionEvidenceAuthority,
    SatisfactionEvidenceClassification,
)
from cheatgame.financial_core.services.journal import post_balanced_journal_entry
from cheatgame.financial_core.services.revenue_recognition_foundation import (
    deterministic_foundation_uuid,
    foundation_fingerprint,
)
from cheatgame.financial_core.test_commercial_finalizer_phase1 import CommercialFinalizerFixture


class RevenueRecognitionFoundationTests(CommercialFinalizerFixture, TransactionTestCase):
    reset_sequences = True

    def _finalized_standard(self):
        placement, commercial_policy = self.ready_standard()
        finalization = self.finalize(placement).finalization
        return placement, commercial_policy, finalization

    def _policy(self, commercial_policy, *, key=None, active=True):
        revenue = FinancialAccount.objects.create(
            key=f"recognition-revenue:{uuid4()}",
            name="Recognition revenue",
            account_type=FinancialAccountType.REVENUE,
        )
        return RecognitionPolicyVersion.objects.create(
            policy_key=key or f"physical-point-in-time:{uuid4()}",
            version=1,
            policy_contract_version="recognition-policy-v1",
            commerce_authority="standard_commerce",
            obligation_type=PerformanceObligationType.PHYSICAL_GOOD,
            satisfaction_pattern=RecognitionSatisfactionPattern.POINT_IN_TIME,
            evidence_contract_version="physical-handoff-v1",
            progress_measurement_method=RecognitionProgressMethod.NONE,
            allocation_method=RecognitionAllocationMethod.DIRECT_FROZEN_PRICE,
            principal_agent_classification=RecognitionPrincipalAgentClassification.PRINCIPAL,
            contract_liability_account=commercial_policy.customer_unapplied_funds_account,
            revenue_account=revenue,
            shipping_treatment="separate_when_material",
            rounding_policy="irr_integer_largest_remainder",
            maximum_recognition_basis="allocated_consideration",
            policy_fingerprint=uuid4().hex + uuid4().hex,
            active_for_new_obligations=active,
        )

    def _foundation_graph(self):
        placement, commercial_policy, finalization = self._finalized_standard()
        policy = self._policy(commercial_policy)
        obligation = PerformanceObligation.objects.create(
            finalization=finalization,
            order=placement.order,
            obligation_key="physical:line:1",
            obligation_type=PerformanceObligationType.PHYSICAL_GOOD,
            commerce_authority="standard_commerce",
            satisfaction_pattern=RecognitionSatisfactionPattern.POINT_IN_TIME,
            recognition_policy_version=policy,
            quantity_basis=placement.order.order_items.get().quantity,
            fulfillment_required=True,
            obligation_contract_version="performance-obligation-v1",
            correlation_id=uuid4(),
        )
        order_item = placement.order.order_items.get()
        checkout_line = placement.order.checkout.lines.get()
        fulfillment = finalization.standard_fulfillment_obligations.get()
        component = PerformanceObligationComponent.objects.create(
            obligation=obligation,
            order=placement.order,
            order_item=order_item,
            checkout_line=checkout_line,
            standard_fulfillment_obligation=fulfillment,
            component_key="order-line:1",
            component_type=PerformanceObligationComponentType.ORDER_LINE,
            source_authority_identity=str(order_item.pk),
            quantity=order_item.quantity,
            commercial_snapshot_digest="a" * 64,
            sequence=1,
            component_contract_version="obligation-component-v1",
        )
        allocation = ConsiderationAllocation.objects.create(
            finalization=finalization,
            obligation=obligation,
            payment=placement.payment,
            recognition_policy_version=policy,
            contract_liability_account=policy.contract_liability_account,
            allocated_amount=placement.payment.amount_due,
            standalone_selling_price=placement.payment.amount_due,
            standalone_selling_price_denominator=placement.payment.amount_due,
            allocation_method=RecognitionAllocationMethod.DIRECT_FROZEN_PRICE,
            discount_classification="line_frozen",
            shipping_classification="not_allocated",
            allocation_contract_version="consideration-allocation-v1",
            allocation_fingerprint=uuid4().hex + uuid4().hex,
            application_idempotency_key=uuid4(),
            correlation_id=uuid4(),
        )
        evidence = SatisfactionEvidence.objects.create(
            obligation=obligation,
            evidence_classification=SatisfactionEvidenceClassification.POINT_IN_TIME_SATISFIED,
            source_domain="standard_fulfillment",
            source_aggregate_type="standard_fulfillment_obligation",
            source_aggregate_id=str(fulfillment.public_id),
            source_event_id=str(uuid4()),
            source_event_version=1,
            evidence_contract_version="physical-handoff-v1",
            standard_fulfillment_obligation=fulfillment,
            satisfied_quantity=order_item.quantity,
            occurred_at=timezone.now(),
            evidence_authority=SatisfactionEvidenceAuthority.SYSTEM,
            actor_type=FinancialActorType.SYSTEM,
            source_evidence_hash="b" * 64,
            request_fingerprint="c" * 64,
            idempotency_key=uuid4(),
            correlation_id=uuid4(),
        )
        work = RevenueRecognitionWorkItem.objects.create(
            obligation=obligation,
            purpose=RevenueRecognitionWorkPurpose.RECOGNIZE_SATISFACTION,
            evidence_set_digest="d" * 64,
            recognition_policy_version=policy,
            recognition_contract_version="revenue-recognition-engine-v1",
            recognition_period_key="point-in-time",
            cumulative_target_amount=allocation.allocated_amount,
            deterministic_identity="e" * 64,
            correlation_id=uuid4(),
        )
        return placement, finalization, policy, obligation, component, allocation, evidence, work

    def test_policy_version_is_frozen_and_active_scope_is_unique(self):
        _, commercial_policy, _ = self._finalized_standard()
        policy = self._policy(commercial_policy)
        policy.active_for_new_obligations = False
        policy.save()
        policy.policy_key = "rewritten"
        with self.assertRaises(ValidationError):
            policy.save()
        self._policy(commercial_policy, active=True)
        with self.assertRaises((ValidationError, DatabaseError)):
            self._policy(commercial_policy, active=True)

    def test_complete_foundation_graph_preserves_ownership_and_append_only_history(self):
        _, _, _, obligation, component, allocation, evidence, work = self._foundation_graph()
        self.assertEqual(component.order_id, obligation.order_id)
        self.assertEqual(allocation.recognition_policy_version_id, obligation.recognition_policy_version_id)
        self.assertEqual(evidence.obligation_id, obligation.pk)
        self.assertEqual(work.obligation_id, obligation.pk)
        for record in (obligation, component, allocation, evidence):
            with self.assertRaises(ValidationError):
                record.delete()
        obligation.obligation_key = "changed"
        with self.assertRaises(ValidationError):
            obligation.save()

    def test_allocation_and_source_identities_are_replay_safe_and_unique(self):
        placement, finalization, policy, obligation, _, allocation, evidence, _ = self._foundation_graph()
        with self.assertRaises((ValidationError, DatabaseError)):
            ConsiderationAllocation.objects.create(
                finalization=finalization,
                obligation=obligation,
                payment=placement.payment,
                recognition_policy_version=policy,
                contract_liability_account=policy.contract_liability_account,
                allocated_amount=allocation.allocated_amount,
                standalone_selling_price=allocation.standalone_selling_price,
                standalone_selling_price_denominator=allocation.standalone_selling_price_denominator,
                allocation_method=allocation.allocation_method,
                discount_classification="line_frozen",
                shipping_classification="not_allocated",
                allocation_contract_version="consideration-allocation-v1",
                allocation_fingerprint="f" * 64,
                application_idempotency_key=uuid4(),
                correlation_id=uuid4(),
            )
        with self.assertRaises((ValidationError, DatabaseError)):
            SatisfactionEvidence.objects.create(
                obligation=obligation,
                evidence_classification=evidence.evidence_classification,
                source_domain=evidence.source_domain,
                source_aggregate_type=evidence.source_aggregate_type,
                source_aggregate_id=evidence.source_aggregate_id,
                source_event_id=evidence.source_event_id,
                source_event_version=evidence.source_event_version,
                evidence_contract_version=evidence.evidence_contract_version,
                standard_fulfillment_obligation=evidence.standard_fulfillment_obligation,
                satisfied_quantity=evidence.satisfied_quantity,
                occurred_at=evidence.occurred_at,
                evidence_authority=evidence.evidence_authority,
                actor_type=evidence.actor_type,
                source_evidence_hash=evidence.source_evidence_hash,
                request_fingerprint="9" * 64,
                idempotency_key=uuid4(),
                correlation_id=uuid4(),
            )

    def test_direct_recognition_without_completed_engine_work_is_rejected(self):
        _, _, policy, obligation, _, allocation, _, work = self._foundation_graph()
        recognition_public_id = uuid4()
        journal = post_balanced_journal_entry(
            source_type="revenue_recognition",
            source_id=recognition_public_id,
            idempotency_key=uuid4(),
            postings=(
                {
                    "account_id": policy.contract_liability_account_id,
                    "direction": PostingDirection.DEBIT,
                    "amount": allocation.allocated_amount,
                    "currency": "IRR",
                },
                {
                    "account_id": policy.revenue_account_id,
                    "direction": PostingDirection.CREDIT,
                    "amount": allocation.allocated_amount,
                    "currency": "IRR",
                },
            ),
        )
        with self.assertRaises((ValidationError, DatabaseError)), transaction.atomic():
            RevenueRecognition.objects.create(
                public_id=recognition_public_id,
                obligation=obligation,
                consideration_allocation=allocation,
                work_item=work,
                recognition_policy_version=policy,
                journal_entry=journal,
                effect=RevenueRecognitionEffect.EARN,
                amount=allocation.allocated_amount,
                cumulative_net_recognized_amount=allocation.allocated_amount,
                evidence_set_digest=work.evidence_set_digest,
                recognition_period_key=work.recognition_period_key,
                command_contract_version="revenue-recognition-engine-v1",
                idempotency_key=uuid4(),
                application_fingerprint="1" * 64,
                actor_type=FinancialActorType.SYSTEM,
                correlation_id=uuid4(),
            )

    def test_postgresql_rejects_raw_ownership_forgery_and_mutation(self):
        _, _, _, obligation, _, allocation, _, _ = self._foundation_graph()
        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE financial_core_performanceobligation SET obligation_key = %s WHERE id = %s",
                    ["forged", obligation.pk],
                )
        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE financial_core_considerationallocation SET payment_id = %s WHERE id = %s",
                    [obligation.finalization.payment_id + 999999, allocation.pk],
                )

    def test_foundation_identity_is_deterministic_and_domain_separated(self):
        identity = {"finalization": str(uuid4()), "obligation_key": "physical:line:1"}
        first = deterministic_foundation_uuid(identity_type="performance_obligation", identity=identity)
        self.assertEqual(
            first,
            deterministic_foundation_uuid(identity_type="performance_obligation", identity=dict(identity)),
        )
        self.assertNotEqual(
            first,
            deterministic_foundation_uuid(identity_type="consideration_allocation", identity=identity),
        )
        self.assertEqual(64, len(foundation_fingerprint(identity_type="performance_obligation", identity=identity)))
        with self.assertRaises(ValueError):
            foundation_fingerprint(identity_type="", identity=identity)
