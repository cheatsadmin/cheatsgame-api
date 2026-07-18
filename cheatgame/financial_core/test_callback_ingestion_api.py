import hashlib
import hmac
import json
from threading import Barrier, Lock, Thread
from unittest.mock import patch
from uuid import uuid4

from django.db import DatabaseError, close_old_connections, connection, transaction
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from cheatgame.digital_products.models import (
    DigitalFulfillmentItem,
    DigitalInventoryReservation,
    Entitlement,
)
from cheatgame.financial_core.callback_apis import ProviderCallbackIngestionApi
from cheatgame.financial_core.models import (
    CallbackAuthenticationStatus,
    CallbackAuthenticationStrength,
    CallbackProcessingStatus,
    CallbackReceipt,
    CallbackReplayWindowStatus,
    CommercialFinalization,
    FinancialAllocation,
    JournalEntry,
    MerchantAccountVersion,
    MoneyUnit,
    Payment,
    PaymentAttempt,
    PaymentTransaction,
    PaymentTransactionOperation,
    ProviderCapabilityVersion,
    ProviderDefinition,
    ProviderEvent,
    ProviderEventResolutionStatus,
    VerificationWorkItem,
    VerificationWorkType,
)
from cheatgame.financial_core.services.adapters import (
    ADAPTER_CONTRACT_VERSION,
    CallbackAuthenticationResult,
    NormalizedCallbackEvent,
    ProviderAdapterRegistry,
)
from cheatgame.financial_core.test_c2b1 import C2B1Fixture
from cheatgame.shop.models import Order


SIGNING_KEY = b"api-05-test-signing-key"


class CallbackAdapter:
    adapter_key = "synthetic"
    contract_version = ADAPTER_CONTRACT_VERSION

    def __init__(
        self,
        *,
        replay_status=CallbackReplayWindowStatus.VALID,
        auth_status=CallbackAuthenticationStatus.AUTHENTICATED,
        key_version="callback-key-v1",
        normalization_error=False,
        barrier=None,
    ):
        self.replay_status = replay_status
        self.auth_status = auth_status
        self.key_version = key_version
        self.normalization_error = normalization_error
        self.barrier = barrier

    def execute_operation(self, envelope):
        raise AssertionError("API-05 cannot execute provider requests")

    def authenticate_callback(self, *, headers, body):
        expected = hmac.new(SIGNING_KEY, body, hashlib.sha256).hexdigest()
        valid = hmac.compare_digest(expected, headers.get("X-Signature", ""))
        if self.barrier is not None:
            self.barrier.wait(timeout=10)
        status = self.auth_status if valid else CallbackAuthenticationStatus.INVALID
        decoded = json.loads(body.decode("utf-8")) if valid else {}
        return CallbackAuthenticationResult(
            status=status,
            strength=(
                CallbackAuthenticationStrength.NONE
                if status == CallbackAuthenticationStatus.UNAUTHENTICATED_HINT
                else CallbackAuthenticationStrength.SHARED_SECRET
            ),
            method="hmac-sha256" if status != CallbackAuthenticationStatus.UNAUTHENTICATED_HINT else "none",
            version=self.key_version,
            signing_key_reference=self.key_version,
            replay_window_status=self.replay_status,
            trustworthy_provider_event_id=(decoded.get("event_id", "") if valid else ""),
            safe_reason_code="valid" if valid else "invalid_signature",
            evidence_hash=hashlib.sha256(body + b":authenticated").hexdigest(),
            authenticated_context=decoded,
        )

    def normalize_callback(self, authenticated_callback):
        if self.normalization_error:
            raise ValueError("provider detail must not escape")
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
        raise AssertionError("Verification remains dormant")

    def query_operation(self, envelope):
        raise AssertionError("Provider query remains dormant")

    def read_reconciliation_records(self, *, period_start, period_end):
        return ()


