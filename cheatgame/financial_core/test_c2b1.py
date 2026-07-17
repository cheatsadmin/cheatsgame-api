import hashlib
import hmac
import json
from datetime import timedelta
from decimal import Decimal
from threading import Barrier, Thread
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.test import TransactionTestCase

from cheatgame.financial_core.models import (
    CallbackAuthenticationStatus,
    CallbackAuthenticationStrength,
    CallbackProcessingStatus,
    CallbackReceipt,
    CallbackReplayWindowStatus,
    FinancialEvent,
    JournalEntry,
    PaymentCollectionStatus,
    ProviderEvent,
    ProviderRequestOutcome,
    ReviewCase,
    Verification,
    VerificationApplicationState,
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
    CallbackAuthenticationResult,
    NormalizedCallbackEvent,
    NormalizedVerificationResult,
    ProviderAdapterRegistry,
    execute_verification_outside_transaction,
)
from cheatgame.financial_core.services.boundaries import ExternalIOInsideTransaction
from cheatgame.financial_core.services.callbacks import (
    MAX_CALLBACK_BODY_BYTES,
    ingest_callback_delivery,
)
from cheatgame.financial_core.services.provider_requests import (
    CollectionBlocked,
    apply_provider_request_result,
    claim_provider_request,
    create_or_replay_payment_attempt,
)
from cheatgame.financial_core.services.verification import (
    VerificationClaimConflict,
    StaleVerificationClaim,
    apply_verification_result,
    claim_verification_work,
    enqueue_verification_work,
)
from cheatgame.financial_core.test_c2a import C2AFixture


TEST_SIGNING_KEY = b"c2b1-test-signing-key"


class SyntheticC2B1Adapter:
    adapter_key = "synthetic"
    contract_version = ADAPTER_CONTRACT_VERSION

    def execute_operation(self, envelope):
        raise AssertionError("C2B1 callback tests must not execute payment requests")

    def authenticate_callback(self, *, headers, body):
        expected = hmac.new(TEST_SIGNING_KEY, body, hashlib.sha256).hexdigest()
        supplied = headers.get("X-Signature", "")
        authenticated = hmac.compare_digest(expected, supplied)
        decoded = json.loads(body.decode("utf-8")) if authenticated else None
        return CallbackAuthenticationResult(
            status=(
                CallbackAuthenticationStatus.AUTHENTICATED
                if authenticated
                else CallbackAuthenticationStatus.INVALID
            ),
            strength=CallbackAuthenticationStrength.SHARED_SECRET,
            method="hmac-sha256",
            version="test-v1",
            signing_key_reference="test-key-v1",
            replay_window_status=CallbackReplayWindowStatus.VALID,
            trustworthy_provider_event_id=(decoded or {}).get("event_id", ""),
            safe_reason_code="valid" if authenticated else "signature_invalid",
            evidence_hash=hashlib.sha256(body + b":auth").hexdigest(),
            authenticated_context=decoded,
        )

    def normalize_callback(self, authenticated_callback):
        data = authenticated_callback.authenticated_context
        return NormalizedCallbackEvent(
            merchant_reference=data.get("merchant_reference", ""),
            provider_authority=data.get("authority", ""),
            provider_reference=data.get("provider_reference", ""),
            operation_type_hint="sale",
            provider_amount_hint=data.get("amount"),
            provider_unit_hint=data.get("unit", ""),
            normalized_hint=data.get("hint", "pending"),
        )

    def verify_operation(self, envelope):
        raise AssertionError("Use an explicit normalized synthetic result")

    def query_operation(self, envelope):
        raise AssertionError("Use an explicit normalized synthetic result")

    def read_reconciliation_records(self, *, period_start, period_end):
        return ()


class TimeoutVerificationAdapter(SyntheticC2B1Adapter):
    def verify_operation(self, envelope):
        raise TimeoutError("synthetic timeout detail must not escape")


