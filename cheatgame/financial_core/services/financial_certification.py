import hashlib
import hmac
import json
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ValidationError

from cheatgame.financial_core.models import (
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
    NormalizedProviderResult,
    NormalizedVerificationResult,
)


FINANCIAL_CERTIFICATION_PROVIDER_KEY = "financial_certification"
FINANCIAL_CERTIFICATION_ADAPTER_KEY = "financial-certification"
FINANCIAL_CERTIFICATION_CREDENTIAL_REFERENCE = "env:FINANCIAL_CERTIFICATION_SECRET"
FINANCIAL_CERTIFICATION_CONVERSION_POLICY_VERSION = "irr-identity-v1"


def assert_financial_certification_runtime():
    if not getattr(settings, "FINANCIAL_CERTIFICATION_PROVIDER_ENABLED", False):
        raise ImproperlyConfigured("Financial Certification provider is disabled.")
    environment = str(getattr(settings, "CHEATSGAME_RUNTIME_ENVIRONMENT", "")).strip().lower()
    if environment not in {"staging", "test"}:
        raise ImproperlyConfigured("Financial Certification is restricted to staging/test runtimes.")
    secret = str(getattr(settings, "FINANCIAL_CERTIFICATION_SECRET", ""))
    if len(secret) < 32:
        raise ImproperlyConfigured("Financial Certification requires a high-entropy secret.")
    hosts = {
        str(host).strip().lower().rstrip(".")
        for host in getattr(settings, "FINANCIAL_CERTIFICATION_ALLOWED_HOSTS", ())
        if str(host).strip()
    }
    allowed_hosts = {
        str(host).strip().lower().rstrip(".")
        for host in getattr(settings, "ALLOWED_HOSTS", ())
    }
    if not hosts or not hosts.issubset(allowed_hosts):
        raise ImproperlyConfigured("Financial Certification hosts are not explicitly allowlisted.")
    if environment == "staging" and any("staging" not in host for host in hosts):
        raise ImproperlyConfigured("Financial Certification may only bind to staging hosts.")
    return secret


def _exact_positive_integer(value, *, field):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError(f"{field} must be a positive exact integer.") from exc
    if amount <= 0 or amount != amount.to_integral_value():
        raise ValidationError(f"{field} must be a positive exact integer.")
    return amount


def _identity(envelope):
    provider_amount = getattr(envelope, "provider_amount", None)
    provider_unit = getattr(envelope, "provider_unit", None)
    if provider_amount is None:
        provider_amount = envelope.requested_provider_amount
        provider_unit = envelope.requested_provider_unit
    canonical = _exact_positive_integer(envelope.canonical_amount, field="canonical amount")
    provider = _exact_positive_integer(provider_amount, field="provider amount")
    if canonical != provider:
        raise ValidationError("Certification provider amount must equal canonical IRR amount.")
    if envelope.canonical_currency != MoneyUnit.IRR or provider_unit != MoneyUnit.IRR:
        raise ValidationError("Certification supports canonical IRR only.")
    if envelope.operation_type != PaymentTransactionOperation.SALE:
        raise ValidationError("Certification supports sale operations only.")
    if envelope.provider_key != FINANCIAL_CERTIFICATION_PROVIDER_KEY:
        raise ValidationError("Certification provider identity is invalid.")
    if envelope.credential_reference != FINANCIAL_CERTIFICATION_CREDENTIAL_REFERENCE:
        raise ValidationError("Certification credential reference is invalid.")
    return {
        "transaction": str(envelope.transaction_public_id),
        "merchant_reference": str(envelope.merchant_reference),
        "account": str(envelope.merchant_account_key),
        "account_version": int(envelope.merchant_account_version),
        "operation": str(envelope.operation_type),
        "amount": str(canonical.quantize(Decimal("1"))),
        "currency": MoneyUnit.IRR,
    }


def _digest(secret, purpose, identity):
    payload = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), purpose.encode("ascii") + b"\0" + payload, hashlib.sha256).hexdigest()


def certification_identity_from_transaction(transaction_obj):
    account = transaction_obj.merchant_account_version
    if (
        transaction_obj.provider != FINANCIAL_CERTIFICATION_PROVIDER_KEY
        or transaction_obj.operation_type != PaymentTransactionOperation.SALE
        or account.credential_reference != FINANCIAL_CERTIFICATION_CREDENTIAL_REFERENCE
    ):
        raise ValidationError("Certification transaction provider policy is invalid.")
    canonical = _exact_positive_integer(transaction_obj.amount, field="canonical amount")
    provider = _exact_positive_integer(transaction_obj.provider_amount, field="provider amount")
    if canonical != provider:
        raise ValidationError("Certification provider amount must equal canonical IRR amount.")
    if transaction_obj.currency != MoneyUnit.IRR or transaction_obj.provider_unit != MoneyUnit.IRR:
        raise ValidationError("Certification supports canonical IRR only.")
    return {
        "transaction": str(transaction_obj.public_id),
        "merchant_reference": str(transaction_obj.merchant_reference),
        "account": str(account.account_key),
        "account_version": int(account.version),
        "operation": str(transaction_obj.operation_type),
        "amount": str(canonical.quantize(Decimal("1"))),
        "currency": MoneyUnit.IRR,
    }