class ProviderCallbackIngestionApiTests(C2B1Fixture, TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.client = APIClient()
        self.callback_authentication = CallbackAuthenticationStrength.SHARED_SECRET
        self.placement, self.account, self.attempt, self.transaction_obj = self.make_pending_graph()
        self.adapter = CallbackAdapter()
        self.registry = ProviderAdapterRegistry(
            {("synthetic", ADAPTER_CONTRACT_VERSION): self.adapter}
        )

    def make_account(self, *, unit=MoneyUnit.IRR, enabled=True, version=1):
        provider = ProviderDefinition.objects.create(
            key=f"synthetic-{uuid4()}",
            display_name="Synthetic Callback Provider",
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
            supports_lookup=True,
            callback_authentication=self.callback_authentication,
            finality_window_seconds=3600,
            authority_expiry_seconds=900,
        )
        account = MerchantAccountVersion.objects.create(
            provider=provider,
            capability_version=capability,
            account_key="platform-primary",
            version=version,
            owner_key="cheats-game",
            credential_reference="env://SYNTHETIC_CALLBACK_CREDENTIAL",
            is_enabled=enabled,
            new_requests_enabled=enabled,
        )
        return provider, capability, account

    def use_new_graph(self, *, callback_authentication):
        self.callback_authentication = callback_authentication
        self.placement, self.account, self.attempt, self.transaction_obj = self.make_pending_graph()

    @property
    def url(self):
        return (
            f"/api/financial-core/providers/{self.account.provider.key}/callbacks/"
            f"{self.transaction_obj.public_id}/"
        )

    def body(self, **overrides):
        payload = {
            "event_id": "event-api-05",
            "merchant_reference": self.transaction_obj.merchant_reference,
            "authority": "authority-api-05",
            "provider_reference": "provider-reference-api-05",
            "amount": int(self.transaction_obj.provider_amount),
            "unit": self.transaction_obj.provider_unit,
            "hint": "success",
        }
        payload.update(overrides)
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def post(self, body=None, *, adapter=None, url=None, content_type="application/json"):
        body = body or self.body()
        signature = hmac.new(SIGNING_KEY, body, hashlib.sha256).hexdigest()
        registry = ProviderAdapterRegistry(
            {("synthetic", ADAPTER_CONTRACT_VERSION): adapter or self.adapter}
        )
        headers = {"HTTP_X_SIGNATURE": signature}
        with patch.object(ProviderCallbackIngestionApi, "adapter_registry", registry):
            return APIClient().post(
                url or self.url,
                data=body,
                content_type=content_type,
                **headers,
            )

    def graph_counts(self):
        return {
            model.__name__: model.objects.count()
            for model in (
                Order,
                Payment,
                PaymentAttempt,
                PaymentTransaction,
                FinancialAllocation,
                JournalEntry,
                CommercialFinalization,
                DigitalInventoryReservation,
                DigitalFulfillmentItem,
                Entitlement,
            )
        }

    def test_authenticated_callback_persists_evidence_and_dormant_work_only(self):
        before = self.graph_counts()
        response = self.post()
        self.assertEqual(response.status_code, 202)
        receipt = CallbackReceipt.objects.get()
        event = ProviderEvent.objects.get()
        work = VerificationWorkItem.objects.get()
        self.assertEqual(receipt.processing_status, CallbackProcessingStatus.NORMALIZED)
        self.assertEqual(event.transaction_id, self.transaction_obj.pk)
        self.assertEqual(event.resolution_status, ProviderEventResolutionStatus.VERIFICATION_REQUIRED)
        self.assertEqual(work.work_type, VerificationWorkType.VERIFY_AFTER_CALLBACK)
        self.assertEqual(work.status, "pending")
        self.assertEqual(before, self.graph_counts())

    def test_exact_replay_recovers_after_window_expiry_without_new_rows(self):
        first = self.post()
        self.assertEqual(first.status_code, 202)
        expired = CallbackAdapter(replay_status=CallbackReplayWindowStatus.EXPIRED)
        replay = self.post(adapter=expired)
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.data["duplicate"])
        self.assertEqual(CallbackReceipt.objects.count(), 1)
        self.assertEqual(ProviderEvent.objects.count(), 1)
        self.assertEqual(VerificationWorkItem.objects.count(), 1)

    def test_first_seen_expired_callback_is_rejected_without_event_or_work(self):
        response = self.post(adapter=CallbackAdapter(replay_status=CallbackReplayWindowStatus.EXPIRED))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(CallbackReceipt.objects.get().processing_status, CallbackProcessingStatus.QUARANTINED)
        self.assertFalse(ProviderEvent.objects.exists())
        self.assertFalse(VerificationWorkItem.objects.exists())

    def test_forged_and_malformed_callbacks_fail_closed(self):
        body = self.body()
        with patch.object(ProviderCallbackIngestionApi, "adapter_registry", self.registry):
            forged = APIClient().post(
                self.url,
                data=body,
                content_type="application/json",
                HTTP_X_SIGNATURE="forged",
            )
        self.assertEqual(forged.status_code, 401)
        malformed = self.post(body=self.body(event_id="malformed"), adapter=CallbackAdapter(normalization_error=True))
        self.assertEqual(malformed.status_code, 400)
        self.assertFalse(ProviderEvent.objects.exists())
        self.assertFalse(VerificationWorkItem.objects.exists())

    def test_changed_bytes_create_distinct_contradiction_event_and_work(self):
        self.assertEqual(self.post().status_code, 202)
        response = self.post(body=self.body(amount=int(self.transaction_obj.provider_amount) + 1))
        self.assertEqual(response.status_code, 409)
        original = ProviderEvent.objects.get(resolution_status=ProviderEventResolutionStatus.VERIFICATION_REQUIRED)
        contradictory = ProviderEvent.objects.get(resolution_status=ProviderEventResolutionStatus.CONTRADICTORY)
        self.assertEqual(contradictory.original_event_id, original.pk)
        self.assertEqual(contradictory.transaction_id, self.transaction_obj.pk)
        self.assertEqual(VerificationWorkItem.objects.count(), 2)
        escalation = VerificationWorkItem.objects.get(work_type=VerificationWorkType.ESCALATE_UNKNOWN_OUTCOME)
        self.assertEqual(escalation.provider_event_id, contradictory.pk)
        self.assertIn(str(original.public_id), escalation.deterministic_identity)
        self.assertIn(str(contradictory.public_id), escalation.deterministic_identity)

    def test_exact_contradiction_replay_does_not_duplicate_evidence_or_work(self):
        self.post()
        changed = self.body(amount=int(self.transaction_obj.provider_amount) + 1)
        self.assertEqual(self.post(body=changed).status_code, 409)
        counts = (
            CallbackReceipt.objects.count(),
            ProviderEvent.objects.count(),
            VerificationWorkItem.objects.count(),
        )
        replay = self.post(body=changed)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(
            counts,
            (
                CallbackReceipt.objects.count(),
                ProviderEvent.objects.count(),
                VerificationWorkItem.objects.count(),
            ),
        )

    def test_key_version_change_is_not_exact_replay(self):
        self.post()
        changed_key = CallbackAdapter(key_version="callback-key-v2")
        response = self.post(adapter=changed_key)
        self.assertEqual(response.status_code, 409)
        self.assertTrue(
            ProviderEvent.objects.filter(
                resolution_status=ProviderEventResolutionStatus.CONTRADICTORY
            ).exists()
        )

    def test_unauthenticated_hint_requires_explicit_policy_and_exact_correlation(self):
        self.use_new_graph(callback_authentication=CallbackAuthenticationStrength.NONE)
        hint = CallbackAdapter(auth_status=CallbackAuthenticationStatus.UNAUTHENTICATED_HINT)
        accepted = self.post(adapter=hint)
        self.assertEqual(accepted.status_code, 202)
        self.assertEqual(ProviderEvent.objects.count(), 1)
        self.assertEqual(VerificationWorkItem.objects.count(), 1)

        wrong = self.post(
            body=self.body(event_id="untrusted-wrong", merchant_reference="not-backend-issued"),
            adapter=hint,
        )
        self.assertEqual(wrong.status_code, 202)
        self.assertEqual(ProviderEvent.objects.count(), 1)
        self.assertEqual(VerificationWorkItem.objects.count(), 1)
        self.assertEqual(
            CallbackReceipt.objects.order_by("pk").last().quarantine_reason,
            "unauthenticated_hint_not_permitted",
        )

    def test_unauthenticated_hint_is_rejected_when_policy_requires_signature(self):
        response = self.post(adapter=CallbackAdapter(auth_status=CallbackAuthenticationStatus.UNAUTHENTICATED_HINT))
        self.assertEqual(response.status_code, 202)
        self.assertFalse(ProviderEvent.objects.exists())
        self.assertFalse(VerificationWorkItem.objects.exists())

    def test_unknown_merchant_is_quarantined_without_work(self):
        response = self.post(body=self.body(merchant_reference="cg-" + "0" * 32))
        self.assertEqual(response.status_code, 202)
        event = ProviderEvent.objects.get()
        self.assertIsNone(event.transaction_id)
        self.assertEqual(event.resolution_status, ProviderEventResolutionStatus.QUARANTINED)
        self.assertFalse(VerificationWorkItem.objects.exists())

    def test_unknown_provider_or_callback_identity_is_ownership_safe(self):
        unknown_provider = self.url.replace(self.account.provider.key, "unknown-provider")
        self.assertEqual(self.post(url=unknown_provider).status_code, 404)
        unknown_transaction = self.url.replace(str(self.transaction_obj.public_id), str(uuid4()))
        self.assertEqual(self.post(url=unknown_transaction).status_code, 404)
        self.assertFalse(CallbackReceipt.objects.exists())

    def test_transport_content_type_and_body_limits_are_stable(self):
        unsupported = self.post(content_type="text/plain")
        self.assertEqual(unsupported.status_code, 415)
        oversized = self.post(body=b"x" * (64 * 1024 + 1))
        self.assertEqual(oversized.status_code, 413)
        self.assertFalse(ProviderEvent.objects.exists())
        self.assertFalse(VerificationWorkItem.objects.exists())

    def test_database_guard_rejects_invalid_contradiction_lineage(self):
        self.post()
        original = ProviderEvent.objects.get()
        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO financial_core_providerevent
                    (public_id, provider_id, capability_version_id, merchant_account_version_id,
                     transaction_id, original_event_id, adapter_contract_version, provider_event_id,
                     canonical_envelope_hash, merchant_reference, provider_authority, provider_reference,
                     operation_type_hint, provider_amount_hint, provider_unit_hint, normalized_hint,
                     provider_occurred_at, authentication_strength, deduplication_identity,
                     resolution_status, quarantine_reason, correlation_id, created_at)
                    SELECT %s, provider_id, capability_version_id, merchant_account_version_id,
                           transaction_id, NULL, adapter_contract_version, provider_event_id,
                           %s, merchant_reference, provider_authority, provider_reference,
                           operation_type_hint, provider_amount_hint, provider_unit_hint, normalized_hint,
                           provider_occurred_at, authentication_strength, %s,
                           'contradictory', 'forged', %s, NOW()
                    FROM financial_core_providerevent WHERE id = %s
                    """,
                    [str(uuid4()), "f" * 64, "e" * 64, str(uuid4()), original.pk],
                )

    def test_concurrent_duplicate_callbacks_create_one_event_and_work(self):
        barrier = Barrier(2)
        adapter = CallbackAdapter(barrier=barrier)
        outcomes = []
        outcome_lock = Lock()

        def runner():
            close_old_connections()
            try:
                response = self.post(adapter=adapter)
                with outcome_lock:
                    outcomes.append(response.status_code)
            finally:
                close_old_connections()

        threads = [Thread(target=runner) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(sorted(outcomes), [200, 202])
        self.assertEqual(ProviderEvent.objects.count(), 1)
        self.assertEqual(VerificationWorkItem.objects.count(), 1)

    def test_endpoint_query_count_is_bounded(self):
        with CaptureQueriesContext(connection) as captured:
            response = self.post()
        self.assertEqual(response.status_code, 202)
        self.assertLessEqual(len(captured), 36)

    def test_only_post_is_exposed(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 405)