class C2B1Fixture(C2AFixture):
    def make_pending_graph(self):
        placement, account, attempt, transaction_obj = self.make_request_graph()
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
        return placement, account, attempt, transaction_obj

    def callback(self, transaction_obj, account, *, event_id="evt-1", body_overrides=None, signature=True):
        payload = {
            "event_id": event_id,
            "merchant_reference": transaction_obj.merchant_reference,
            "authority": "authority-1",
            "provider_reference": "provider-reference-1",
            "amount": int(transaction_obj.provider_amount),
            "unit": transaction_obj.provider_unit,
            "hint": "success",
        }
        payload.update(body_overrides or {})
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "X-Signature": (
                hmac.new(TEST_SIGNING_KEY, body, hashlib.sha256).hexdigest()
                if signature
                else "invalid"
            ),
        }
        registry = ProviderAdapterRegistry(
            {("synthetic", ADAPTER_CONTRACT_VERSION): SyntheticC2B1Adapter()}
        )
        return ingest_callback_delivery(
            provider_key=account.provider.key,
            capability_version=account.capability_version.version,
            account_key=account.account_key,
            account_version=account.version,
            method="POST",
            content_type="application/json",
            body=body,
            headers=headers,
            delivery_idempotency_key=uuid4(),
            adapter_registry=registry,
            source_network="192.0.2.10",
        )

    def verification_claim(self, transaction_obj, account):
        callback = self.callback(transaction_obj, account)
        claim = claim_verification_work(
            work_item_id=callback.verification_work_id,
            trigger_source=VerificationTriggerSource.CALLBACK,
            claim_idempotency_key=uuid4(),
        )
        return callback, claim

    def normalized_result(self, transaction_obj, account, *, outcome=VerificationOutcome.CONFIRMED_SUCCESS, **overrides):
        values = {
            "outcome": outcome,
            "financial_effect": (
                VerificationFinancialEffect.PAID
                if outcome == VerificationOutcome.CONFIRMED_SUCCESS
                else VerificationFinancialEffect.UNPAID
            ),
            "finality": VerificationFinality.FINAL,
            "transport_classification": VerificationTransportClassification.SUCCESS,
            "provider_key": transaction_obj.provider,
            "adapter_contract_version": transaction_obj.adapter_contract_version,
            "merchant_account_key": account.account_key,
            "merchant_account_version": account.version,
            "merchant_reference": transaction_obj.merchant_reference,
            "provider_authority": "authority-1",
            "provider_reference": "provider-reference-1",
            "operation_type": transaction_obj.operation_type,
            "observed_provider_amount": transaction_obj.provider_amount,
            "observed_provider_unit": transaction_obj.provider_unit,
            "evidence_hash": "2" * 64,
            "evidence_basis": VerificationEvidenceBasis.SERVER_TO_SERVER,
        }
        values.update(overrides)
        return NormalizedVerificationResult(**values)


