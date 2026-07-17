import hashlib
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Protocol

from django.core.exceptions import ValidationError

from cheatgame.financial_core.models import (
    CallbackAuthenticationStrength,
    CallbackAuthenticationStatus,
    CallbackReplayWindowStatus,
    ProviderRequestOutcome,
    VerificationEvidenceBasis,
    VerificationOutcome,
    VerificationTransportClassification,
)
from cheatgame.financial_core.services.boundaries import assert_external_io_allowed


ADAPTER_CONTRACT_VERSION = "c2a-v1"


@dataclass(frozen=True)
class ProviderOperationEnvelope:
    transaction_public_id: str
    operation_type: str
    provider_key: str
    adapter_key: str
    adapter_contract_version: str
    merchant_account_key: str
    merchant_account_version: int
    credential_reference: str
    merchant_reference: str
    canonical_amount: str
    canonical_currency: str
    provider_amount: str
    provider_unit: str
    provider_idempotency_reference: str
    request_fingerprint: str
    claim_token: str
    correlation_id: str


@dataclass(frozen=True)
class NormalizedProviderResult:
    outcome: str
    evidence_hash: str
    reason_code: str = ""
    safe_metadata: Mapping[str, Any] = None


@dataclass(frozen=True)
class CallbackAuthenticationResult:
    status: str
    strength: str
    method: str
    version: str
    signing_key_reference: str
    replay_window_status: str
    trustworthy_provider_event_id: str
    safe_reason_code: str
    evidence_hash: str
    authenticated_context: Any = None


@dataclass(frozen=True)
class NormalizedCallbackEvent:
    merchant_reference: str
    provider_authority: str
    provider_reference: str
    operation_type_hint: str
    provider_amount_hint: Any
    provider_unit_hint: str
    normalized_hint: str
    provider_occurred_at: Any = None


@dataclass(frozen=True)
class VerificationEnvelope:
    transaction_public_id: str
    operation_type: str
    provider_key: str
    adapter_key: str
    adapter_contract_version: str
    merchant_account_key: str
    merchant_account_version: int
    credential_reference: str
    merchant_reference: str
    provider_authority: str
    provider_reference: str
    requested_provider_amount: str
    requested_provider_unit: str
    canonical_amount: str
    canonical_currency: str
    claim_token: str
    correlation_id: str


@dataclass(frozen=True)
class NormalizedVerificationResult:
    outcome: str
    financial_effect: str
    finality: str
    transport_classification: str
    provider_key: str
    adapter_contract_version: str
    merchant_account_key: str
    merchant_account_version: int
    merchant_reference: str
    provider_authority: str
    provider_reference: str
    operation_type: str
    observed_provider_amount: Any
    observed_provider_unit: str
    evidence_hash: str
    provider_occurred_at: Any = None
    error_classification: str = ""
    retryable: bool = False
    response_evidence_reference: str = ""
    already_verified_fresh_query: bool = False
    evidence_basis: str = VerificationEvidenceBasis.NONE


class ProviderAdapter(Protocol):
    adapter_key: str
    contract_version: str

    def execute_operation(self, envelope: ProviderOperationEnvelope) -> NormalizedProviderResult:
        ...

    def authenticate_callback(self, *, headers: Mapping[str, str], body: bytes) -> Any:
        ...

    def normalize_callback(self, authenticated_callback: Any) -> Any:
        ...

    def verify_operation(self, envelope: ProviderOperationEnvelope) -> Any:
        ...

    def query_operation(self, envelope: ProviderOperationEnvelope) -> Any:
        ...

    def read_reconciliation_records(self, *, period_start, period_end) -> Iterable[Any]:
        ...


class ProviderAdapterRegistry:
    def __init__(self, adapters=None):
        self._adapters = dict(adapters or {})

    def resolve(self, *, adapter_key, contract_version):
        adapter = self._adapters.get((str(adapter_key), str(contract_version)))
        if adapter is None:
            raise ValidationError("Provider adapter is not allowlisted for this contract version.")
        assert_adapter_conformance(adapter)
        return adapter


