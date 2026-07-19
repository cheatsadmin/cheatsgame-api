import hashlib
import hmac
from datetime import timedelta
from decimal import Decimal
from threading import Event, Thread
from unittest.mock import patch
from uuid import uuid4

from django.conf import settings
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from cheatgame.financial_core.models import (
    CallbackAuthenticationStrength,
    CommercialFinalization,
    FinancialAllocation,
    JournalEntry,
    MerchantAccountVersion,
    PaymentTransactionOperation,
    ProviderCapabilityVersion,
    ProviderDefinition,
    ProviderReferenceAllocation,
    Verification,
    VerificationApplicationState,
    VerificationClaim,
    VerificationEvidenceBasis,
    VerificationFinality,
    VerificationFinancialEffect,
    VerificationOutcome,
    VerificationTransportClassification,
    VerificationTriggerSource,
    VerificationWorkItem,
    VerificationWorkStatus,
    VerificationWorkType,
)
from cheatgame.financial_core.services.adapters import (
    ADAPTER_CONTRACT_VERSION,
    NormalizedVerificationResult,
    ProviderAdapterRegistry,
)
from cheatgame.financial_core.services.verification import enqueue_verification_work
from cheatgame.financial_core.services.verification import (
    VerificationClaimConflict,
    claim_verification_work,
)
from cheatgame.financial_core.services.verification_worker import (
    VerificationInterpretationState,
    derive_current_verification_interpretation,
    execute_verification_work_item,
)
from cheatgame.financial_core.test_c2b1 import C2B1Fixture, SyntheticC2B1Adapter


class WorkerAdapter(SyntheticC2B1Adapter):
    def __init__(self):
        self.verify_calls = []
        self.query_calls = []
        self.atomic_states = []
        self.overrides = {}
        self.mode = "result"

    def _result(self, envelope, *, callback=False):
        self.atomic_states.append(connection.in_atomic_block)
        if self.mode == "timeout":
            raise TimeoutError("provider detail must not escape")
        if self.mode == "malformed":
            return {"unsafe": "provider response"}
        values = {
            "outcome": VerificationOutcome.CONFIRMED_SUCCESS,
            "financial_effect": VerificationFinancialEffect.PAID,
            "finality": VerificationFinality.FINAL,
            "transport_classification": VerificationTransportClassification.SUCCESS,
            "provider_key": envelope.provider_key,
            "adapter_contract_version": envelope.adapter_contract_version,
            "merchant_account_key": envelope.merchant_account_key,
            "merchant_account_version": envelope.merchant_account_version,
            "merchant_reference": (
                envelope.callback_merchant_reference if callback else envelope.merchant_reference
            ),
            "provider_authority": (
                envelope.callback_provider_authority if callback else "worker-authority"
            ),
            "provider_reference": (
                envelope.callback_provider_reference if callback else "worker-provider-reference"
            ),
            "operation_type": (
                envelope.callback_operation_type if callback else envelope.operation_type
            ),
            "observed_provider_amount": Decimal(
                envelope.callback_provider_amount if callback else envelope.requested_provider_amount
            ),
            "observed_provider_unit": (
                envelope.callback_provider_unit if callback else envelope.requested_provider_unit
            ),
            "evidence_hash": hashlib.sha256(
                f"{envelope.transaction_public_id}:{envelope.claim_token}".encode("utf-8")
            ).hexdigest(),
            "evidence_basis": (
                VerificationEvidenceBasis.AUTHENTICATED_SETTLEMENT
                if callback
                else VerificationEvidenceBasis.SERVER_TO_SERVER
            ),
            "provider_occurred_at": (
                parse_datetime(envelope.callback_provider_occurred_at)
                if callback
                else timezone.now()
            ),
        }
        values.update(self.overrides)
        return NormalizedVerificationResult(**values)

    def verify_operation(self, envelope):
        self.verify_calls.append(envelope)
        return self._result(envelope, callback=True)

    def query_operation(self, envelope):
        self.query_calls.append(envelope)
        return self._result(envelope, callback=False)


