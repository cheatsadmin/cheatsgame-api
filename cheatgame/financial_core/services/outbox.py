from django.core.exceptions import ValidationError

from cheatgame.financial_core.models import FinancialOutboxMessage


SAFE_OUTBOX_KEYS = frozenset(
    {
        "event_type",
        "provider",
        "operation_type",
        "transaction_public_id",
        "payment_public_id",
        "new_status",
        "reason_code",
    }
)
SENSITIVE_FRAGMENTS = ("secret", "password", "credential", "token", "authorization", "cookie", "card")


def sanitize_outbox_payload(payload):
    if not isinstance(payload, dict):
        raise ValidationError("Outbox payload must be an object.")
    clean = {}
    for key, value in payload.items():
        normalized = str(key).lower()
        if key not in SAFE_OUTBOX_KEYS or any(fragment in normalized for fragment in SENSITIVE_FRAGMENTS):
            continue
        if value is None or isinstance(value, (bool, int, str)):
            clean[key] = value if not isinstance(value, str) else value[:256]
    return clean


def append_outbox_message(
    *, topic, aggregate_type, aggregate_id, idempotency_key, correlation_id, causation_id=None, payload=None
):
    return FinancialOutboxMessage.objects.create(
        topic=str(topic),
        aggregate_type=str(aggregate_type),
        aggregate_id=str(aggregate_id),
        idempotency_key=str(idempotency_key),
        correlation_id=correlation_id,
        causation_id=causation_id,
        safe_payload=sanitize_outbox_payload(payload or {}),
    )
