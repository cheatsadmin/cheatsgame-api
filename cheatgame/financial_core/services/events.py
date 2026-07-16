from decimal import Decimal

from django.core.exceptions import ValidationError

from cheatgame.financial_core.models import FinancialActorType, FinancialEvent


SAFE_METADATA_KEYS = frozenset(
    {
        "previous_status",
        "new_status",
        "reason_code",
        "operation_type",
        "provider",
        "currency",
        "amount",
        "sequence",
        "outcome",
        "finding_type",
        "severity",
    }
)
SENSITIVE_FRAGMENTS = (
    "password",
    "secret",
    "token",
    "card",
    "cookie",
    "authorization",
    "merchant_id",
    "api_key",
    "apikey",
)


def sanitize_financial_metadata(metadata):
    if not isinstance(metadata, dict):
        return {}
    clean = {}
    for key, value in metadata.items():
        normalized = str(key).lower()
        if key not in SAFE_METADATA_KEYS or any(fragment in normalized for fragment in SENSITIVE_FRAGMENTS):
            continue
        if value is None or isinstance(value, (bool, int, float)):
            clean[key] = value
        elif isinstance(value, (str, Decimal)):
            clean[key] = str(value)[:512]
    return clean


def append_financial_event(
    *,
    aggregate_type,
    aggregate_id,
    aggregate_version,
    event_type,
    actor_type=FinancialActorType.SYSTEM,
    actor_id=None,
    idempotency_key,
    correlation_id=None,
    causation_id=None,
    metadata=None,
):
    if actor_type not in FinancialActorType.values:
        raise ValidationError({"actor_type": "Unsupported financial actor type."})
    kwargs = {}
    if correlation_id is not None:
        kwargs["correlation_id"] = correlation_id
    return FinancialEvent.objects.create(
        aggregate_type=str(aggregate_type),
        aggregate_id=str(aggregate_id),
        aggregate_version=aggregate_version,
        event_type=str(event_type),
        actor_type=actor_type,
        actor_id=actor_id,
        idempotency_key=str(idempotency_key),
        causation_id=causation_id,
        metadata=sanitize_financial_metadata(metadata or {}),
        **kwargs,
    )
