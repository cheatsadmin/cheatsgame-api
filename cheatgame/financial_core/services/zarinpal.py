import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs, urljoin, urlsplit

import requests
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.urls import reverse

from cheatgame.financial_core.models import (
    CallbackAuthenticationStatus,
    CallbackAuthenticationStrength,
    CallbackReplayWindowStatus,
    MoneyUnit,
    PaymentTransactionOperation,
    ProviderRequestOutcome,
    VerificationEvidenceBasis,
    VerificationFinality,
    VerificationFinancialEffect,
    VerificationOutcome,
    VerificationTransportClassification,
)
from cheatgame.financial_core.services.adapters import (
    ADAPTER_CONTRACT_VERSION,
    CallbackAuthenticationResult,
    NormalizedCallbackEvent,
    NormalizedProviderResult,
    NormalizedVerificationResult,
)


ZARINPAL_PROVIDER_KEY = "zarinpal"
ZARINPAL_ADAPTER_KEY = "zarinpal-v4"
ZARINPAL_CREDENTIAL_REFERENCE = "env://ZARINPAL_MERCHANT_ID"
ZARINPAL_PROVIDER_UNIT = MoneyUnit.IRR
ZARINPAL_CONVERSION_POLICY_VERSION = "irr-exact-v1"
ZARINPAL_REQUEST_ACCEPTED = 100
ZARINPAL_VERIFY_SUCCESS = frozenset((100, 101))
ZARINPAL_AUTHORITY = re.compile(r"^[AS][A-Za-z0-9]{35}$")


class ZarinpalConfigurationError(ImproperlyConfigured):
    pass


class ZarinpalProtocolError(ValidationError):
    pass


@dataclass(frozen=True)
class ZarinpalHttpResult:
    status_code: int
    payload: dict


def _canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _evidence_hash(value):
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _exact_positive_integer(value, *, field):
    if isinstance(value, (float, bool)):
        raise ZarinpalProtocolError({field: "Amount must be an exact integer."})
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ZarinpalProtocolError({field: "Amount is invalid."}) from exc
    if amount <= 0 or amount != amount.to_integral_value():
        raise ZarinpalProtocolError({field: "Amount must be a positive exact integer."})
    return int(amount)


def _require_https_url(value, *, setting_name, allow_template=False):
    if not isinstance(value, str) or not value:
        raise ZarinpalConfigurationError(f"{setting_name} is required.")
    candidate = value.replace("{authority}", "A" + ("0" * 35)) if allow_template else value
    parsed = urlsplit(candidate)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ZarinpalConfigurationError(f"{setting_name} must be an absolute HTTPS URL.")
    return value


