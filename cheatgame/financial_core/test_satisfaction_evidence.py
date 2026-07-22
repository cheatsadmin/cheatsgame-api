from uuid import uuid4

from django.core.exceptions import PermissionDenied
from django.db import DatabaseError, connection, transaction
from django.test import TransactionTestCase, override_settings

from cheatgame.digital_products.models import DigitalFulfillmentStatus
from cheatgame.digital_products.services.fulfillment import (
    assign_fulfillment_operator,
    provision_digital_fulfillment_obligation,
    record_console_received,
    record_customer_contact,
    record_purchased_game_installation,
    staff_verify_fulfillment_completion,
    start_fulfillment_work,
)
from cheatgame.financial_core.models import (
    DigitalFulfillmentObligation,
    FinancialAccount,
    FinancialAccountType,
    PerformanceObligationType,
    RecognitionAllocationMethod,
    RecognitionPolicyVersion,
    RecognitionPrincipalAgentClassification,
    RecognitionProgressMethod,
    RecognitionSatisfactionPattern,
    SatisfactionEvidence,
)
from cheatgame.financial_core.services.satisfaction_evidence import (
    DIGITAL_FULFILLMENT_COMPLETED,
    STANDARD_DELIVERY_COMPLETED,
    SatisfactionEvidenceConflict,
    complete_standard_fulfillment,
    normalize_digital_fulfillment_completion,
)
from cheatgame.financial_core.test_commercial_finalizer_phase1 import CommercialFinalizerFixture
from cheatgame.users.models import BaseUser, UserTypes


