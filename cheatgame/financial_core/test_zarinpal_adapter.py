from decimal import Decimal
from io import StringIO
from unittest.mock import patch
from uuid import uuid4

import requests
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from rest_framework.test import APIClient

from cheatgame.financial_core.callback_apis import ProviderCallbackIngestionApi
from cheatgame.financial_core.models import (
    CallbackAuthenticationStrength,
    CallbackAuthenticationStatus,
    CallbackProcessingStatus,
    MerchantAccountVersion,
    MoneyUnit,
    PaymentTransactionOperation,
    ProviderCapabilityVersion,
    ProviderEvent,
    ProviderEventResolutionStatus,
    ProviderDefinition,
    ProviderRequestOutcome,
    VerificationEvidenceBasis,
    VerificationFinancialEffect,
    VerificationOutcome,
    VerificationWorkItem,
    VerificationWorkType,
)
from cheatgame.financial_core.services.adapters import (
    ADAPTER_CONTRACT_VERSION,
    ImmutableProviderRequestEnvelope,
    ProviderAdapterRegistry,
    PRODUCTION_ADAPTER_REGISTRY,
    VerificationEnvelope,
    build_production_adapter_registry,
)
from cheatgame.financial_core.services.money import normalize_obligation_money
from cheatgame.financial_core.services.provider_requests import (
    apply_provider_request_result,
    claim_provider_request,
)
from cheatgame.financial_core.services.zarinpal import (
    ZARINPAL_ADAPTER_KEY,
    ZARINPAL_CREDENTIAL_REFERENCE,
    ZarinpalAdapter,
)
from cheatgame.financial_core.test_c2b1 import C2B1Fixture


MERCHANT = "00000000-0000-0000-0000-000000000000"
SANDBOX_AUTHORITY = "S" + ("1" * 35)
PRODUCTION_AUTHORITY = "A" + ("1" * 35)


class FakeResponse:
    def __init__(self, payload, status_code=200, *, json_error=False):
        self.payload = payload
        self.status_code = status_code
        self.json_error = json_error

    def json(self):
        if self.json_error:
            raise ValueError("invalid json")
        return self.payload


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def adapter(*responses, sandbox=True):
    host = "sandbox.zarinpal.com" if sandbox else "payment.zarinpal.com"
    return ZarinpalAdapter(
        merchant_id=MERCHANT,
        sandbox=sandbox,
        request_url=f"https://{host}/pg/v4/payment/request.json",
        verify_url=f"https://{host}/pg/v4/payment/verify.json",
        startpay_url=f"https://{host}/pg/StartPay/{{authority}}",
        callback_base_url="https://backend.example",
        connect_timeout=2,
        read_timeout=7,
        transport=FakeTransport(*responses),
    )


def request_envelope(**overrides):
    values = {
        "transaction_public_id": str(uuid4()),
        "operation_type": "sale",
        "provider_key": "zarinpal",
        "adapter_key": ZARINPAL_ADAPTER_KEY,
        "adapter_contract_version": ADAPTER_CONTRACT_VERSION,
        "provider_capability_version": 1,
        "merchant_account_key": "zarinpal-launch",
        "merchant_account_version": 1,
        "credential_reference": ZARINPAL_CREDENTIAL_REFERENCE,
        "merchant_reference": "cg-" + ("1" * 32),
        "provider_reference": "",
        "canonical_amount": "510000",
        "canonical_currency": MoneyUnit.IRR,
        "provider_amount": "510000",
        "provider_unit": MoneyUnit.IRR,
        "provider_idempotency_reference": "",
        "request_fingerprint": "f" * 64,
        "claim_token": str(uuid4()),
        "callback_identity": "financial-payment:test",
        "correlation_id": str(uuid4()),
    }
    values.update(overrides)
    return ImmutableProviderRequestEnvelope(**values)


