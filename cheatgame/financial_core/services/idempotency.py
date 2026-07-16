import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from django.db import IntegrityError, transaction
from django.utils import timezone

from cheatgame.financial_core.models import IdempotencyRecord, IdempotencyStatus


class IdempotencyConflict(Exception):
    pass


class IdempotencyInProgress(Exception):
    pass


def _json_default(value):
    if isinstance(value, (Decimal, UUID, date, datetime)):
        return str(value)
    raise TypeError(f"Unsupported idempotency value: {type(value).__name__}")


def canonical_request_hash(payload):
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def begin_idempotent_command(*, scope, key, request_payload):
    request_hash = canonical_request_hash(request_payload)
    with transaction.atomic():
        record = IdempotencyRecord.objects.select_for_update().filter(scope=scope, key=str(key)).first()
        if record is None:
            try:
                with transaction.atomic():
                    record = IdempotencyRecord.objects.create(
                        scope=scope,
                        key=str(key),
                        request_hash=request_hash,
                    )
                return record, True
            except IntegrityError:
                record = IdempotencyRecord.objects.select_for_update().get(scope=scope, key=str(key))
        if record.request_hash != request_hash:
            raise IdempotencyConflict("Idempotency key was reused with a different request.")
        if record.status == IdempotencyStatus.IN_PROGRESS:
            raise IdempotencyInProgress("Idempotent command is already in progress.")
        return record, False


@transaction.atomic
def complete_idempotent_command(*, record_id, result_type, result_id, safe_response=None):
    record = IdempotencyRecord.objects.select_for_update().get(pk=record_id)
    if record.status == IdempotencyStatus.COMPLETED:
        return record
    if record.status == IdempotencyStatus.FAILED:
        raise IdempotencyConflict("A failed idempotency record cannot be rewritten as completed.")
    record.status = IdempotencyStatus.COMPLETED
    record.result_type = str(result_type)
    record.result_id = str(result_id)
    record.safe_response = safe_response or {}
    record.error_code = ""
    record.completed_at = timezone.now()
    record.save(
        update_fields=(
            "status",
            "result_type",
            "result_id",
            "safe_response",
            "error_code",
            "completed_at",
            "updated_at",
        )
    )
    return record


@transaction.atomic
def fail_idempotent_command(*, record_id, error_code):
    record = IdempotencyRecord.objects.select_for_update().get(pk=record_id)
    if record.status == IdempotencyStatus.COMPLETED:
        return record
    record.status = IdempotencyStatus.FAILED
    record.error_code = str(error_code)[:100]
    record.completed_at = timezone.now()
    record.save(update_fields=("status", "error_code", "completed_at", "updated_at"))
    return record