@override_settings(REVENUE_RECOGNITION_V2_ENABLED=True)
class SatisfactionEvidenceLaunchTests(CommercialFinalizerFixture, TransactionTestCase):
    reset_sequences = True

    def policy(self, authority, obligation_type, source_liability):
        target = FinancialAccount.objects.create(
            key=f"contract-liability:{uuid4()}", name="Contract liability",
            account_type=FinancialAccountType.LIABILITY,
        )
        revenue = FinancialAccount.objects.create(
            key=f"future-revenue:{uuid4()}", name="Future revenue",
            account_type=FinancialAccountType.REVENUE,
        )
        policy = RecognitionPolicyVersion.objects.create(
            policy_key=f"launch:{authority}:{obligation_type}", version=1,
            policy_contract_version="recognition-policy-v1", commerce_authority=authority,
            obligation_type=obligation_type,
            satisfaction_pattern=RecognitionSatisfactionPattern.POINT_IN_TIME,
            evidence_contract_version="fulfillment-satisfaction-v1",
            progress_measurement_method=RecognitionProgressMethod.NONE,
            allocation_method=RecognitionAllocationMethod.DIRECT_FROZEN_PRICE,
            principal_agent_classification=RecognitionPrincipalAgentClassification.PRINCIPAL,
            contract_liability_account=target, revenue_account=revenue,
            shipping_treatment="included", rounding_policy="irr_integer",
            maximum_recognition_basis="allocated_consideration",
            policy_fingerprint=uuid4().hex + uuid4().hex,
            active_for_new_obligations=True,
        )
        self.assertNotEqual(source_liability.pk, target.pk)
        return policy

    def operator(self, suffix="1", user_type=UserTypes.MANAGER):
        return BaseUser.objects.create_user(
            phone_number=f"0912888800{suffix}", firstname="Launch", lastname="Operator",
            user_type=user_type,
        )

    def standard_graph(self):
        placement, commercial = self.ready_standard()
        if not self._active_standard_policy_exists():
            self.policy(
                "standard_commerce", PerformanceObligationType.PHYSICAL_GOOD,
                commercial.customer_unapplied_funds_account,
            )
        finalization = self.finalize(placement).finalization
        return placement, finalization, finalization.standard_fulfillment_obligations.get()

    @staticmethod
    def _active_standard_policy_exists():
        from cheatgame.financial_core.models import RecognitionPolicyVersion
        return RecognitionPolicyVersion.objects.filter(
            commerce_authority="standard_commerce",
            obligation_type=PerformanceObligationType.PHYSICAL_GOOD,
            active_for_new_obligations=True,
        ).exists()

    def digital_graph(self):
        placement, _ = self.ready_digital()
        source_liability = (
            placement.payment.financial_allocations.get()
            .accounting_policy_version.customer_unapplied_funds_account
        )
        self.policy(
            "digital_products", PerformanceObligationType.DIGITAL_ACCESS_INSTALLATION,
            source_liability,
        )
        self.finalize(placement)
        obligation = DigitalFulfillmentObligation.objects.get(order=placement.order)
        item = provision_digital_fulfillment_obligation(
            obligation_public_id=obligation.public_id, idempotency_key=uuid4()
        )
        operator = self.operator("2")
        assign_fulfillment_operator(
            fulfillment_id=item.public_id, operator=operator, actor=operator,
            idempotency_key=uuid4(),
        )
        record_customer_contact(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
        record_console_received(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
        start_fulfillment_work(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
        record_purchased_game_installation(
            fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4()
        )
        staff_verify_fulfillment_completion(
            fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4()
        )
        item.refresh_from_db()
        self.assertEqual(item.status, DigitalFulfillmentStatus.COMPLETED)
        return placement, obligation, item, operator

    def test_standard_operator_completion_is_immutable_and_replay_safe(self):
        placement, _, fulfillment = self.standard_graph()
        operator = self.operator()
        key = uuid4()
        first = complete_standard_fulfillment(
            fulfillment_obligation_public_id=fulfillment.public_id, operator=operator,
            idempotency_key=key, correlation_id=uuid4(),
        )
        same_key = complete_standard_fulfillment(
            fulfillment_obligation_public_id=fulfillment.public_id, operator=operator,
            idempotency_key=key, correlation_id=uuid4(),
        )
        different_key = complete_standard_fulfillment(
            fulfillment_obligation_public_id=fulfillment.public_id, operator=operator,
            idempotency_key=uuid4(), correlation_id=uuid4(),
        )
        evidence = first.evidence
        self.assertFalse(first.replayed)
        self.assertTrue(same_key.replayed)
        self.assertTrue(different_key.replayed)
        self.assertEqual({evidence.pk}, {same_key.evidence.pk, different_key.evidence.pk})
        self.assertEqual(evidence.evidence_contract_version, STANDARD_DELIVERY_COMPLETED)
        self.assertEqual(evidence.obligation.order_id, placement.order.pk)
        self.assertEqual(evidence.actor_id, operator.pk)
        self.assertEqual(SatisfactionEvidence.objects.count(), 1)

    def test_standard_completion_rejects_customer_and_same_key_other_obligation(self):
        placement, _, fulfillment = self.standard_graph()
        operator = self.operator()
        key = uuid4()
        complete_standard_fulfillment(
            fulfillment_obligation_public_id=fulfillment.public_id, operator=operator,
            idempotency_key=key, correlation_id=uuid4(),
        )
        with self.assertRaises(PermissionDenied):
            complete_standard_fulfillment(
                fulfillment_obligation_public_id=fulfillment.public_id, operator=placement.order.user,
                idempotency_key=uuid4(), correlation_id=uuid4(),
            )
        other_operator = self.operator("3")
        with self.assertRaises(SatisfactionEvidenceConflict):
            complete_standard_fulfillment(
                fulfillment_obligation_public_id=fulfillment.public_id, operator=other_operator,
                idempotency_key=key, correlation_id=uuid4(),
            )

    def test_digital_completion_normalizes_existing_authoritative_graph(self):
        placement, obligation, item, operator = self.digital_graph()
        key = uuid4()
        first = normalize_digital_fulfillment_completion(
            fulfillment_item_public_id=item.public_id, idempotency_key=key,
            correlation_id=uuid4(),
        )
        replay = normalize_digital_fulfillment_completion(
            fulfillment_item_public_id=item.public_id, idempotency_key=uuid4(),
            correlation_id=uuid4(),
        )
        evidence = first.evidence
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(evidence.pk, replay.evidence.pk)
        self.assertEqual(evidence.evidence_contract_version, DIGITAL_FULFILLMENT_COMPLETED)
        self.assertEqual(evidence.digital_fulfillment_obligation_id, obligation.pk)
        self.assertEqual(evidence.obligation.order_id, placement.order.pk)
        self.assertEqual(evidence.actor_id, operator.pk)

    def test_postgresql_rejects_wrong_obligation_and_duplicate_launch_evidence(self):
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL guard proof")
        _, _, fulfillment = self.standard_graph()
        operator = self.operator()
        evidence = complete_standard_fulfillment(
            fulfillment_obligation_public_id=fulfillment.public_id, operator=operator,
            idempotency_key=uuid4(), correlation_id=uuid4(),
        ).evidence
        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO financial_core_satisfactionevidence
                      (public_id,obligation_id,evidence_classification,source_domain,
                       source_aggregate_type,source_aggregate_id,source_event_id,
                       source_event_version,evidence_contract_version,
                       standard_fulfillment_obligation_id,digital_fulfillment_obligation_id,
                       satisfied_quantity,progress_numerator,progress_denominator,occurred_at,
                       evidence_authority,actor_type,actor_id,source_evidence_hash,
                       request_fingerprint,idempotency_key,correlation_id,causation_id,
                       corrects_id,contradicts_id,created_at)
                    SELECT %s,%s,evidence_classification,source_domain,source_aggregate_type,
                           source_aggregate_id,source_event_id,source_event_version,
                           evidence_contract_version,standard_fulfillment_obligation_id,
                           digital_fulfillment_obligation_id,satisfied_quantity,
                           progress_numerator,progress_denominator,occurred_at,evidence_authority,
                           actor_type,actor_id,source_evidence_hash,%s,%s,%s,causation_id,
                           corrects_id,contradicts_id,now()
                      FROM financial_core_satisfactionevidence WHERE id=%s
                    """,
                    [str(uuid4()), evidence.obligation_id + 999999, "d" * 64, str(uuid4()), str(uuid4()), evidence.pk],
                )
        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO financial_core_satisfactionevidence
                      (public_id,obligation_id,evidence_classification,source_domain,
                       source_aggregate_type,source_aggregate_id,source_event_id,
                       source_event_version,evidence_contract_version,
                       standard_fulfillment_obligation_id,digital_fulfillment_obligation_id,
                       satisfied_quantity,progress_numerator,progress_denominator,occurred_at,
                       evidence_authority,actor_type,actor_id,source_evidence_hash,
                       request_fingerprint,idempotency_key,correlation_id,causation_id,
                       corrects_id,contradicts_id,created_at)
                    SELECT %s,obligation_id,evidence_classification,source_domain,
                           source_aggregate_type,source_aggregate_id,source_event_id,
                           source_event_version,evidence_contract_version,
                           standard_fulfillment_obligation_id,digital_fulfillment_obligation_id,
                           satisfied_quantity,progress_numerator,progress_denominator,occurred_at,
                           evidence_authority,actor_type,actor_id,source_evidence_hash,%s,%s,%s,
                           causation_id,corrects_id,contradicts_id,now()
                      FROM financial_core_satisfactionevidence WHERE id=%s
                    """,
                    [str(uuid4()), "e" * 64, str(uuid4()), str(uuid4()), evidence.pk],
                )

    def test_postgresql_rejects_launch_evidence_update_and_delete(self):
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL guard proof")
        _, _, fulfillment = self.standard_graph()
        evidence = complete_standard_fulfillment(
            fulfillment_obligation_public_id=fulfillment.public_id, operator=self.operator(),
            idempotency_key=uuid4(), correlation_id=uuid4(),
        ).evidence
        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE financial_core_satisfactionevidence SET actor_id=actor_id+1 WHERE id=%s",
                    [evidence.pk],
                )
        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM financial_core_satisfactionevidence WHERE id=%s", [evidence.pk])