def verification_envelope(**overrides):
    request = request_envelope()
    values = {
        "transaction_public_id": request.transaction_public_id,
        "operation_type": request.operation_type,
        "provider_key": request.provider_key,
        "adapter_key": request.adapter_key,
        "adapter_contract_version": request.adapter_contract_version,
        "merchant_account_key": request.merchant_account_key,
        "merchant_account_version": request.merchant_account_version,
        "credential_reference": request.credential_reference,
        "merchant_reference": request.merchant_reference,
        "provider_authority": SANDBOX_AUTHORITY,
        "provider_reference": "",
        "requested_provider_amount": request.provider_amount,
        "requested_provider_unit": request.provider_unit,
        "canonical_amount": request.canonical_amount,
        "canonical_currency": request.canonical_currency,
        "claim_token": str(uuid4()),
        "correlation_id": request.correlation_id,
    }
    values.update(overrides)
    return VerificationEnvelope(**values)


class ZarinpalAdapterTests(SimpleTestCase):
    def test_request_uses_exact_irr_and_returns_canonical_handoff(self):
        transport = FakeTransport(
            FakeResponse(
                {
                    "data": {
                        "code": 100,
                        "message": "Success",
                        "authority": SANDBOX_AUTHORITY,
                    },
                    "errors": [],
                }
            )
        )
        instance = adapter()
        instance.transport = transport
        envelope = request_envelope()
        result = instance.execute_operation(envelope)
        self.assertEqual(result.outcome, ProviderRequestOutcome.CUSTOMER_ACTION_REQUIRED)
        self.assertEqual(result.provider_authority, SANDBOX_AUTHORITY)
        self.assertEqual(
            result.customer_action_url,
            f"https://sandbox.zarinpal.com/pg/StartPay/{SANDBOX_AUTHORITY}",
        )
        url, call = transport.calls[0]
        self.assertEqual(url, "https://sandbox.zarinpal.com/pg/v4/payment/request.json")
        self.assertEqual(call["json"]["amount"], 510000)
        self.assertEqual(call["json"]["currency"], MoneyUnit.IRR)
        self.assertNotIn(MERCHANT, result.evidence_hash)
        self.assertEqual(call["timeout"], (2.0, 7.0))

    def test_request_amount_is_exact_and_never_double_converted(self):
        instance = adapter(FakeResponse({"data": {"code": -9}, "errors": {}}))
        with self.assertRaisesMessage(Exception, "positive exact integer"):
            instance.execute_operation(request_envelope(canonical_amount="0", provider_amount="0"))
        with self.assertRaisesMessage(Exception, "must equal"):
            instance.execute_operation(request_envelope(provider_amount="51000"))
        bridge = normalize_obligation_money(
            source_amount=Decimal("51000"),
            source_unit=MoneyUnit.IRT,
            source_model="digital_offer",
            source_object_id="41",
            source_field="price",
        )
        self.assertEqual(bridge.canonical_amount, Decimal("510000"))

    def test_request_rejection_timeout_and_malformed_response_fail_safely(self):
        rejected = adapter(
            FakeResponse({"data": {}, "errors": {"code": -10, "message": "invalid"}})
        ).execute_operation(request_envelope())
        self.assertEqual(rejected.outcome, ProviderRequestOutcome.CONFIGURATION_FAILURE)
        malformed = adapter(FakeResponse({}, json_error=True)).execute_operation(request_envelope())
        self.assertEqual(malformed.outcome, ProviderRequestOutcome.PROTOCOL_FAILURE)
        with self.assertRaises(TimeoutError):
            adapter(requests.ReadTimeout("secret detail")).execute_operation(request_envelope())

    def test_authority_mode_mismatch_fails_as_security_result(self):
        result = adapter(
            FakeResponse(
                {"data": {"code": 100, "authority": PRODUCTION_AUTHORITY}, "errors": []}
            )
        ).execute_operation(request_envelope())
        self.assertEqual(result.outcome, ProviderRequestOutcome.SECURITY_FAILURE)
        self.assertFalse(result.customer_action_url)

    def test_production_mode_uses_only_production_hosts(self):
        instance = adapter(
            FakeResponse(
                {"data": {"code": 100, "authority": PRODUCTION_AUTHORITY}, "errors": []}
            ),
            sandbox=False,
        )
        result = instance.execute_operation(request_envelope())
        self.assertEqual(result.outcome, ProviderRequestOutcome.CUSTOMER_ACTION_REQUIRED)
        self.assertEqual(
            instance.transport.calls[0][0],
            "https://payment.zarinpal.com/pg/v4/payment/request.json",
        )
        self.assertEqual(
            result.customer_action_url,
            f"https://payment.zarinpal.com/pg/StartPay/{PRODUCTION_AUTHORITY}",
        )

    def test_callback_is_unsigned_hint_and_never_payment_truth(self):
        instance = adapter()
        body = f"Authority={SANDBOX_AUTHORITY}&Status=OK".encode("ascii")
        authenticated = instance.authenticate_callback(headers={}, body=body)
        self.assertEqual(
            authenticated.status,
            CallbackAuthenticationStatus.UNAUTHENTICATED_HINT,
        )
        normalized = instance.normalize_callback(authenticated)
        self.assertEqual(normalized.provider_authority, SANDBOX_AUTHORITY)
        self.assertEqual(normalized.financial_effect_hint, VerificationFinancialEffect.UNKNOWN)
        with self.assertRaises(Exception):
            instance.authenticate_callback(
                headers={},
                body=f"Authority={SANDBOX_AUTHORITY}&Status=YES".encode("ascii"),
            )

    def test_verification_success_and_already_verified_are_server_truth(self):
        for code in (100, 101):
            instance = adapter(
                FakeResponse(
                    {
                        "data": {"code": code, "message": "Verified", "ref_id": 123456},
                        "errors": [],
                    }
                )
            )
            result = instance.verify_operation(verification_envelope())
            self.assertEqual(result.outcome, VerificationOutcome.CONFIRMED_SUCCESS)
            self.assertEqual(result.provider_reference, "123456")
            self.assertEqual(result.observed_provider_amount, "510000")
            self.assertEqual(result.observed_provider_unit, MoneyUnit.IRR)
            self.assertEqual(result.evidence_basis, VerificationEvidenceBasis.SERVER_TO_SERVER)
            self.assertEqual(result.already_verified_fresh_query, code == 101)

    def test_verification_code_mapping_is_exhaustive_and_fail_closed(self):
        cases = {
            -50: VerificationOutcome.MISMATCH,
            -51: VerificationOutcome.CONFIRMED_DECLINE,
            -52: VerificationOutcome.OUTCOME_UNKNOWN,
            -53: VerificationOutcome.SECURITY_FAILURE,
            -54: VerificationOutcome.NOT_FOUND_FINAL,
            -55: VerificationOutcome.NOT_FOUND_FINAL,
            -999: VerificationOutcome.PROTOCOL_FAILURE,
        }
        for code, expected in cases.items():
            result = adapter(
                FakeResponse({"data": {}, "errors": {"code": code, "message": "safe"}})
            ).verify_operation(verification_envelope())
            self.assertEqual(result.outcome, expected)
            if code == -52:
                self.assertTrue(result.retryable)

    def test_verification_http_and_malformed_results_are_not_false_failure(self):
        unavailable = adapter(
            FakeResponse({"data": {}, "errors": {"code": -52}}, status_code=503)
        ).verify_operation(verification_envelope())
        self.assertEqual(unavailable.outcome, VerificationOutcome.OUTCOME_UNKNOWN)
        self.assertTrue(unavailable.retryable)
        malformed = adapter(FakeResponse({}, json_error=True)).verify_operation(
            verification_envelope()
        )
        self.assertEqual(malformed.outcome, VerificationOutcome.PROTOCOL_FAILURE)

    @override_settings(
        FINANCIAL_ZARINPAL_ENABLED=True,
        ZARINPAL_MERCHANT_ID=MERCHANT,
        ZARINPAL_SANDBOX=True,
        ZARINPAL_REQUEST_URL="https://sandbox.zarinpal.com/pg/v4/payment/request.json",
        ZARINPAL_VERIFY_URL="https://sandbox.zarinpal.com/pg/v4/payment/verify.json",
        ZARINPAL_STARTPAY_URL="https://sandbox.zarinpal.com/pg/StartPay/{authority}",
        FINANCIAL_PROVIDER_CALLBACK_BASE_URL="https://backend.example",
    )
    def test_registry_activation_is_explicit(self):
        registry = build_production_adapter_registry()
        resolved = registry.resolve(
            adapter_key=ZARINPAL_ADAPTER_KEY,
            contract_version=ADAPTER_CONTRACT_VERSION,
        )
        self.assertIsInstance(resolved, ZarinpalAdapter)

    @override_settings(
        FINANCIAL_ZARINPAL_ENABLED=True,
        ZARINPAL_MERCHANT_ID=MERCHANT,
        ZARINPAL_SANDBOX=True,
        ZARINPAL_REQUEST_URL="https://sandbox.zarinpal.com/pg/v4/payment/request.json",
        ZARINPAL_VERIFY_URL="https://sandbox.zarinpal.com/pg/v4/payment/verify.json",
        ZARINPAL_STARTPAY_URL="https://sandbox.zarinpal.com/pg/StartPay/{authority}",
        FINANCIAL_PROVIDER_CALLBACK_BASE_URL="https://backend.example",
    )
    def test_production_registry_resolves_after_adapter_contract_import(self):
        resolved = PRODUCTION_ADAPTER_REGISTRY.resolve(
            adapter_key=ZARINPAL_ADAPTER_KEY,
            contract_version=ADAPTER_CONTRACT_VERSION,
        )
        self.assertIsInstance(resolved, ZarinpalAdapter)

    @override_settings(
        FINANCIAL_ZARINPAL_ENABLED=True,
        ZARINPAL_MERCHANT_ID="",
    )
    def test_missing_merchant_configuration_fails_closed(self):
        with self.assertRaises(ImproperlyConfigured):
            build_production_adapter_registry()