class ZarinpalAdapter:
    adapter_key = ZARINPAL_ADAPTER_KEY
    contract_version = ADAPTER_CONTRACT_VERSION

    def __init__(
        self,
        *,
        merchant_id,
        sandbox,
        request_url,
        verify_url,
        startpay_url,
        callback_base_url,
        connect_timeout,
        read_timeout,
        transport=None,
    ):
        self.merchant_id = str(merchant_id).strip()
        self.sandbox = bool(sandbox)
        self.request_url = _require_https_url(request_url, setting_name="ZARINPAL_REQUEST_URL")
        self.verify_url = _require_https_url(verify_url, setting_name="ZARINPAL_VERIFY_URL")
        self.startpay_url = _require_https_url(
            startpay_url,
            setting_name="ZARINPAL_STARTPAY_URL",
            allow_template=True,
        )
        self.callback_base_url = _require_https_url(
            callback_base_url,
            setting_name="FINANCIAL_PROVIDER_CALLBACK_BASE_URL",
        )
        self.connect_timeout = float(connect_timeout)
        self.read_timeout = float(read_timeout)
        if self.connect_timeout <= 0 or self.read_timeout <= 0:
            raise ZarinpalConfigurationError("Zarinpal HTTP timeouts must be positive.")
        if not re.fullmatch(r"[0-9A-Za-z-]{36}", self.merchant_id):
            raise ZarinpalConfigurationError("ZARINPAL_MERCHANT_ID must be a 36-character identifier.")
        expected_host = "sandbox.zarinpal.com" if self.sandbox else "payment.zarinpal.com"
        configured_hosts = {
            urlsplit(self.request_url).hostname,
            urlsplit(self.verify_url).hostname,
            urlsplit(self.startpay_url.replace("{authority}", "A" + ("0" * 35))).hostname,
        }
        if configured_hosts != {expected_host}:
            raise ZarinpalConfigurationError("Zarinpal mode and endpoint hosts are inconsistent.")
        self.transport = transport or requests.Session()

    @classmethod
    def from_settings(cls, *, transport=None):
        return cls(
            merchant_id=getattr(settings, "ZARINPAL_MERCHANT_ID", ""),
            sandbox=getattr(settings, "ZARINPAL_SANDBOX", True),
            request_url=getattr(settings, "ZARINPAL_REQUEST_URL", ""),
            verify_url=getattr(settings, "ZARINPAL_VERIFY_URL", ""),
            startpay_url=getattr(settings, "ZARINPAL_STARTPAY_URL", ""),
            callback_base_url=getattr(settings, "FINANCIAL_PROVIDER_CALLBACK_BASE_URL", ""),
            connect_timeout=getattr(settings, "ZARINPAL_CONNECT_TIMEOUT_SECONDS", 3),
            read_timeout=getattr(settings, "ZARINPAL_READ_TIMEOUT_SECONDS", 10),
            transport=transport,
        )

    def _assert_envelope(self, envelope):
        if envelope.provider_key != ZARINPAL_PROVIDER_KEY:
            raise ZarinpalProtocolError("Zarinpal envelope has the wrong provider.")
        if envelope.adapter_key != self.adapter_key or envelope.adapter_contract_version != self.contract_version:
            raise ZarinpalProtocolError("Zarinpal adapter identity is inconsistent.")
        if envelope.credential_reference != ZARINPAL_CREDENTIAL_REFERENCE:
            raise ZarinpalConfigurationError("Zarinpal merchant credential reference is unsupported.")
        if envelope.operation_type != PaymentTransactionOperation.SALE:
            raise ZarinpalProtocolError("Zarinpal launch adapter supports sale operations only.")
        provider_unit = getattr(
            envelope,
            "provider_unit",
            getattr(envelope, "requested_provider_unit", ""),
        )
        provider_amount = getattr(
            envelope,
            "provider_amount",
            getattr(envelope, "requested_provider_amount", None),
        )
        if envelope.canonical_currency != MoneyUnit.IRR or provider_unit != MoneyUnit.IRR:
            raise ZarinpalProtocolError("Zarinpal launch adapter requires canonical IRR.")
        canonical = _exact_positive_integer(envelope.canonical_amount, field="canonical_amount")
        provider = _exact_positive_integer(provider_amount, field="provider_amount")
        if canonical != provider:
            raise ZarinpalProtocolError("Zarinpal IRR amount must equal the canonical amount.")
        return provider

    def _post_json(self, *, url, payload):
        try:
            response = self.transport.post(
                url,
                json=payload,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                timeout=(self.connect_timeout, self.read_timeout),
            )
        except (requests.ConnectTimeout, requests.ReadTimeout, requests.Timeout) as exc:
            raise TimeoutError("Zarinpal request timed out.") from exc
        except requests.RequestException as exc:
            raise ConnectionError("Zarinpal transport failed.") from exc
        try:
            decoded = response.json()
        except (TypeError, ValueError) as exc:
            raise ZarinpalProtocolError("Zarinpal returned malformed JSON.") from exc
        if not isinstance(decoded, dict):
            raise ZarinpalProtocolError("Zarinpal response must be a JSON object.")
        return ZarinpalHttpResult(status_code=int(response.status_code), payload=decoded)

    @staticmethod
    def _response_parts(result):
        payload = result.payload
        data = payload.get("data")
        errors = payload.get("errors")
        if data in (None, []):
            data = {}
        if errors in (None, []):
            errors = {}
        if not isinstance(data, dict) or not isinstance(errors, (dict, list)):
            raise ZarinpalProtocolError("Zarinpal response envelope is malformed.")
        code_value = data.get("code")
        if code_value is None and isinstance(errors, dict):
            code_value = errors.get("code")
        if code_value is None and isinstance(errors, list) and errors and isinstance(errors[0], dict):
            code_value = errors[0].get("code")
        if isinstance(code_value, bool):
            raise ZarinpalProtocolError("Zarinpal response code is malformed.")
        try:
            code = int(code_value)
        except (TypeError, ValueError) as exc:
            raise ZarinpalProtocolError("Zarinpal response code is missing.") from exc
        return data, errors, code

    def _callback_url(self, transaction_public_id):
        path = reverse(
            "api:financial-core:provider-callback-ingest",
            kwargs={
                "provider_key": ZARINPAL_PROVIDER_KEY,
                "transaction_id": transaction_public_id,
            },
        )
        return urljoin(self.callback_base_url.rstrip("/") + "/", path.lstrip("/"))

    def _startpay_url(self, authority):
        if "{authority}" in self.startpay_url:
            return self.startpay_url.format(authority=authority)
        return f"{self.startpay_url.rstrip('/')}/{authority}"

    @staticmethod
    def _request_result(envelope, *, outcome, reason, code, category, authority="", customer_url=""):
        return NormalizedProviderResult(
            outcome=outcome,
            evidence_hash=_evidence_hash(
                {
                    "transaction": envelope.transaction_public_id,
                    "request_fingerprint": envelope.request_fingerprint,
                    "provider_amount": envelope.provider_amount,
                    "provider_unit": envelope.provider_unit,
                    "code": code,
                    "authority": authority,
                    "category": category,
                }
            ),
            reason_code=reason,
            safe_metadata={"result_code": code, "result_category": category},
            customer_action_url=customer_url,
            provider_authority=authority,
        )

    def execute_operation(self, envelope):
        amount = self._assert_envelope(envelope)
        payload = {
            "merchant_id": self.merchant_id,
            "amount": amount,
            "currency": MoneyUnit.IRR,
            "callback_url": self._callback_url(envelope.transaction_public_id),
            "description": f"Cheats Game payment {envelope.merchant_reference[-16:]}",
            "metadata": {"order_id": envelope.merchant_reference},
        }
        try:
            result = self._post_json(url=self.request_url, payload=payload)
            data, _, code = self._response_parts(result)
        except ZarinpalProtocolError:
            return self._request_result(
                envelope,
                outcome=ProviderRequestOutcome.PROTOCOL_FAILURE,
                reason="zarinpal_malformed_response",
                code="malformed",
                category="protocol_failure",
            )
        if result.status_code >= 500:
            return self._request_result(
                envelope,
                outcome=ProviderRequestOutcome.OUTCOME_UNKNOWN,
                reason="zarinpal_http_5xx",
                code=code,
                category="transport_uncertain",
            )
        if code == ZARINPAL_REQUEST_ACCEPTED:
            authority = data.get("authority")
            if not isinstance(authority, str) or not ZARINPAL_AUTHORITY.fullmatch(authority):
                return self._request_result(
                    envelope,
                    outcome=ProviderRequestOutcome.PROTOCOL_FAILURE,
                    reason="zarinpal_invalid_authority",
                    code=code,
                    category="protocol_failure",
                )
            expected_prefix = "S" if self.sandbox else "A"
            if not authority.startswith(expected_prefix):
                return self._request_result(
                    envelope,
                    outcome=ProviderRequestOutcome.SECURITY_FAILURE,
                    reason="zarinpal_authority_mode_mismatch",
                    code=code,
                    category="security_failure",
                )
            return self._request_result(
                envelope,
                outcome=ProviderRequestOutcome.CUSTOMER_ACTION_REQUIRED,
                reason="",
                code=code,
                category="accepted",
                authority=authority,
                customer_url=self._startpay_url(authority),
            )
        if code == -12:
            outcome, category = ProviderRequestOutcome.NO_EFFECT_RETRYABLE, "rate_limited"
        elif code in (-9, -10, -11, -13, -14, -15, -16, -17, -18, -19, -41):
            outcome, category = ProviderRequestOutcome.CONFIGURATION_FAILURE, "configuration_failure"
        else:
            outcome, category = ProviderRequestOutcome.PROTOCOL_FAILURE, "provider_rejected"
        return self._request_result(
            envelope,
            outcome=outcome,
            reason=f"zarinpal_request_{code}",
            code=code,
            category=category,
        )

    def authenticate_callback(self, *, headers, body):
        del headers
        evidence = hashlib.sha256(body).hexdigest()
        try:
            parsed = parse_qs(body.decode("ascii"), keep_blank_values=True, strict_parsing=True, max_num_fields=4)
        except (UnicodeDecodeError, ValueError) as exc:
            raise ZarinpalProtocolError("Zarinpal callback query is malformed.") from exc
        if set(parsed) != {"Authority", "Status"} or any(len(values) != 1 for values in parsed.values()):
            raise ZarinpalProtocolError("Zarinpal callback parameters are malformed.")
        authority = parsed["Authority"][0]
        status = parsed["Status"][0].upper()
        if not ZARINPAL_AUTHORITY.fullmatch(authority) or status not in ("OK", "NOK"):
            raise ZarinpalProtocolError("Zarinpal callback claim is invalid.")
        return CallbackAuthenticationResult(
            status=CallbackAuthenticationStatus.UNAUTHENTICATED_HINT,
            strength=CallbackAuthenticationStrength.NONE,
            method="zarinpal_unsigned_query",
            version="v4",
            signing_key_reference="",
            replay_window_status=CallbackReplayWindowStatus.NOT_SUPPORTED,
            trustworthy_provider_event_id="",
            safe_reason_code="server_verification_required",
            evidence_hash=evidence,
            authenticated_context={"authority": authority, "status": status},
        )

    def normalize_callback(self, authenticated_callback):
        context = authenticated_callback.authenticated_context
        return NormalizedCallbackEvent(
            merchant_reference="",
            provider_authority=context["authority"],
            provider_reference="",
            operation_type_hint=PaymentTransactionOperation.SALE,
            provider_amount_hint=None,
            provider_unit_hint="",
            normalized_hint=(
                "customer_returned_success_claim"
                if context["status"] == "OK"
                else "customer_returned_nonsuccess_claim"
            ),
            financial_effect_hint=VerificationFinancialEffect.UNKNOWN,
            finality_hint=VerificationFinality.UNKNOWN,
        )

    def _verification_result(
        self,
        envelope,
        *,
        outcome,
        financial_effect,
        finality,
        code,
        provider_reference="",
        retryable=False,
        error_classification="",
        transport=VerificationTransportClassification.SUCCESS,
        evidence_basis=VerificationEvidenceBasis.NONE,
    ):
        return NormalizedVerificationResult(
            outcome=outcome,
            financial_effect=financial_effect,
            finality=finality,
            transport_classification=transport,
            provider_key=envelope.provider_key,
            adapter_contract_version=envelope.adapter_contract_version,
            merchant_account_key=envelope.merchant_account_key,
            merchant_account_version=envelope.merchant_account_version,
            merchant_reference=envelope.merchant_reference,
            provider_authority=envelope.provider_authority,
            provider_reference=provider_reference,
            operation_type=envelope.operation_type,
            observed_provider_amount=(
                envelope.requested_provider_amount
                if outcome == VerificationOutcome.CONFIRMED_SUCCESS
                else None
            ),
            observed_provider_unit=(
                envelope.requested_provider_unit
                if outcome == VerificationOutcome.CONFIRMED_SUCCESS
                else ""
            ),
            evidence_hash=_evidence_hash(
                {
                    "transaction": envelope.transaction_public_id,
                    "authority": envelope.provider_authority,
                    "requested_amount": envelope.requested_provider_amount,
                    "requested_unit": envelope.requested_provider_unit,
                    "code": code,
                    "provider_reference": provider_reference,
                    "outcome": outcome,
                }
            ),
            error_classification=error_classification,
            retryable=retryable,
            response_evidence_reference=f"zarinpal:v4:{code}",
            already_verified_fresh_query=code == 101,
            evidence_basis=evidence_basis,
        )

    def verify_operation(self, envelope):
        return self._verify_or_query(envelope)

    def query_operation(self, envelope):
        return self._verify_or_query(envelope)

    def _verify_or_query(self, envelope):
        amount = self._assert_envelope(envelope)
        authority = str(envelope.provider_authority)
        if not ZARINPAL_AUTHORITY.fullmatch(authority):
            return self._verification_result(
                envelope,
                outcome=VerificationOutcome.PROTOCOL_FAILURE,
                financial_effect=VerificationFinancialEffect.UNKNOWN,
                finality=VerificationFinality.UNKNOWN,
                code="invalid_local_authority",
                error_classification="zarinpal_invalid_local_authority",
            )
        payload = {
            "merchant_id": self.merchant_id,
            "amount": amount,
            "authority": authority,
        }
        try:
            result = self._post_json(url=self.verify_url, payload=payload)
            data, _, code = self._response_parts(result)
        except ZarinpalProtocolError:
            return self._verification_result(
                envelope,
                outcome=VerificationOutcome.PROTOCOL_FAILURE,
                financial_effect=VerificationFinancialEffect.UNKNOWN,
                finality=VerificationFinality.UNKNOWN,
                code="malformed",
                error_classification="zarinpal_malformed_response",
                transport=VerificationTransportClassification.PROTOCOL_FAILURE,
            )
        if result.status_code >= 500:
            return self._verification_result(
                envelope,
                outcome=VerificationOutcome.OUTCOME_UNKNOWN,
                financial_effect=VerificationFinancialEffect.UNKNOWN,
                finality=VerificationFinality.UNKNOWN,
                code=code,
                retryable=True,
                error_classification="zarinpal_http_5xx",
                transport=VerificationTransportClassification.NETWORK_FAILURE,
            )
        if code in ZARINPAL_VERIFY_SUCCESS:
            ref_id = data.get("ref_id")
            if isinstance(ref_id, bool) or not isinstance(ref_id, (int, str)) or not str(ref_id):
                return self._verification_result(
                    envelope,
                    outcome=VerificationOutcome.PROTOCOL_FAILURE,
                    financial_effect=VerificationFinancialEffect.UNKNOWN,
                    finality=VerificationFinality.UNKNOWN,
                    code=code,
                    error_classification="zarinpal_missing_ref_id",
                    transport=VerificationTransportClassification.PROTOCOL_FAILURE,
                )
            return self._verification_result(
                envelope,
                outcome=VerificationOutcome.CONFIRMED_SUCCESS,
                financial_effect=VerificationFinancialEffect.PAID,
                finality=VerificationFinality.FINAL,
                code=code,
                provider_reference=str(ref_id),
                evidence_basis=VerificationEvidenceBasis.SERVER_TO_SERVER,
            )
        mapping = {
            -50: (
                VerificationOutcome.MISMATCH,
                VerificationFinancialEffect.UNKNOWN,
                VerificationFinality.UNKNOWN,
                False,
                "zarinpal_amount_mismatch",
            ),
            -51: (
                VerificationOutcome.CONFIRMED_DECLINE,
                VerificationFinancialEffect.UNPAID,
                VerificationFinality.FINAL,
                False,
                "zarinpal_payment_not_successful",
            ),
            -53: (
                VerificationOutcome.SECURITY_FAILURE,
                VerificationFinancialEffect.UNKNOWN,
                VerificationFinality.UNKNOWN,
                False,
                "zarinpal_merchant_mismatch",
            ),
            -54: (
                VerificationOutcome.NOT_FOUND_FINAL,
                VerificationFinancialEffect.UNPAID,
                VerificationFinality.FINAL,
                False,
                "zarinpal_invalid_authority",
            ),
            -55: (
                VerificationOutcome.NOT_FOUND_FINAL,
                VerificationFinancialEffect.UNPAID,
                VerificationFinality.FINAL,
                False,
                "zarinpal_payment_not_found",
            ),
        }
        if code == -52 or code == -12:
            values = (
                VerificationOutcome.OUTCOME_UNKNOWN,
                VerificationFinancialEffect.UNKNOWN,
                VerificationFinality.UNKNOWN,
                True,
                f"zarinpal_verify_{code}",
            )
        else:
            values = mapping.get(
                code,
                (
                    VerificationOutcome.PROTOCOL_FAILURE,
                    VerificationFinancialEffect.UNKNOWN,
                    VerificationFinality.UNKNOWN,
                    False,
                    f"zarinpal_verify_{code}",
                ),
            )
        return self._verification_result(
            envelope,
            outcome=values[0],
            financial_effect=values[1],
            finality=values[2],
            code=code,
            retryable=values[3],
            error_classification=values[4],
        )

    def read_reconciliation_records(self, *, period_start, period_end):
        del period_start, period_end
        return ()