class C2B1CallbackAndVerificationTests(C2B1Fixture, TransactionTestCase):
    reset_sequences = True
    def test_authenticated_callback_creates_auditable_deduplicated_evidence_and_work(self):
        _, account, _, transaction_obj = self.make_pending_graph()
        first = self.callback(transaction_obj, account)
        second = self.callback(transaction_obj, account)
        self.assertEqual(CallbackReceipt.objects.count(), 2)
        self.assertEqual(ProviderEvent.objects.count(), 1)
        self.assertEqual(VerificationWorkItem.objects.count(), 1)
        self.assertEqual(first.provider_event.pk, second.provider_event.pk)
        self.assertEqual(second.receipt.processing_status, CallbackProcessingStatus.DUPLICATE)
        self.assertEqual(len(first.receipt.authentication_evidence_hash), 64)
        self.assertFalse(hasattr(first.receipt, "raw_body"))

    def test_changed_body_under_trusted_event_id_is_quarantined_as_contradiction(self):
        _, account, _, transaction_obj = self.make_pending_graph()
        first = self.callback(transaction_obj, account)
        changed = self.callback(
            transaction_obj,
            account,
            body_overrides={"amount": int(transaction_obj.provider_amount) + 1},
        )
        self.assertEqual(first.provider_event.pk, changed.provider_event.pk)
        self.assertEqual(changed.receipt.processing_status, CallbackProcessingStatus.QUARANTINED)
        self.assertEqual(changed.receipt.quarantine_reason, "contradictory_callback_evidence")

    def test_invalid_signature_and_unknown_reference_never_create_financial_aggregates(self):
        _, account, _, transaction_obj = self.make_pending_graph()
        counts = tuple(model.objects.count() for model in (type(transaction_obj.attempt.payment), type(transaction_obj.attempt), type(transaction_obj)))
        invalid = self.callback(transaction_obj, account, signature=False)
        unknown = self.callback(
            transaction_obj,
            account,
            event_id="evt-unknown",
            body_overrides={"merchant_reference": "unknown-reference"},
        )
        self.assertEqual(invalid.receipt.processing_status, CallbackProcessingStatus.SECURITY_REJECTED)
        self.assertEqual(unknown.provider_event.transaction_id, None)
        self.assertEqual(unknown.provider_event.resolution_status, "quarantined")
        self.assertEqual(counts, tuple(model.objects.count() for model in (type(transaction_obj.attempt.payment), type(transaction_obj.attempt), type(transaction_obj))))

    def test_transport_limits_preserve_only_bounded_hashed_evidence(self):
        _, account, _, transaction_obj = self.make_pending_graph()
        result = ingest_callback_delivery(
            provider_key=account.provider.key,
            capability_version=account.capability_version.version,
            account_key=account.account_key,
            account_version=account.version,
            method="POST",
            content_type="application/json",
            body=b"x" * (MAX_CALLBACK_BODY_BYTES + 1),
            headers={},
            delivery_idempotency_key=uuid4(),
            adapter_registry=ProviderAdapterRegistry(),
        )
        self.assertEqual(result.receipt.quarantine_reason, "body_too_large")
        self.assertEqual(len(result.receipt.raw_envelope_hash), 64)
        self.assertEqual(result.receipt.header_evidence, {})

    def test_callback_and_verification_records_are_database_append_only(self):
        _, account, _, transaction_obj = self.make_pending_graph()
        callback, claim = self.verification_claim(transaction_obj, account)
        verification = apply_verification_result(
            claim_token=claim.claim.claim_token,
            result=self.normalized_result(transaction_obj, account),
            result_idempotency_key=uuid4(),
            trigger_source=VerificationTriggerSource.CALLBACK,
        )
        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE financial_core_callbackreceipt SET quarantine_reason = %s WHERE id = %s",
                    ["changed", callback.receipt.pk],
                )
        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM financial_core_verification WHERE id = %s",
                    [verification.pk],
                )

    def test_success_is_blocking_evidence_only_and_never_posts_or_marks_paid(self):
        placement, account, _, transaction_obj = self.make_pending_graph()
        reservation_before = placement.order.stock_reservations.get().state
        _, claim = self.verification_claim(transaction_obj, account)
        verification = apply_verification_result(
            claim_token=claim.claim.claim_token,
            result=self.normalized_result(transaction_obj, account),
            result_idempotency_key=uuid4(),
            trigger_source=VerificationTriggerSource.CALLBACK,
        )
        placement.payment.refresh_from_db()
        self.assertEqual(verification.application_state, VerificationApplicationState.APPLIED_BLOCKING_SUCCESS)
        self.assertEqual(placement.payment.collection_status, PaymentCollectionStatus.REVIEW)
        self.assertEqual(placement.payment.confirmed_amount, Decimal("0"))
        self.assertEqual(JournalEntry.objects.count(), 0)
        self.assertEqual(placement.order.stock_reservations.get().state, reservation_before)
        self.assertTrue(ReviewCase.objects.filter(payment=placement.payment).exists())
        self.assertTrue(
            VerificationWorkItem.objects.filter(work_type="apply_verified_funds").exists()
        )
        with self.assertRaises(CollectionBlocked):
            create_or_replay_payment_attempt(
                payment_id=placement.payment.pk,
                merchant_account_version_id=account.pk,
                tender_type="external_provider",
                requested_amount=placement.payment.amount_due,
                idempotency_key=uuid4(),
            )

    def test_amount_mismatch_is_review_evidence_not_success(self):
        placement, account, _, transaction_obj = self.make_pending_graph()
        _, claim = self.verification_claim(transaction_obj, account)
        verification = apply_verification_result(
            claim_token=claim.claim.claim_token,
            result=self.normalized_result(
                transaction_obj,
                account,
                observed_provider_amount=transaction_obj.provider_amount + 1,
            ),
            result_idempotency_key=uuid4(),
            trigger_source=VerificationTriggerSource.CALLBACK,
        )
        self.assertEqual(verification.normalized_outcome, VerificationOutcome.MISMATCH)
        self.assertEqual(JournalEntry.objects.count(), 0)
        self.assertTrue(ReviewCase.objects.filter(payment=placement.payment).exists())

    def test_repeated_mismatch_escalates_one_logical_review_through_append_only_event(self):
        placement, account, _, transaction_obj = self.make_pending_graph()
        _, first_claim = self.verification_claim(transaction_obj, account)
        mismatch = self.normalized_result(
            transaction_obj,
            account,
            observed_provider_amount=transaction_obj.provider_amount + 1,
        )
        apply_verification_result(
            claim_token=first_claim.claim.claim_token,
            result=mismatch,
            result_idempotency_key=uuid4(),
            trigger_source=VerificationTriggerSource.CALLBACK,
        )
        retry_work, _ = enqueue_verification_work(
            transaction_obj=transaction_obj,
            work_type=VerificationWorkType.RETRY_PROVIDER_QUERY,
            deterministic_identity=f"test-review-escalation:{transaction_obj.public_id}",
            correlation_id=uuid4(),
        )
        second_claim = claim_verification_work(
            work_item_id=retry_work.pk,
            trigger_source=VerificationTriggerSource.POLL,
            claim_idempotency_key=uuid4(),
        )
        apply_verification_result(
            claim_token=second_claim.claim.claim_token,
            result=mismatch,
            result_idempotency_key=uuid4(),
            trigger_source=VerificationTriggerSource.POLL,
        )
        review = ReviewCase.objects.get(payment=placement.payment)
        self.assertEqual(review.version, 2)
        self.assertEqual(
            FinancialEvent.objects.filter(
                aggregate_id=str(review.public_id),
                event_type="review_case.escalated",
            ).count(),
            1,
        )

    def test_final_decline_reopens_only_without_other_blockers(self):
        placement, account, attempt, transaction_obj = self.make_pending_graph()
        _, claim = self.verification_claim(transaction_obj, account)
        verification = apply_verification_result(
            claim_token=claim.claim.claim_token,
            result=self.normalized_result(
                transaction_obj,
                account,
                outcome=VerificationOutcome.CONFIRMED_DECLINE,
            ),
            result_idempotency_key=uuid4(),
            trigger_source=VerificationTriggerSource.CALLBACK,
        )
        placement.payment.refresh_from_db()
        attempt.refresh_from_db()
        self.assertEqual(verification.application_state, VerificationApplicationState.APPLIED_UNPAID)
        self.assertEqual(placement.payment.collection_status, PaymentCollectionStatus.OPEN)
        self.assertEqual(attempt.status, "definitive_failed")

    def test_not_found_without_explicit_capability_is_unknown(self):
        placement, account, _, transaction_obj = self.make_pending_graph()
        _, claim = self.verification_claim(transaction_obj, account)
        verification = apply_verification_result(
            claim_token=claim.claim.claim_token,
            result=self.normalized_result(
                transaction_obj,
                account,
                outcome=VerificationOutcome.NOT_FOUND_FINAL,
            ),
            result_idempotency_key=uuid4(),
            trigger_source=VerificationTriggerSource.CALLBACK,
        )
        placement.payment.refresh_from_db()
        self.assertEqual(verification.normalized_outcome, VerificationOutcome.OUTCOME_UNKNOWN)
        self.assertEqual(placement.payment.collection_status, PaymentCollectionStatus.REVIEW)

    def test_adapter_verification_is_forbidden_inside_atomic(self):
        adapter = SyntheticC2B1Adapter()
        with self.assertRaises(ExternalIOInsideTransaction), transaction.atomic():
            execute_verification_outside_transaction(adapter=adapter, envelope=object())

    def test_provider_timeout_normalizes_to_unknown_without_leaking_exception_detail(self):
        placement, account, _, transaction_obj = self.make_pending_graph()
        _, claim = self.verification_claim(transaction_obj, account)
        result = execute_verification_outside_transaction(
            adapter=TimeoutVerificationAdapter(),
            envelope=claim.envelope,
        )
        self.assertEqual(result.outcome, VerificationOutcome.OUTCOME_UNKNOWN)
        self.assertEqual(result.transport_classification, VerificationTransportClassification.TIMEOUT)
        self.assertNotIn("detail", result.error_classification)
        verification = apply_verification_result(
            claim_token=claim.claim.claim_token,
            result=result,
            result_idempotency_key=uuid4(),
            trigger_source=VerificationTriggerSource.CALLBACK,
        )
        placement.payment.refresh_from_db()
        self.assertEqual(verification.application_state, VerificationApplicationState.REVIEW_REQUIRED)
        self.assertEqual(placement.payment.collection_status, PaymentCollectionStatus.REVIEW)

    def test_raw_sql_provider_receipt_journal_is_blocked_in_c2b1(self):
        with self.assertRaises(DatabaseError), transaction.atomic():
            JournalEntry.objects.create(
                source_type="provider_receipt",
                source_id="forged",
                idempotency_key=uuid4(),
            )