class ZarinpalFinancialBoundaryTests(C2B1Fixture, TransactionTestCase):
    reset_sequences = True

    def make_account(self, *, unit=MoneyUnit.IRR, enabled=True, version=1):
        provider = ProviderDefinition.objects.create(
            key="zarinpal",
            display_name="Zarinpal",
            is_enabled=enabled,
            new_requests_enabled=enabled,
        )
        capability = ProviderCapabilityVersion.objects.create(
            provider=provider,
            version=version,
            adapter_key=ZARINPAL_ADAPTER_KEY,
            adapter_contract_version=ADAPTER_CONTRACT_VERSION,
            provider_unit=unit,
            conversion_policy_version="irr-exact-v1",
            supported_operations=[PaymentTransactionOperation.SALE],
            supports_request_idempotency=True,
            supports_lookup=True,
            callback_authentication=CallbackAuthenticationStrength.NONE,
            finality_window_seconds=86400,
            authority_expiry_seconds=1800,
        )
        account = MerchantAccountVersion.objects.create(
            provider=provider,
            capability_version=capability,
            account_key="zarinpal-launch",
            version=version,
            owner_key="cheats-game",
            credential_reference=ZARINPAL_CREDENTIAL_REFERENCE,
            is_enabled=enabled,
            new_requests_enabled=enabled,
        )
        return provider, capability, account

    def test_provider_authority_is_persisted_write_once_at_request_boundary(self):
        _, _, _, transaction_obj = self.make_request_graph()
        claim = claim_provider_request(
            transaction_id=transaction_obj.pk,
            claim_idempotency_key=uuid4(),
        )
        apply_provider_request_result(
            transaction_id=transaction_obj.pk,
            claim_token=claim.claim.claim_token,
            outcome=ProviderRequestOutcome.CUSTOMER_ACTION_REQUIRED,
            evidence_hash="a" * 64,
            result_idempotency_key=uuid4(),
            provider_authority=SANDBOX_AUTHORITY,
        )
        transaction_obj.refresh_from_db()
        self.assertEqual(transaction_obj.provider_authority, SANDBOX_AUTHORITY)

    @override_settings(
        DIGITAL_PAYMENT_CUSTOMER_RETURN_BASE_URL="https://storefront.example/Profile/DigitalPayment"
    )
    def test_unsigned_get_callback_binds_exact_authority_and_only_enqueues_verification(self):
        _, account, _, transaction_obj = self.make_pending_graph()
        transaction_obj.provider_authority = SANDBOX_AUTHORITY
        transaction_obj.save(update_fields=("provider_authority", "updated_at"))
        registry = ProviderAdapterRegistry(
            {(ZARINPAL_ADAPTER_KEY, ADAPTER_CONTRACT_VERSION): adapter()}
        )
        url = (
            f"/api/financial-core/providers/{account.provider.key}/callbacks/"
            f"{transaction_obj.public_id}/"
        )
        with patch.object(ProviderCallbackIngestionApi, "adapter_registry", registry):
            response = APIClient().get(
                url,
                {"Authority": SANDBOX_AUTHORITY, "Status": "OK"},
            )
        self.assertEqual(response.status_code, 303)
        self.assertIn(
            str(transaction_obj.attempt.payment.order.checkout.public_id),
            response["Location"],
        )
        event = ProviderEvent.objects.get()
        self.assertEqual(event.transaction_id, transaction_obj.pk)
        self.assertEqual(
            event.resolution_status,
            ProviderEventResolutionStatus.VERIFICATION_REQUIRED,
        )
        self.assertEqual(
            event.receipt_links.get().callback_receipt.processing_status,
            CallbackProcessingStatus.NORMALIZED,
        )
        work = VerificationWorkItem.objects.get()
        self.assertEqual(work.work_type, VerificationWorkType.VERIFY_AFTER_CALLBACK)
        transaction_obj.refresh_from_db()
        self.assertNotEqual(transaction_obj.status, "succeeded")

    def test_foreign_authority_callback_is_quarantined(self):
        _, account, _, transaction_obj = self.make_pending_graph()
        transaction_obj.provider_authority = SANDBOX_AUTHORITY
        transaction_obj.save(update_fields=("provider_authority", "updated_at"))
        registry = ProviderAdapterRegistry(
            {(ZARINPAL_ADAPTER_KEY, ADAPTER_CONTRACT_VERSION): adapter()}
        )
        url = (
            f"/api/financial-core/providers/{account.provider.key}/callbacks/"
            f"{transaction_obj.public_id}/"
        )
        with patch.object(ProviderCallbackIngestionApi, "adapter_registry", registry):
            response = APIClient().get(
                url,
                {"Authority": "S" + ("2" * 35), "Status": "OK"},
            )
        self.assertEqual(response.status_code, 202)
        self.assertFalse(ProviderEvent.objects.exists())
        self.assertFalse(VerificationWorkItem.objects.exists())

    @override_settings(
        FINANCIAL_ZARINPAL_ENABLED=True,
        ZARINPAL_MERCHANT_ID=MERCHANT,
        ZARINPAL_SANDBOX=True,
        ZARINPAL_REQUEST_URL="https://sandbox.zarinpal.com/pg/v4/payment/request.json",
        ZARINPAL_VERIFY_URL="https://sandbox.zarinpal.com/pg/v4/payment/verify.json",
        ZARINPAL_STARTPAY_URL="https://sandbox.zarinpal.com/pg/StartPay/{authority}",
        FINANCIAL_PROVIDER_CALLBACK_BASE_URL="https://backend.example",
        FINANCIAL_ZARINPAL_ACCOUNT_KEY="zarinpal-launch",
        FINANCIAL_ZARINPAL_OWNER_KEY="cheats-game",
        FINANCIAL_ZARINPAL_AUTHORITY_EXPIRY_SECONDS=1800,
        FINANCIAL_ZARINPAL_FINALITY_WINDOW_SECONDS=86400,
    )
    def test_configuration_command_is_explicit_and_idempotent(self):
        inspection = StringIO()
        call_command("configure_zarinpal", stdout=inspection)
        self.assertIn("provider=missing", inspection.getvalue())
        self.assertFalse(ProviderDefinition.objects.filter(key="zarinpal").exists())

        call_command("configure_zarinpal", "--apply", stdout=StringIO())
        call_command("configure_zarinpal", "--apply", stdout=StringIO())
        provider = ProviderDefinition.objects.get(key="zarinpal")
        account = MerchantAccountVersion.objects.get(
            provider=provider,
            account_key="zarinpal-launch",
            version=1,
        )
        self.assertTrue(provider.is_enabled)
        self.assertTrue(provider.new_requests_enabled)
        self.assertTrue(account.is_enabled)
        self.assertTrue(account.new_requests_enabled)
        self.assertEqual(
            account.credential_reference,
            ZARINPAL_CREDENTIAL_REFERENCE,
        )