def assert_adapter_conformance(adapter):
    if getattr(adapter, "contract_version", None) != ADAPTER_CONTRACT_VERSION:
        raise ValidationError("Provider adapter contract version is unsupported.")
    if not getattr(adapter, "adapter_key", ""):
        raise ValidationError("Provider adapter key is required.")
    for method_name in (
        "execute_operation",
        "authenticate_callback",
        "normalize_callback",
        "verify_operation",
        "query_operation",
        "read_reconciliation_records",
    ):
        if not callable(getattr(adapter, method_name, None)):
            raise ValidationError(f"Provider adapter is missing {method_name}.")
    return True


def execute_adapter_outside_transaction(*, adapter, envelope):
    assert_external_io_allowed()
    assert_adapter_conformance(adapter)
    result = adapter.execute_operation(envelope)
    if result.outcome not in ProviderRequestOutcome.values:
        raise ValidationError("Provider adapter returned an unsupported normalized outcome.")
    return result


def authenticate_callback_outside_transaction(*, adapter, headers, body):
    assert_external_io_allowed()
    assert_adapter_conformance(adapter)
    result = adapter.authenticate_callback(headers=headers, body=body)
    if not isinstance(result, CallbackAuthenticationResult):
        raise ValidationError("Provider adapter returned an invalid callback authentication result.")
    if result.status not in CallbackAuthenticationStatus.values:
        raise ValidationError("Provider adapter returned an unsupported callback authentication status.")
    if result.replay_window_status not in CallbackReplayWindowStatus.values:
        raise ValidationError("Provider adapter returned an unsupported replay-window result.")
    if result.strength not in CallbackAuthenticationStrength.values:
        raise ValidationError("Provider adapter returned an unsupported authentication strength.")
    if not result.evidence_hash or len(str(result.evidence_hash)) != 64:
        raise ValidationError("Callback authentication requires a sanitized evidence hash.")
    return result


def normalize_callback_outside_transaction(*, adapter, authentication_result):
    assert_external_io_allowed()
    assert_adapter_conformance(adapter)
    result = adapter.normalize_callback(authentication_result)
    if not isinstance(result, NormalizedCallbackEvent):
        raise ValidationError("Provider adapter returned an invalid normalized callback event.")
    return result


def execute_verification_outside_transaction(*, adapter, envelope, use_query=False):
    assert_external_io_allowed()
    assert_adapter_conformance(adapter)
    try:
        result = adapter.query_operation(envelope) if use_query else adapter.verify_operation(envelope)
    except TimeoutError:
        return _unknown_verification_transport_result(
            envelope=envelope,
            transport=VerificationTransportClassification.TIMEOUT,
            error_classification="provider_timeout",
        )
    except Exception:
        return _unknown_verification_transport_result(
            envelope=envelope,
            transport=VerificationTransportClassification.NETWORK_FAILURE,
            error_classification="provider_transport_failure",
        )
    if not isinstance(result, NormalizedVerificationResult):
        raise ValidationError("Provider adapter returned an invalid normalized verification result.")
    if result.outcome not in VerificationOutcome.values:
        raise ValidationError("Provider adapter returned an unsupported verification outcome.")
    if result.evidence_basis not in VerificationEvidenceBasis.values:
        raise ValidationError("Provider adapter returned an unsupported verification evidence basis.")
    return result


def _unknown_verification_transport_result(*, envelope, transport, error_classification):
    evidence_hash = hashlib.sha256(
        (
            f"{envelope.transaction_public_id}:{envelope.claim_token}:"
            f"{transport}:{error_classification}"
        ).encode("utf-8")
    ).hexdigest()
    return NormalizedVerificationResult(
        outcome=VerificationOutcome.OUTCOME_UNKNOWN,
        financial_effect="unknown",
        finality="unknown",
        transport_classification=transport,
        provider_key=envelope.provider_key,
        adapter_contract_version=envelope.adapter_contract_version,
        merchant_account_key=envelope.merchant_account_key,
        merchant_account_version=envelope.merchant_account_version,
        merchant_reference=envelope.merchant_reference,
        provider_authority=envelope.provider_authority,
        provider_reference=envelope.provider_reference,
        operation_type=envelope.operation_type,
        observed_provider_amount=None,
        observed_provider_unit="",
        evidence_hash=evidence_hash,
        error_classification=error_classification,
        retryable=True,
    )


PRODUCTION_ADAPTER_REGISTRY = ProviderAdapterRegistry()