class C2B1ConcurrencyTests(C2B1Fixture, TransactionTestCase):
    reset_sequences = True

    def test_duplicate_callbacks_concurrently_create_two_receipts_one_event_and_one_work(self):
        _, account, _, transaction_obj = self.make_pending_graph()
        barrier = Barrier(2)
        outcomes = []

        def runner():
            close_old_connections()
            try:
                barrier.wait()
                outcomes.append(("ok", self.callback(transaction_obj, account)))
            except Exception as exc:
                outcomes.append(("error", exc))
            finally:
                close_old_connections()

        threads = [Thread(target=runner) for _ in range(2)]
        for item in threads:
            item.start()
        for item in threads:
            item.join(timeout=20)
        self.assertTrue(all(not item.is_alive() for item in threads))
        self.assertEqual([kind for kind, _ in outcomes], ["ok", "ok"])
        self.assertEqual(CallbackReceipt.objects.count(), 2)
        self.assertEqual(ProviderEvent.objects.count(), 1)
        self.assertEqual(VerificationWorkItem.objects.count(), 1)

    def test_two_verification_claims_have_one_winner(self):
        _, account, _, transaction_obj = self.make_pending_graph()
        callback = self.callback(transaction_obj, account)
        barrier = Barrier(2)
        outcomes = []

        def runner():
            close_old_connections()
            try:
                barrier.wait()
                claim_verification_work(
                    work_item_id=callback.verification_work_id,
                    trigger_source=VerificationTriggerSource.CALLBACK,
                    claim_idempotency_key=uuid4(),
                )
                outcomes.append("winner")
            except VerificationClaimConflict:
                outcomes.append("blocked")
            finally:
                close_old_connections()

        threads = [Thread(target=runner) for _ in range(2)]
        for item in threads:
            item.start()
        for item in threads:
            item.join(timeout=20)
        self.assertEqual(sorted(outcomes), ["blocked", "winner"])
        self.assertEqual(VerificationWorkItem.objects.get(pk=callback.verification_work_id).status, VerificationWorkStatus.CLAIMED)

    def test_expired_verification_claim_is_recoverable_without_inventing_unpaid_truth(self):
        _, account, _, transaction_obj = self.make_pending_graph()
        callback = self.callback(transaction_obj, account)
        first = claim_verification_work(
            work_item_id=callback.verification_work_id,
            trigger_source=VerificationTriggerSource.CALLBACK,
            claim_idempotency_key=uuid4(),
            lease_seconds=5,
        )
        with patch(
            "cheatgame.financial_core.services.verification.timezone.now",
            return_value=first.claim.expires_at + timedelta(seconds=1),
        ):
            second = claim_verification_work(
                work_item_id=callback.verification_work_id,
                trigger_source=VerificationTriggerSource.UNKNOWN_OUTCOME,
                claim_idempotency_key=uuid4(),
                lease_seconds=5,
            )
        self.assertNotEqual(first.claim.claim_token, second.claim.claim_token)
        with self.assertRaises(StaleVerificationClaim):
            apply_verification_result(
                claim_token=first.claim.claim_token,
                result=self.normalized_result(transaction_obj, account),
                result_idempotency_key=uuid4(),
                trigger_source=VerificationTriggerSource.CALLBACK,
            )
        transaction_obj.refresh_from_db()
        self.assertNotEqual(transaction_obj.status, "declined")