class FinancialTruthVerificationWorkerTests(C2B1Fixture, TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        super().setUp()
        self.callback_final = True
        self.placement, self.account, self.attempt, self.transaction_obj = self.make_pending_graph()
        self.adapter = WorkerAdapter()
        self.registry = ProviderAdapterRegistry(
            {("synthetic", ADAPTER_CONTRACT_VERSION): self.adapter}
        )

    def callback(self, transaction_obj, account, **kwargs):
        kwargs.setdefault("correlate_by_transaction", True)
        return super().callback(transaction_obj, account, **kwargs)

    def make_account(self, *, unit="IRR", enabled=True, version=1):
        provider = ProviderDefinition.objects.create(
            key=f"verification-{uuid4()}",
            display_name="Verification Provider",
            is_enabled=enabled,
            new_requests_enabled=enabled,
        )
        capability = ProviderCapabilityVersion.objects.create(
            provider=provider,
            version=version,
            adapter_key="synthetic",
            adapter_contract_version=ADAPTER_CONTRACT_VERSION,
            provider_unit=unit,
            conversion_policy_version=f"{unit.lower()}-exact-v1",
            supported_operations=[PaymentTransactionOperation.SALE],
            supports_request_idempotency=True,
            supports_lookup=getattr(self, "supports_lookup", True),
            callback_authentication=CallbackAuthenticationStrength.SHARED_SECRET,
            callback_authentication_method="hmac-sha256",
            callback_authentication_version=getattr(self, "callback_authentication_version", "test-v1"),
            callback_verification_is_final=self.callback_final,
            finality_window_seconds=3600,
            authority_expiry_seconds=900,
            not_found_is_final_unpaid=getattr(self, "not_found_is_final_unpaid", False),
        )
        account = MerchantAccountVersion.objects.create(
            provider=provider,
            capability_version=capability,
            account_key="platform-primary",
            version=version,
            owner_key="cheats-game",
            credential_reference="env://VERIFICATION_PROVIDER_CREDENTIAL",
            callback_signing_key_reference_hash=hmac.new(
                settings.SECRET_KEY.encode("utf-8"),
                getattr(self, "callback_signing_key_reference", "test-key-v1").encode("utf-8"),
                hashlib.sha256,
            ).hexdigest(),
            is_enabled=enabled,
            new_requests_enabled=enabled,
        )
        return provider, capability, account

    def callback_work(self, **callback_kwargs):
        callback = self.callback(self.transaction_obj, self.account, **callback_kwargs)
        if callback.verification_work_id is None:
            self.fail(
                "Callback did not create verification work: "
                f"{callback.receipt.processing_status}/"
                f"{callback.receipt.quarantine_reason}/"
                f"{callback.receipt.safe_reason_code}"
            )
        return VerificationWorkItem.objects.get(pk=callback.verification_work_id)

    def separate_callback_work(self, *, callback_final=True):
        self.callback_final = callback_final
        placement, account, attempt, transaction_obj = self.make_pending_graph()
        callback = self.callback(transaction_obj, account)
        return (
            VerificationWorkItem.objects.get(pk=callback.verification_work_id),
            placement,
            account,
            attempt,
            transaction_obj,
        )

    def run_work(self, work, *, root_key=None, trigger=VerificationTriggerSource.CALLBACK):
        return execute_verification_work_item(
            work_item_id=work.pk,
            trigger_source=trigger,
            execution_idempotency_key=root_key or uuid4(),
            adapter_registry=self.registry,
            retry_after_seconds=1,
        )

    def aggregate_snapshot(self):
        payment = self.placement.payment
        payment.refresh_from_db()
        self.attempt.refresh_from_db()
        self.transaction_obj.refresh_from_db()
        return {
            "payment": (
                payment.collection_status,
                payment.confirmed_amount,
                payment.version,
            ),
            "attempt": (self.attempt.status, self.attempt.version),
            "transaction": (
                self.transaction_obj.status,
                self.transaction_obj.version,
                self.transaction_obj.provider_authority,
                self.transaction_obj.provider_reference,
            ),
        }

    def enqueue_followup(self, transaction_obj, *, suffix):
        return enqueue_verification_work(
            transaction_obj=transaction_obj,
            work_type=VerificationWorkType.VERIFY_UNKNOWN_OUTCOME,
            deterministic_identity=f"api06:{suffix}:{transaction_obj.public_id}",
            correlation_id=transaction_obj.correlation_id,
        )[0]

    def raw_clone_verification(self, source, **overrides):
        fields = [
            field for field in Verification._meta.concrete_fields
            if field.column != Verification._meta.pk.column
        ]
        quote = connection.ops.quote_name
        columns = [field.column for field in fields]
        expressions = []
        params = []
        for column in columns:
            if column in overrides:
                expressions.append("%s")
                params.append(overrides[column])
            else:
                expressions.append(f"source.{quote(column)}")
        params.append(source.pk)
        sql = (
            f"INSERT INTO {quote(Verification._meta.db_table)} "
            f"({', '.join(quote(column) for column in columns)}) "
            f"SELECT {', '.join(expressions)} FROM {quote(Verification._meta.db_table)} source "
            f"WHERE source.{quote(Verification._meta.pk.column)} = %s"
        )
        with connection.cursor() as cursor:
            cursor.execute(sql, params)

    def raw_insert_reference_allocation(
        self, *, verification_id, transaction_id, account_id, provider_reference
    ):
        table = connection.ops.quote_name(ProviderReferenceAllocation._meta.db_table)
        with connection.cursor() as cursor:
            cursor.execute(
                f"INSERT INTO {table} "
                "(merchant_account_version_id, transaction_id, verification_id, "
                "provider_reference, allocation_fingerprint, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    account_id,
                    transaction_id,
                    verification_id,
                    provider_reference,
                    hashlib.sha256(str(uuid4()).encode("utf-8")).hexdigest(),
                    timezone.now(),
                ),
            )

    def test_callback_sufficient_path_records_truth_without_query_or_payment_mutation(self):
        work = self.callback_work()
        before = self.aggregate_snapshot()
        result = self.run_work(work)
        self.assertEqual(result.verification.normalized_outcome, VerificationOutcome.CONFIRMED_SUCCESS)
        self.assertFalse(result.used_provider_query)
        self.assertEqual(len(self.adapter.verify_calls), 1)
        self.assertEqual(self.adapter.query_calls, [])
        self.assertEqual(self.adapter.atomic_states, [False])
        self.assertEqual(self.aggregate_snapshot(), before)
        self.assertEqual(FinancialAllocation.objects.count(), 0)
        self.assertEqual(JournalEntry.objects.count(), 0)
        self.assertEqual(CommercialFinalization.objects.count(), 0)
        self.assertFalse(
            VerificationWorkItem.objects.filter(work_type="apply_verified_funds").exists()
        )

    def test_provider_query_is_required_without_explicit_callback_final_policy(self):
        work, _, _, _, _ = self.separate_callback_work(callback_final=False)
        result = self.run_work(work)
        self.assertTrue(result.used_provider_query)
        self.assertEqual(len(self.adapter.query_calls), 1)
        self.assertEqual(self.adapter.verify_calls, [])

    def test_amount_and_currency_mismatch_are_review_observations(self):
        work, self.placement, self.account, self.attempt, self.transaction_obj = (
            self.separate_callback_work(callback_final=False)
        )
        self.adapter.overrides = {
            "observed_provider_amount": Decimal(self.transaction_obj.provider_amount) + 1
        }
        amount = self.run_work(work).verification
        self.assertEqual(amount.normalized_outcome, VerificationOutcome.MISMATCH)
        self.assertEqual(amount.error_classification, "provider_amount_mismatch")

        work, _, _, _, transaction_obj = self.separate_callback_work(callback_final=False)
        self.adapter.overrides = {
            "observed_provider_unit": "IRT" if transaction_obj.provider_unit == "IRR" else "IRR"
        }
        currency = self.run_work(work).verification
        self.assertEqual(currency.normalized_outcome, VerificationOutcome.MISMATCH)
        self.assertEqual(currency.error_classification, "provider_unit_mismatch")

    def test_provider_and_merchant_reference_mismatch_fail_closed(self):
        work, _, _, _, _ = self.separate_callback_work(callback_final=False)
        self.adapter.overrides = {"merchant_reference": "wrong-merchant-reference"}
        merchant = self.run_work(work).verification
        self.assertEqual(merchant.normalized_outcome, VerificationOutcome.MISMATCH)
        self.assertEqual(merchant.error_classification, "merchant_reference_mismatch")

        work, _, _, _, transaction_obj = self.separate_callback_work(callback_final=False)
        transaction_obj.provider_reference = "expected-provider-reference"
        transaction_obj.save(update_fields=("provider_reference", "updated_at"))
        self.adapter.overrides = {"provider_reference": "wrong-provider-reference"}
        provider = self.run_work(work).verification
        self.assertEqual(provider.normalized_outcome, VerificationOutcome.MISMATCH)
        self.assertEqual(provider.error_classification, "provider_reference_mismatch")

    def test_provider_identity_and_nonfinal_success_fail_closed(self):
        work, _, _, _, _ = self.separate_callback_work(callback_final=False)
        self.adapter.overrides = {"provider_key": "wrong-provider"}
        identity = self.run_work(work).verification
        self.assertEqual(identity.normalized_outcome, VerificationOutcome.MISMATCH)
        self.assertEqual(identity.error_classification, "provider_identity_mismatch")

        work, _, _, _, _ = self.separate_callback_work(callback_final=False)
        self.adapter.overrides = {"finality": VerificationFinality.NON_FINAL}
        finality = self.run_work(work).verification
        self.assertEqual(finality.normalized_outcome, VerificationOutcome.MISMATCH)
        self.assertEqual(finality.error_classification, "success_not_final_paid")

    def test_pending_unknown_timeout_and_malformed_results_remain_nonfinancial(self):
        cases = (
            (
                {"outcome": VerificationOutcome.PENDING, "financial_effect": VerificationFinancialEffect.NONE,
                 "finality": VerificationFinality.NON_FINAL, "retryable": True},
                "result",
                VerificationOutcome.PENDING,
            ),
            (
                {"outcome": VerificationOutcome.OUTCOME_UNKNOWN, "financial_effect": VerificationFinancialEffect.UNKNOWN,
                 "finality": VerificationFinality.UNKNOWN, "retryable": True},
                "result",
                VerificationOutcome.OUTCOME_UNKNOWN,
            ),
            ({}, "timeout", VerificationOutcome.OUTCOME_UNKNOWN),
            ({}, "malformed", VerificationOutcome.PROTOCOL_FAILURE),
        )
        work, self.placement, self.account, self.attempt, self.transaction_obj = (
            self.separate_callback_work(callback_final=False)
        )
        before = self.aggregate_snapshot()
        for index, (overrides, mode, expected) in enumerate(cases):
            if index:
                work.refresh_from_db()
                work.next_attempt_at = timezone.now() - timedelta(seconds=1)
                work.save(update_fields=("next_attempt_at", "updated_at"))
            self.adapter.overrides = overrides
            self.adapter.mode = mode
            observation = self.run_work(work).verification
            self.assertEqual(observation.normalized_outcome, expected)
            self.assertEqual(self.aggregate_snapshot(), before)
        self.assertEqual(Verification.objects.filter(transaction=self.transaction_obj).count(), 4)

    def test_contradictory_callback_creates_review_truth_without_overwrite(self):
        original = self.callback(self.transaction_obj, self.account, event_id="contradiction-event")
        changed = self.callback(
            self.transaction_obj,
            self.account,
            event_id="contradiction-event",
            body_overrides={"amount": int(self.transaction_obj.provider_amount) + 5},
        )
        self.assertNotEqual(original.provider_event.pk, changed.provider_event.pk)
        work = VerificationWorkItem.objects.get(pk=changed.verification_work_id)
        observation = self.run_work(work).verification
        self.assertEqual(observation.normalized_outcome, VerificationOutcome.CONTRADICTORY_EVIDENCE)
        self.assertEqual(observation.application_state, VerificationApplicationState.REVIEW_REQUIRED)
        self.assertEqual(original.provider_event.contradictory_events.count(), 1)

    def test_current_interpretation_is_policy_derived_not_last_write_wins(self):
        first = self.run_work(self.callback_work()).verification
        followup, _ = enqueue_verification_work(
            transaction_obj=self.transaction_obj,
            work_type=VerificationWorkType.VERIFY_UNKNOWN_OUTCOME,
            deterministic_identity=f"api06-contradiction:{self.transaction_obj.public_id}",
            correlation_id=self.transaction_obj.correlation_id,
        )
        self.adapter.overrides = {
            "outcome": VerificationOutcome.CONFIRMED_DECLINE,
            "financial_effect": VerificationFinancialEffect.UNPAID,
            "finality": VerificationFinality.FINAL,
        }
        second = self.run_work(
            followup,
            trigger=VerificationTriggerSource.UNKNOWN_OUTCOME,
        ).verification
        with CaptureQueriesContext(connection) as interpretation_queries:
            interpretation = derive_current_verification_interpretation(
                transaction_id=self.transaction_obj.pk
            )
        self.assertEqual(first.normalized_outcome, VerificationOutcome.CONFIRMED_SUCCESS)
        self.assertEqual(second.normalized_outcome, VerificationOutcome.CONTRADICTORY_EVIDENCE)
        self.assertEqual(interpretation.state, VerificationInterpretationState.BLOCKED_REVIEW)
        self.assertEqual(interpretation.controlling_verification.pk, second.pk)
        self.assertLessEqual(len(interpretation_queries), 4)

    def test_duplicate_callback_and_worker_replay_are_idempotent(self):
        first = self.callback(self.transaction_obj, self.account, event_id="duplicate-event")
        second = self.callback(self.transaction_obj, self.account, event_id="duplicate-event")
        self.assertIsNone(second.verification_work_id)
        self.assertEqual(VerificationWorkItem.objects.count(), 1)
        root = uuid4()
        initial = self.run_work(
            VerificationWorkItem.objects.get(pk=first.verification_work_id), root_key=root
        )
        replay = self.run_work(
            VerificationWorkItem.objects.get(pk=first.verification_work_id), root_key=root
        )
        self.assertFalse(initial.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(initial.verification.pk, replay.verification.pk)
        self.assertEqual(Verification.objects.count(), 1)
        self.assertEqual(VerificationClaim.objects.count(), 1)
        self.assertEqual(len(self.adapter.verify_calls), 1)

    def test_expired_claim_and_crash_before_query_are_recoverable(self):
        work, _, _, _, _ = self.separate_callback_work(callback_final=False)
        with patch.object(self.registry, "resolve", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_work(work)
        work.refresh_from_db()
        self.assertEqual(work.status, VerificationWorkStatus.CLAIMED)
        VerificationWorkItem.objects.filter(pk=work.pk).update(
            claim_expires_at=timezone.now() - timedelta(seconds=1)
        )
        recovered = self.run_work(work)
        self.assertEqual(recovered.verification.normalized_outcome, VerificationOutcome.CONFIRMED_SUCCESS)
        self.assertEqual(VerificationClaim.objects.filter(work_item=work).count(), 2)

    def test_crash_after_query_before_persistence_requeries_without_partial_truth(self):
        work, _, _, _, _ = self.separate_callback_work(callback_final=False)
        with patch(
            "cheatgame.financial_core.services.verification_worker.apply_verification_result",
            side_effect=RuntimeError("simulated crash"),
        ):
            with self.assertRaises(RuntimeError):
                self.run_work(work)
        self.assertEqual(Verification.objects.count(), 0)
        VerificationWorkItem.objects.filter(pk=work.pk).update(
            claim_expires_at=timezone.now() - timedelta(seconds=1)
        )
        recovered = self.run_work(work)
        self.assertEqual(recovered.verification.normalized_outcome, VerificationOutcome.CONFIRMED_SUCCESS)
        self.assertEqual(len(self.adapter.query_calls), 2)

    def test_observation_history_is_append_only_and_retry_identity_is_frozen(self):
        work, _, _, _, _ = self.separate_callback_work(callback_final=False)
        self.adapter.overrides = {
            "outcome": VerificationOutcome.PENDING,
            "financial_effect": VerificationFinancialEffect.NONE,
            "finality": VerificationFinality.NON_FINAL,
            "retryable": True,
        }
        first = self.run_work(work).verification
        VerificationWorkItem.objects.filter(pk=work.pk).update(
            next_attempt_at=timezone.now() - timedelta(seconds=1)
        )
        self.adapter.overrides = {}
        second = self.run_work(work).verification
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual([first.sequence, second.sequence], [1, 2])
        first.refresh_from_db()
        self.assertEqual(first.normalized_outcome, VerificationOutcome.PENDING)
        first_envelope, second_envelope = self.adapter.query_calls
        frozen = lambda envelope: (
            envelope.transaction_public_id,
            envelope.provider_key,
            envelope.merchant_account_key,
            envelope.merchant_account_version,
            envelope.operation_type,
        )
        self.assertEqual(frozen(first_envelope), frozen(second_envelope))
        with self.assertRaises(Exception):
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE financial_core_verification SET normalized_outcome = %s WHERE id = %s",
                        [VerificationOutcome.CONFIRMED_SUCCESS, first.pk],
                    )

    def test_incomplete_callback_evidence_uses_query_instead_of_manufacturing_agreement(self):
        incomplete_payloads = (
            {"provider_reference": None},
            {"merchant_reference": None},
            {"operation": None},
            {"amount": None},
            {"unit": None},
            {"hint": "pending"},
        )
        for index, body_overrides in enumerate(incomplete_payloads):
            _, account, _, transaction_obj = self.make_pending_graph()
            callback = self.callback(
                transaction_obj,
                account,
                event_id=f"incomplete-{index}",
                body_overrides=body_overrides,
            )
            incomplete_work = VerificationWorkItem.objects.get(pk=callback.verification_work_id)
            result = self.run_work(incomplete_work)
            self.assertTrue(result.used_provider_query)
            self.assertEqual(result.verification.normalized_outcome, VerificationOutcome.CONFIRMED_SUCCESS)

    def test_incomplete_callback_without_lookup_cannot_produce_success(self):
        self.supports_lookup = False
        _, account, _, transaction_obj = self.make_pending_graph()
        callback = self.callback(
            transaction_obj,
            account,
            event_id="incomplete-no-query",
            body_overrides={"provider_reference": None},
        )
        result = self.run_work(VerificationWorkItem.objects.get(pk=callback.verification_work_id))
        self.assertEqual(result.verification.normalized_outcome, VerificationOutcome.CONFIGURATION_FAILURE)
        self.assertEqual(result.verification.application_state, VerificationApplicationState.REVIEW_REQUIRED)

    def test_callback_authentication_and_key_versions_must_match_frozen_policy(self):
        self.callback_authentication_version = "other-auth-v2"
        work, _, _, _, _ = self.separate_callback_work()
        version_result = self.run_work(work)
        self.assertTrue(version_result.used_provider_query)

        self.callback_authentication_version = "test-v1"
        self.callback_signing_key_reference = "other-key-v2"
        work, _, _, _, _ = self.separate_callback_work()
        key_result = self.run_work(work)
        self.assertTrue(key_result.used_provider_query)

    def test_final_unpaid_then_late_success_is_temporal_contradiction(self):
        cases = (
            VerificationOutcome.CONFIRMED_DECLINE,
            VerificationOutcome.CONFIRMED_CANCELED,
            VerificationOutcome.CONFIRMED_EXPIRED,
            VerificationOutcome.NOT_FOUND_FINAL,
        )
        for index, unpaid_outcome in enumerate(cases):
            self.not_found_is_final_unpaid = unpaid_outcome == VerificationOutcome.NOT_FOUND_FINAL
            work, _, _, _, transaction_obj = self.separate_callback_work(callback_final=False)
            self.adapter.overrides = {
                "outcome": unpaid_outcome,
                "financial_effect": VerificationFinancialEffect.UNPAID,
                "finality": VerificationFinality.FINAL,
            }
            unpaid = self.run_work(work).verification
            self.assertEqual(unpaid.application_state, VerificationApplicationState.APPLIED_UNPAID)
            followup = self.enqueue_followup(transaction_obj, suffix=f"late-success-{index}")
            self.adapter.overrides = {}
            late = self.run_work(
                followup,
                trigger=VerificationTriggerSource.UNKNOWN_OUTCOME,
            ).verification
            self.assertEqual(late.normalized_outcome, VerificationOutcome.CONTRADICTORY_EVIDENCE)
            self.assertEqual(late.application_state, VerificationApplicationState.REVIEW_REQUIRED)
            interpretation = derive_current_verification_interpretation(transaction_id=transaction_obj.pk)
            self.assertEqual(interpretation.state, VerificationInterpretationState.BLOCKED_REVIEW)

    def test_retry_exhaustion_creates_terminal_review_not_waiting(self):
        for mode, overrides in (
            (
                "result",
                {
                    "outcome": VerificationOutcome.PENDING,
                    "financial_effect": VerificationFinancialEffect.NONE,
                    "finality": VerificationFinality.NON_FINAL,
                    "retryable": True,
                },
            ),
            (
                "result",
                {
                    "outcome": VerificationOutcome.OUTCOME_UNKNOWN,
                    "financial_effect": VerificationFinancialEffect.UNKNOWN,
                    "finality": VerificationFinality.UNKNOWN,
                    "retryable": True,
                },
            ),
            ("timeout", {},),
        ):
            self.callback_final = False
            _, _, _, transaction_obj = self.make_pending_graph()
            work = enqueue_verification_work(
                transaction_obj=transaction_obj,
                work_type=VerificationWorkType.VERIFY_UNKNOWN_OUTCOME,
                deterministic_identity=f"api06-exhaustion:{uuid4()}",
                correlation_id=transaction_obj.correlation_id,
                max_attempts=1,
            )[0]
            self.adapter.mode = mode
            self.adapter.overrides = overrides
            observation = self.run_work(work).verification
            work.refresh_from_db()
            self.assertEqual(work.status, VerificationWorkStatus.COMPLETED)
            self.assertEqual(observation.application_state, VerificationApplicationState.REVIEW_REQUIRED)
            self.assertEqual(observation.error_classification, "verification_attempts_exhausted")
            self.assertEqual(
                derive_current_verification_interpretation(transaction_id=transaction_obj.pk).state,
                VerificationInterpretationState.BLOCKED_REVIEW,
            )

    def test_postgresql_verification_lineage_guards_reject_raw_forgery(self):
        valid = self.run_work(self.callback_work()).verification
        foreign_work, _, _, _, foreign_tx = self.separate_callback_work(callback_final=False)
        foreign_claim = claim_verification_work(
            work_item_id=foreign_work.pk,
            trigger_source=VerificationTriggerSource.UNKNOWN_OUTCOME,
            claim_idempotency_key=uuid4(),
        ).claim
        base = {
            "public_id": uuid4(),
            "result_idempotency_key": uuid4(),
            "result_fingerprint": "f" * 64,
            "sequence": valid.sequence + 1,
        }
        attacks = (
            {**base, "claim_id": foreign_claim.pk},
            {**base, "work_item_id": foreign_work.pk},
            {**base, "provider_event_id": foreign_work.provider_event_id},
            {**base, "provider_id": foreign_tx.capability_version.provider_id},
            {**base, "capability_version_id": foreign_tx.capability_version_id},
            {**base, "merchant_account_version_id": foreign_tx.merchant_account_version_id},
            {**base, "sequence": valid.sequence + 5},
        )
        for overrides in attacks:
            with self.assertRaises(DatabaseError), transaction.atomic():
                self.raw_clone_verification(valid, **overrides)

        success_overrides = {
            "public_id": uuid4(),
            "transaction_id": foreign_tx.pk,
            "claim_id": foreign_claim.pk,
            "work_item_id": foreign_work.pk,
            "provider_event_id": foreign_work.provider_event_id,
            "provider_id": foreign_tx.capability_version.provider_id,
            "capability_version_id": foreign_tx.capability_version_id,
            "merchant_account_version_id": foreign_tx.merchant_account_version_id,
            "sequence": 1,
            "merchant_reference": foreign_tx.merchant_reference,
            "provider_reference": "unallocated-provider-reference",
            "operation_type": foreign_tx.operation_type,
            "requested_provider_amount": foreign_tx.provider_amount,
            "requested_provider_unit": foreign_tx.provider_unit,
            "observed_provider_amount": foreign_tx.provider_amount,
            "observed_provider_unit": foreign_tx.provider_unit,
            "canonical_allocation_amount": foreign_tx.amount,
            "canonical_currency": foreign_tx.currency,
            "evidence_basis": VerificationEvidenceBasis.SERVER_TO_SERVER,
            "result_idempotency_key": uuid4(),
            "result_fingerprint": "e" * 64,
        }
        with self.assertRaises(DatabaseError), transaction.atomic():
            self.raw_clone_verification(valid, **success_overrides)

    def test_success_evidence_basis_is_authoritative_at_every_boundary(self):
        for basis in ("none", "browser", "callback_hint", "callback_assertion"):
            work, _, _, _, transaction_obj = self.separate_callback_work(callback_final=False)
            self.adapter.overrides = {"evidence_basis": basis}
            observation = self.run_work(
                work, trigger=VerificationTriggerSource.UNKNOWN_OUTCOME
            ).verification
            self.assertNotEqual(observation.normalized_outcome, VerificationOutcome.CONFIRMED_SUCCESS)
            self.assertEqual(observation.application_state, VerificationApplicationState.REVIEW_REQUIRED)
            self.assertEqual(
                derive_current_verification_interpretation(transaction_id=transaction_obj.pk).state,
                VerificationInterpretationState.BLOCKED_REVIEW,
            )

        self.adapter.overrides = {}
        query_work, _, _, _, query_tx = self.separate_callback_work(callback_final=False)
        query_success = self.run_work(
            query_work, trigger=VerificationTriggerSource.UNKNOWN_OUTCOME
        ).verification
        self.assertEqual(query_success.evidence_basis, VerificationEvidenceBasis.SERVER_TO_SERVER)
        self.assertEqual(
            derive_current_verification_interpretation(transaction_id=query_tx.pk).state,
            VerificationInterpretationState.ELIGIBLE_FINAL_PAID,
        )
        callback_work, _, _, _, callback_tx = self.separate_callback_work(callback_final=True)
        callback_success = self.run_work(callback_work).verification
        self.assertEqual(callback_success.evidence_basis, VerificationEvidenceBasis.AUTHENTICATED_SETTLEMENT)
        self.assertEqual(
            derive_current_verification_interpretation(transaction_id=callback_tx.pk).state,
            VerificationInterpretationState.ELIGIBLE_FINAL_PAID,
        )

        foreign_work, _, _, _, foreign_tx = self.separate_callback_work(callback_final=False)
        foreign_claim = claim_verification_work(
            work_item_id=foreign_work.pk,
            trigger_source=VerificationTriggerSource.UNKNOWN_OUTCOME,
            claim_idempotency_key=uuid4(),
        ).claim
        for basis in ("none", "browser", "callback_hint", "callback_assertion"):
            with self.assertRaises(DatabaseError), transaction.atomic():
                self.raw_clone_verification(
                    callback_success,
                    public_id=uuid4(), transaction_id=foreign_tx.pk,
                    claim_id=foreign_claim.pk, work_item_id=foreign_work.pk,
                    provider_event_id=foreign_work.provider_event_id,
                    provider_id=foreign_tx.capability_version.provider_id,
                    capability_version_id=foreign_tx.capability_version_id,
                    merchant_account_version_id=foreign_tx.merchant_account_version_id,
                    sequence=1, merchant_reference=foreign_tx.merchant_reference,
                    provider_reference=f"raw-{basis}", operation_type=foreign_tx.operation_type,
                    requested_provider_amount=foreign_tx.provider_amount,
                    requested_provider_unit=foreign_tx.provider_unit,
                    observed_provider_amount=foreign_tx.provider_amount,
                    observed_provider_unit=foreign_tx.provider_unit,
                    canonical_allocation_amount=foreign_tx.amount,
                    canonical_currency=foreign_tx.currency, evidence_basis=basis,
                    result_idempotency_key=uuid4(), result_fingerprint="b" * 64,
                )

    def test_raw_allocation_requires_exact_verification_ownership(self):
        valid = self.run_work(self.callback_work()).verification
        foreign_work, _, _, _, foreign_tx = self.separate_callback_work(callback_final=False)
        foreign_claim = claim_verification_work(
            work_item_id=foreign_work.pk,
            trigger_source=VerificationTriggerSource.UNKNOWN_OUTCOME,
            claim_idempotency_key=uuid4(),
        ).claim
        for defect in ("verification", "transaction", "account", "reference"):
            public_id = uuid4()
            provider_reference = f"allocation-{defect}-{uuid4()}"
            with self.assertRaises(DatabaseError), transaction.atomic():
                self.raw_clone_verification(
                    valid,
                    public_id=public_id, transaction_id=foreign_tx.pk,
                    claim_id=foreign_claim.pk, work_item_id=foreign_work.pk,
                    provider_event_id=foreign_work.provider_event_id,
                    provider_id=foreign_tx.capability_version.provider_id,
                    capability_version_id=foreign_tx.capability_version_id,
                    merchant_account_version_id=foreign_tx.merchant_account_version_id,
                    sequence=1, merchant_reference=foreign_tx.merchant_reference,
                    provider_reference=provider_reference, operation_type=foreign_tx.operation_type,
                    requested_provider_amount=foreign_tx.provider_amount,
                    requested_provider_unit=foreign_tx.provider_unit,
                    observed_provider_amount=foreign_tx.provider_amount,
                    observed_provider_unit=foreign_tx.provider_unit,
                    canonical_allocation_amount=foreign_tx.amount,
                    canonical_currency=foreign_tx.currency,
                    evidence_basis=VerificationEvidenceBasis.SERVER_TO_SERVER,
                    result_idempotency_key=uuid4(), result_fingerprint="a" * 64,
                )
                inserted = Verification.objects.get(public_id=public_id)
                self.raw_insert_reference_allocation(
                    verification_id=valid.pk if defect == "verification" else inserted.pk,
                    transaction_id=self.transaction_obj.pk if defect == "transaction" else foreign_tx.pk,
                    account_id=self.account.pk if defect == "account" else foreign_tx.merchant_account_version_id,
                    provider_reference="wrong-reference" if defect == "reference" else provider_reference,
                )

    def test_two_workers_have_one_claim_and_one_result(self):
        work = self.callback_work()
        entered_provider = Event()
        release_provider = Event()
        original_verify = self.adapter.verify_operation

        def blocked_verify(envelope):
            entered_provider.set()
            release_provider.wait(timeout=10)
            return original_verify(envelope)

        self.adapter.verify_operation = blocked_verify
        outcomes = []

        def runner(root_key):
            close_old_connections()
            try:
                outcomes.append(("ok", self.run_work(work, root_key=root_key)))
            except VerificationClaimConflict:
                outcomes.append(("blocked", None))
            finally:
                close_old_connections()

        first = Thread(target=runner, args=(uuid4(),))
        second = Thread(target=runner, args=(uuid4(),))
        first.start()
        self.assertTrue(entered_provider.wait(timeout=10))
        second.start()
        second.join(timeout=10)
        release_provider.set()
        first.join(timeout=10)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(sorted(kind for kind, _ in outcomes), ["blocked", "ok"])
        self.assertEqual(Verification.objects.filter(work_item=work).count(), 1)
        self.assertEqual(VerificationClaim.objects.filter(work_item=work).count(), 1)

    def test_query_budget_is_bounded(self):
        work = self.callback_work()
        with CaptureQueriesContext(connection) as captured:
            self.run_work(work)
        self.assertLessEqual(len(captured), 80)
