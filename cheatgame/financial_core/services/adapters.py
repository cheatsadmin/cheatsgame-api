from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Protocol

from django.core.exceptions import ValidationError

from cheatgame.financial_core.models import ProviderRequestOutcome
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


PRODUCTION_ADAPTER_REGISTRY = ProviderAdapterRegistry()