def certification_authority(*, secret, identity):
    return "fcert_a_" + _digest(secret, "authority", identity)[:48]


def certification_reference(*, secret, identity):
    return "fcert_r_" + _digest(secret, "reference", identity)[:48]


class FinancialCertificationAdapter:
    adapter_key = FINANCIAL_CERTIFICATION_ADAPTER_KEY
    contract_version = ADAPTER_CONTRACT_VERSION

    def __init__(self, *, secret):
        self.secret = secret

    @classmethod
    def from_settings(cls):
        return cls(secret=assert_financial_certification_runtime())

    def execute_operation(self, envelope):
        identity = _identity(envelope)
        authority = certification_authority(secret=self.secret, identity=identity)
        evidence_hash = hashlib.sha256(
            (envelope.request_fingerprint + ":" + authority + ":pending-admin-certification").encode("utf-8")
        ).hexdigest()
        return NormalizedProviderResult(
            outcome=ProviderRequestOutcome.ACCEPTED_PENDING,
            evidence_hash=evidence_hash,
            reason_code="awaiting_admin_certification",
            safe_metadata={"result_category": "awaiting_admin_certification"},
            provider_authority=authority,
        )

    def _verification_result(self, envelope):
        identity = _identity(envelope)
        expected = certification_authority(secret=self.secret, identity=identity)
        if not hmac.compare_digest(str(envelope.provider_authority), expected):
            return NormalizedVerificationResult(
                outcome=VerificationOutcome.SECURITY_FAILURE,
                financial_effect=VerificationFinancialEffect.UNKNOWN,
                finality=VerificationFinality.UNKNOWN,
                transport_classification=VerificationTransportClassification.NOT_EXECUTED,
                provider_key=envelope.provider_key,
                adapter_contract_version=envelope.adapter_contract_version,
                merchant_account_key=envelope.merchant_account_key,
                merchant_account_version=envelope.merchant_account_version,
                merchant_reference=envelope.merchant_reference,
                provider_authority=envelope.provider_authority,
                provider_reference="",
                operation_type=envelope.operation_type,
                observed_provider_amount=None,
                observed_provider_unit="",
                evidence_hash=hashlib.sha256((envelope.transaction_public_id + ":invalid-authority").encode()).hexdigest(),
                error_classification="financial_certification_authority_mismatch",
                retryable=False,
                evidence_basis=VerificationEvidenceBasis.NONE,
            )
        reference = certification_reference(secret=self.secret, identity=identity)
        return NormalizedVerificationResult(
            outcome=VerificationOutcome.CONFIRMED_SUCCESS,
            financial_effect=VerificationFinancialEffect.PAID,
            finality=VerificationFinality.FINAL,
            transport_classification=VerificationTransportClassification.SUCCESS,
            provider_key=envelope.provider_key,
            adapter_contract_version=envelope.adapter_contract_version,
            merchant_account_key=envelope.merchant_account_key,
            merchant_account_version=envelope.merchant_account_version,
            merchant_reference=envelope.merchant_reference,
            provider_authority=envelope.provider_authority,
            provider_reference=reference,
            operation_type=envelope.operation_type,
            observed_provider_amount=Decimal(str(envelope.requested_provider_amount)),
            observed_provider_unit=envelope.requested_provider_unit,
            evidence_hash=hashlib.sha256((envelope.transaction_public_id + ":" + reference + ":verified").encode()).hexdigest(),
            response_evidence_reference="financial-certification:v1",
            evidence_basis=VerificationEvidenceBasis.SERVER_TO_SERVER,
        )

    def verify_operation(self, envelope):
        return self._verification_result(envelope)

    def query_operation(self, envelope):
        return self._verification_result(envelope)

    def authenticate_callback(self, *, headers, body):
        del headers, body
        raise ValidationError("Financial Certification has no callback boundary.")

    def normalize_callback(self, authenticated_callback):
        del authenticated_callback
        raise ValidationError("Financial Certification has no callback boundary.")

    def read_reconciliation_records(self, *, period_start, period_end):
        del period_start, period_end
        return ()
