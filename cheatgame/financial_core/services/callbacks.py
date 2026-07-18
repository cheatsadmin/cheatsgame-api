import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.utils import timezone

from cheatgame.financial_core.models import (
    CallbackAuthenticationStatus,
    CallbackAuthenticationStrength,
    CallbackProcessingStatus,
    CallbackReceipt,
    CallbackReplayWindowStatus,
    MerchantAccountVersion,
    MoneyUnit,
    PaymentTransaction,
    ProviderDefinition,
    ProviderEvent,
    ProviderEventReceipt,
    ProviderEventResolutionStatus,
    VerificationWorkType,
)
from cheatgame.financial_core.services.adapters import (
    authenticate_callback_outside_transaction,
    normalize_callback_outside_transaction,
)
from cheatgame.financial_core.services.idempotency import IdempotencyConflict
from cheatgame.financial_core.services.money import exact_integer_money
from cheatgame.financial_core.services.verification import enqueue_verification_work


MAX_CALLBACK_BODY_BYTES = 64 * 1024
MAX_CALLBACK_HEADER_COUNT = 32
MAX_CALLBACK_HEADER_BYTES = 8 * 1024
MAX_SINGLE_HEADER_BYTES = 1024
CALLBACK_RETENTION_DAYS = 90
ALLOWED_CONTENT_TYPES = frozenset(("application/json", "application/x-www-form-urlencoded"))
SAFE_HEADER_NAMES = frozenset(("content-type", "user-agent", "x-request-id", "x-signature-version"))
BACKEND_MERCHANT_REFERENCE = re.compile(r"^cg-[0-9a-f]{32}$")


@dataclass(frozen=True)
class CallbackIngestionResult:
    receipt: CallbackReceipt
    provider_event: ProviderEvent = None
    verification_work_id: int = None
    replayed: bool = False


def _sha_bytes(value):
    return hashlib.sha256(value).hexdigest()


def _protected_hash(value):
    return hmac.new(
        settings.SECRET_KEY.encode("utf-8"),
        str(value).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _safe_header_evidence(headers):
    evidence = {}
    for key, value in headers.items():
        normalized = str(key).lower()
        if normalized in SAFE_HEADER_NAMES:
            evidence[normalized] = _protected_hash(str(value))
    return evidence


def callback_transport_rejection(*, method, content_type, body, headers):
    if str(method).upper() != "POST":
        return "method_not_allowed"
    normalized_content_type = str(content_type).split(";", 1)[0].strip().lower()
    if normalized_content_type not in ALLOWED_CONTENT_TYPES:
        return "unsupported_content_type"
    if len(body) > MAX_CALLBACK_BODY_BYTES:
        return "body_too_large"
    if len(headers) > MAX_CALLBACK_HEADER_COUNT:
        return "header_count_exceeded"
    total = 0
    for key, value in headers.items():
        size = len(str(key).encode("utf-8")) + len(str(value).encode("utf-8"))
        if size > MAX_SINGLE_HEADER_BYTES:
            return "single_header_too_large"
        total += size
    if total > MAX_CALLBACK_HEADER_BYTES:
        return "headers_too_large"
    return ""


def _deduplication_identity(*, account_id, provider_event_id, raw_hash, authenticated):
    trusted_identity = provider_event_id if authenticated and provider_event_id else raw_hash
    return _sha_bytes(f"{account_id}:{trusted_identity}".encode("utf-8"))


def _normalized_fingerprint(normalized):
    payload = {
        "merchant_reference": str(normalized.merchant_reference),
        "provider_authority": str(normalized.provider_authority),
        "provider_reference": str(normalized.provider_reference),
        "operation_type_hint": str(normalized.operation_type_hint),
        "provider_amount_hint": (
            str(normalized.provider_amount_hint)
            if normalized.provider_amount_hint is not None
            else None
        ),
        "provider_unit_hint": str(normalized.provider_unit_hint).upper(),
        "normalized_hint": str(normalized.normalized_hint),
        "provider_occurred_at": (
            normalized.provider_occurred_at.isoformat()
            if normalized.provider_occurred_at is not None
            else None
        ),
    }
    return _sha_bytes(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _event_normalized_fingerprint(event):
    return _normalized_fingerprint(
        SimpleNamespace(
            merchant_reference=event.merchant_reference,
            provider_authority=event.provider_authority,
            provider_reference=event.provider_reference,
            operation_type_hint=event.operation_type_hint,
            provider_amount_hint=event.provider_amount_hint,
            provider_unit_hint=event.provider_unit_hint,
            normalized_hint=event.normalized_hint,
            provider_occurred_at=event.provider_occurred_at,
        )
    )


def _known_exact_replay(
    *, provider, capability, account, auth, raw_hash, callback_transaction_public_id
):
    """Recover only authenticated evidence under the exact immutable callback context."""
    if (
        not callback_transaction_public_id
        or auth.status != CallbackAuthenticationStatus.AUTHENTICATED
        or not auth.trustworthy_provider_event_id
    ):
        return None
    signing_key_hash = _protected_hash(auth.signing_key_reference) if auth.signing_key_reference else ""
    receipt = (
        CallbackReceipt.objects.select_related("event_link__provider_event")
        .filter(
            provider=provider,
            capability_version=capability,
            merchant_account_version=account,
            authentication_status=CallbackAuthenticationStatus.AUTHENTICATED,
            authentication_version=str(auth.version)[:32],
            signing_key_reference_hash=signing_key_hash,
            raw_envelope_hash=raw_hash,
            event_link__provider_event__provider_event_id=str(auth.trustworthy_provider_event_id)[:128],
            event_link__provider_event__transaction__public_id=callback_transaction_public_id,
        )
        .order_by("pk")
        .first()
    )
    if receipt is None:
        return None
    return CallbackIngestionResult(receipt, receipt.event_link.provider_event, replayed=True)


def _callback_transaction(*, account, capability, normalized, callback_transaction_public_id):
    queryset = PaymentTransaction.objects.filter(
        merchant_account_version=account,
        capability_version=capability,
        provider=account.provider.key,
        merchant_reference=str(normalized.merchant_reference)[:128],
    )
    if callback_transaction_public_id:
        queryset = queryset.filter(public_id=callback_transaction_public_id)
    return queryset.select_related("attempt__payment").first()


def _unauthenticated_hint_is_permitted(
    *, capability, account, normalized, callback_transaction_public_id
):
    if capability.callback_authentication != CallbackAuthenticationStrength.NONE:
        return False, None
    if not callback_transaction_public_id:
        return False, None
    merchant_reference = str(normalized.merchant_reference)
    if not BACKEND_MERCHANT_REFERENCE.fullmatch(merchant_reference):
        return False, None
    transaction_obj = _callback_transaction(
        account=account,
        capability=capability,
        normalized=normalized,
        callback_transaction_public_id=callback_transaction_public_id,
    )
    return transaction_obj is not None, transaction_obj


def _lock_isolated_evidence_identity(identity):
    """Serialize only evidence deduplication; never lock a financial aggregate."""
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                [f"financial-callback:{identity}"],
            )


def _resolve_configuration(*, provider_key, capability_version, account_key, account_version):
    provider = ProviderDefinition.objects.filter(key=provider_key).first()
    if provider is None:
        return None, None, None, "unknown_provider"
    capability = provider.capability_versions.filter(version=capability_version).first()
    if capability is None:
        return provider, None, None, "unsupported_adapter_version"
    account = MerchantAccountVersion.objects.filter(
        provider=provider,
        capability_version=capability,
        account_key=account_key,
        version=account_version,
        recovery_enabled=True,
    ).first()
    if account is None:
        return provider, capability, None, "unresolved_merchant_account"
    return provider, capability, account, ""


@transaction.atomic
def _persist_receipt_only(
    *,
    provider,
    capability,
    account,
    provider_key,
    adapter_version,
    account_key,
    method,
    content_type,
    body,
    headers,
    source_network,
    delivery_idempotency_key,
    reason,
    authentication_status=CallbackAuthenticationStatus.INVALID,
    authentication_strength="none",
    authentication_method="",
    authentication_version="",
    authentication_evidence_hash="",
    signing_key_reference="",
    replay_window_status=CallbackReplayWindowStatus.NOT_SUPPORTED,
    safe_reason_code="",
):
    raw_hash = _sha_bytes(body)
    _lock_isolated_evidence_identity(f"delivery:{delivery_idempotency_key}")
    existing = CallbackReceipt.objects.filter(delivery_idempotency_key=delivery_idempotency_key).first()
    if existing:
        if existing.raw_envelope_hash != raw_hash:
            raise IdempotencyConflict("Callback delivery key conflicts with changed raw evidence.")
        return CallbackIngestionResult(existing, replayed=True)
    duplicate = CallbackReceipt.objects.filter(
        merchant_account_version=account,
        raw_envelope_hash=raw_hash,
    ).order_by("pk").first()
    status = (
        CallbackProcessingStatus.SECURITY_REJECTED
        if authentication_status == CallbackAuthenticationStatus.INVALID
        else CallbackProcessingStatus.QUARANTINED
    )
    receipt = CallbackReceipt.objects.create(
        provider=provider,
        capability_version=capability,
        merchant_account_version=account,
        provider_key_hint=str(provider_key)[:64],
        adapter_version_hint=str(adapter_version)[:32],
        account_hint_hash=_protected_hash(account_key) if account_key else "",
        http_method=str(method).upper()[:8],
        content_type=str(content_type).split(";", 1)[0].strip().lower()[:100],
        body_length=len(body),
        raw_envelope_hash=raw_hash,
        header_evidence=_safe_header_evidence(headers),
        source_network_hash=_protected_hash(source_network) if source_network else "",
        authentication_status=authentication_status,
        authentication_strength=authentication_strength,
        authentication_method=str(authentication_method)[:64],
        authentication_version=str(authentication_version)[:32],
        authentication_evidence_hash=str(authentication_evidence_hash)[:64],
        signing_key_reference_hash=(
            _protected_hash(signing_key_reference) if signing_key_reference else ""
        ),
        replay_window_status=replay_window_status,
        processing_status=status,
        correlation_id=uuid4(),
        duplicate_of=duplicate,
        quarantine_reason=str(reason)[:64],
        safe_reason_code=str(safe_reason_code)[:64],
        delivery_idempotency_key=UUID(str(delivery_idempotency_key)),
        retention_until=timezone.now() + timedelta(days=CALLBACK_RETENTION_DAYS),
    )
    return CallbackIngestionResult(receipt)


def ingest_callback_delivery(
    *,
    provider_key,
    capability_version,
    account_key,
    account_version,
    method,
    content_type,
    body,
    headers,
    delivery_idempotency_key,
    adapter_registry,
    source_network="",
    callback_transaction_public_id=None,
):
    if not isinstance(body, bytes):
        raise ValidationError("Callback body must be exact raw bytes.")
    transport_reason = callback_transport_rejection(
        method=method,
        content_type=content_type,
        body=body,
        headers=headers,
    )
    provider, capability, account, resolution_reason = _resolve_configuration(
        provider_key=provider_key,
        capability_version=capability_version,
        account_key=account_key,
        account_version=account_version,
    )
    if transport_reason or resolution_reason:
        return _persist_receipt_only(
            provider=provider,
            capability=capability,
            account=account,
            provider_key=provider_key,
            adapter_version=capability_version,
            account_key=account_key,
            method=method,
            content_type=content_type,
            body=body,
            headers=headers,
            source_network=source_network,
            delivery_idempotency_key=delivery_idempotency_key,
            reason=transport_reason or resolution_reason,
        )

    try:
        adapter = adapter_registry.resolve(
            adapter_key=capability.adapter_key,
            contract_version=capability.adapter_contract_version,
        )
    except ValidationError:
        return _persist_receipt_only(
            provider=provider,
            capability=capability,
            account=account,
            provider_key=provider_key,
            adapter_version=capability_version,
            account_key=account_key,
            method=method,
            content_type=content_type,
            body=body,
            headers=headers,
            source_network=source_network,
            delivery_idempotency_key=delivery_idempotency_key,
            reason="unsupported_adapter_version",
        )

    try:
        auth = authenticate_callback_outside_transaction(adapter=adapter, headers=headers, body=body)
    except Exception:
        return _persist_receipt_only(
            provider=provider,
            capability=capability,
            account=account,
            provider_key=provider_key,
            adapter_version=capability_version,
            account_key=account_key,
            method=method,
            content_type=content_type,
            body=body,
            headers=headers,
            source_network=source_network,
            delivery_idempotency_key=delivery_idempotency_key,
            reason="callback_authentication_failure",
            safe_reason_code="adapter_authentication_failure",
        )
    if callback_transaction_public_id and (
        auth.status == CallbackAuthenticationStatus.AUTHENTICATED
        and (
            capability.callback_authentication == CallbackAuthenticationStrength.NONE
            or auth.strength != capability.callback_authentication
        )
    ):
        return _persist_receipt_only(
            provider=provider,
            capability=capability,
            account=account,
            provider_key=provider_key,
            adapter_version=capability_version,
            account_key=account_key,
            method=method,
            content_type=content_type,
            body=body,
            headers=headers,
            source_network=source_network,
            delivery_idempotency_key=delivery_idempotency_key,
            reason="callback_authentication_policy_mismatch",
            authentication_status=CallbackAuthenticationStatus.INVALID,
            authentication_strength=auth.strength,
            authentication_method=auth.method,
            authentication_version=auth.version,
            authentication_evidence_hash=auth.evidence_hash,
            signing_key_reference=auth.signing_key_reference,
            replay_window_status=auth.replay_window_status,
            safe_reason_code="authentication_policy_mismatch",
        )
    known_replay = _known_exact_replay(
        provider=provider,
        capability=capability,
        account=account,
        auth=auth,
        raw_hash=_sha_bytes(body),
        callback_transaction_public_id=callback_transaction_public_id,
    )
    if known_replay is not None:
        return known_replay
    if (
        auth.status == CallbackAuthenticationStatus.INVALID
        or auth.replay_window_status == CallbackReplayWindowStatus.EXPIRED
    ):
        return _persist_receipt_only(
            provider=provider,
            capability=capability,
            account=account,
            provider_key=provider_key,
            adapter_version=capability_version,
            account_key=account_key,
            method=method,
            content_type=content_type,
            body=body,
            headers=headers,
            source_network=source_network,
            delivery_idempotency_key=delivery_idempotency_key,
            reason=(
                "expired_replay_window"
                if auth.replay_window_status == CallbackReplayWindowStatus.EXPIRED
                else "invalid_signature"
            ),
            authentication_status=auth.status,
            authentication_strength=auth.strength,
            authentication_method=auth.method,
            authentication_version=auth.version,
            authentication_evidence_hash=auth.evidence_hash,
            signing_key_reference=auth.signing_key_reference,
            replay_window_status=auth.replay_window_status,
            safe_reason_code=auth.safe_reason_code,
        )
    try:
        normalized = normalize_callback_outside_transaction(
            adapter=adapter,
            authentication_result=auth,
        )
    except Exception:
        return _persist_receipt_only(
            provider=provider,
            capability=capability,
            account=account,
            provider_key=provider_key,
            adapter_version=capability_version,
            account_key=account_key,
            method=method,
            content_type=content_type,
            body=body,
            headers=headers,
            source_network=source_network,
            delivery_idempotency_key=delivery_idempotency_key,
            reason="malformed_payload",
            authentication_status=auth.status,
            authentication_strength=auth.strength,
            authentication_method=auth.method,
            authentication_version=auth.version,
            authentication_evidence_hash=auth.evidence_hash,
            signing_key_reference=auth.signing_key_reference,
            replay_window_status=auth.replay_window_status,
            safe_reason_code="normalization_failed",
        )

    permitted_hint_transaction = None
    if auth.status == CallbackAuthenticationStatus.UNAUTHENTICATED_HINT:
        permitted, permitted_hint_transaction = _unauthenticated_hint_is_permitted(
            capability=capability,
            account=account,
            normalized=normalized,
            callback_transaction_public_id=callback_transaction_public_id,
        )
        if not permitted:
            return _persist_receipt_only(
                provider=provider,
                capability=capability,
                account=account,
                provider_key=provider_key,
                adapter_version=capability_version,
                account_key=account_key,
                method=method,
                content_type=content_type,
                body=body,
                headers=headers,
                source_network=source_network,
                delivery_idempotency_key=delivery_idempotency_key,
                reason="unauthenticated_hint_not_permitted",
                authentication_status=auth.status,
                authentication_strength=auth.strength,
                authentication_method=auth.method,
                authentication_version=auth.version,
                authentication_evidence_hash=auth.evidence_hash,
                signing_key_reference=auth.signing_key_reference,
                replay_window_status=auth.replay_window_status,
                safe_reason_code="hint_rejected",
            )

    return _persist_normalized(
        provider=provider,
        capability=capability,
        account=account,
        provider_key=provider_key,
        account_key=account_key,
        method=method,
        content_type=content_type,
        body=body,
        headers=headers,
        source_network=source_network,
        delivery_idempotency_key=delivery_idempotency_key,
        auth=auth,
        normalized=normalized,
        callback_transaction_public_id=callback_transaction_public_id,
        permitted_hint_transaction=permitted_hint_transaction,
    )


@transaction.atomic
def _persist_normalized(
    *,
    provider,
    capability,
    account,
    provider_key,
    account_key,
    method,
    content_type,
    body,
    headers,
    source_network,
    delivery_idempotency_key,
    auth,
    normalized,
    callback_transaction_public_id,
    permitted_hint_transaction,
):
    raw_hash = _sha_bytes(body)
    _lock_isolated_evidence_identity(f"delivery:{delivery_idempotency_key}")
    existing_receipt = CallbackReceipt.objects.filter(
        delivery_idempotency_key=delivery_idempotency_key
    ).first()
    if existing_receipt:
        if existing_receipt.raw_envelope_hash != raw_hash:
            raise IdempotencyConflict("Callback delivery key conflicts with changed evidence.")
        event = getattr(getattr(existing_receipt, "event_link", None), "provider_event", None)
        return CallbackIngestionResult(existing_receipt, event, replayed=True)

    authenticated = auth.status == CallbackAuthenticationStatus.AUTHENTICATED
    dedupe = _deduplication_identity(
        account_id=account.pk,
        provider_event_id=auth.trustworthy_provider_event_id,
        raw_hash=raw_hash,
        authenticated=authenticated,
    )
    _lock_isolated_evidence_identity(f"event:{dedupe}")
    existing_event = (
        ProviderEvent.objects.select_related("transaction")
        .filter(deduplication_identity=dedupe)
        .first()
    )
    signing_key_hash = _protected_hash(auth.signing_key_reference) if auth.signing_key_reference else ""
    original_receipt = None
    if existing_event:
        original_receipt = (
            CallbackReceipt.objects.filter(event_link__provider_event=existing_event)
            .order_by("pk")
            .first()
        )
    contradiction = bool(
        existing_event
        and (
            existing_event.canonical_envelope_hash != raw_hash
            or _event_normalized_fingerprint(existing_event) != _normalized_fingerprint(normalized)
            or original_receipt is None
            or original_receipt.authentication_version != str(auth.version)[:32]
            or original_receipt.signing_key_reference_hash != signing_key_hash
        )
    )
    duplicate_receipt = CallbackReceipt.objects.filter(
        merchant_account_version=account,
        raw_envelope_hash=raw_hash,
    ).order_by("pk").first()

    transaction_obj = permitted_hint_transaction or _callback_transaction(
        account=account,
        capability=capability,
        normalized=normalized,
        callback_transaction_public_id=callback_transaction_public_id,
    )
    quarantine_reason = ""
    if transaction_obj is None:
        quarantine_reason = "unknown_merchant_reference"
    elif normalized.provider_authority:
        foreign = PaymentTransaction.objects.filter(
            merchant_account_version=account,
            provider_authority=normalized.provider_authority,
        ).exclude(pk=transaction_obj.pk).exists()
        if foreign:
            quarantine_reason = "foreign_provider_authority"
            contradiction = bool(existing_event)

    amount = None
    unit = ""
    if normalized.provider_amount_hint is not None:
        try:
            amount = exact_integer_money(normalized.provider_amount_hint, field="provider_amount_hint")
        except ValidationError:
            quarantine_reason = quarantine_reason or "malformed_provider_amount"
        unit = str(normalized.provider_unit_hint).upper()
        if unit not in MoneyUnit.values:
            quarantine_reason = quarantine_reason or "unsupported_provider_unit"
            amount = None
            unit = ""
    elif normalized.provider_unit_hint:
        quarantine_reason = quarantine_reason or "provider_money_incomplete"

    contradiction_event = None
    if contradiction:
        contradiction_dedupe = _sha_bytes(
            (
                f"contradiction:{dedupe}:{raw_hash}:{_normalized_fingerprint(normalized)}:"
                f"{auth.version}:{signing_key_hash}"
            ).encode("utf-8")
        )
        _lock_isolated_evidence_identity(f"event:{contradiction_dedupe}")
        contradiction_event = ProviderEvent.objects.filter(
            deduplication_identity=contradiction_dedupe
        ).first()

    processing_status = (
        CallbackProcessingStatus.DUPLICATE
        if existing_event and not contradiction
        else CallbackProcessingStatus.QUARANTINED
        if quarantine_reason or contradiction
        else CallbackProcessingStatus.NORMALIZED
    )
    receipt = CallbackReceipt.objects.create(
        provider=provider,
        capability_version=capability,
        merchant_account_version=account,
        provider_key_hint=provider.key,
        adapter_version_hint=str(capability.version),
        account_hint_hash=_protected_hash(account_key),
        http_method=str(method).upper()[:8],
        content_type=str(content_type).split(";", 1)[0].strip().lower()[:100],
        body_length=len(body),
        raw_envelope_hash=raw_hash,
        header_evidence=_safe_header_evidence(headers),
        source_network_hash=_protected_hash(source_network) if source_network else "",
        authentication_status=auth.status,
        authentication_strength=auth.strength,
        authentication_method=str(auth.method)[:64],
        authentication_version=str(auth.version)[:32],
        authentication_evidence_hash=str(auth.evidence_hash)[:64],
        signing_key_reference_hash=(
            _protected_hash(auth.signing_key_reference) if auth.signing_key_reference else ""
        ),
        replay_window_status=auth.replay_window_status,
        processing_status=processing_status,
        correlation_id=uuid4(),
        duplicate_of=duplicate_receipt,
        quarantine_reason=(
            "contradictory_callback_evidence" if contradiction else quarantine_reason
        ),
        safe_reason_code=str(auth.safe_reason_code)[:64],
        delivery_idempotency_key=UUID(str(delivery_idempotency_key)),
        retention_until=timezone.now() + timedelta(days=CALLBACK_RETENTION_DAYS),
    )

    if existing_event and not contradiction:
        ProviderEventReceipt.objects.create(
            provider_event=existing_event,
            callback_receipt=receipt,
            linkage_fingerprint=_sha_bytes(f"{existing_event.pk}:{receipt.pk}".encode("utf-8")),
        )
        return CallbackIngestionResult(receipt, existing_event, replayed=True)

    if contradiction:
        if contradiction_event is None:
            contradiction_event = ProviderEvent.objects.create(
                provider=provider,
                capability_version=capability,
                merchant_account_version=account,
                transaction=existing_event.transaction,
                original_event=existing_event,
                adapter_contract_version=capability.adapter_contract_version,
                provider_event_id=str(auth.trustworthy_provider_event_id)[:128],
                canonical_envelope_hash=raw_hash,
                merchant_reference=str(normalized.merchant_reference)[:128],
                provider_authority=str(normalized.provider_authority)[:128],
                provider_reference=str(normalized.provider_reference)[:128],
                operation_type_hint=str(normalized.operation_type_hint)[:16],
                provider_amount_hint=amount,
                provider_unit_hint=unit,
                normalized_hint=str(normalized.normalized_hint)[:64],
                provider_occurred_at=normalized.provider_occurred_at,
                authentication_strength=auth.strength,
                deduplication_identity=contradiction_dedupe,
                resolution_status=ProviderEventResolutionStatus.CONTRADICTORY,
                quarantine_reason="contradictory_callback_evidence",
                correlation_id=receipt.correlation_id,
            )
        ProviderEventReceipt.objects.create(
            provider_event=contradiction_event,
            callback_receipt=receipt,
            linkage_fingerprint=_sha_bytes(
                f"{contradiction_event.pk}:{receipt.pk}".encode("utf-8")
            ),
        )
        work_id = None
        if contradiction_event.transaction_id:
            work, _ = enqueue_verification_work(
                transaction_obj=contradiction_event.transaction,
                provider_event=contradiction_event,
                work_type=VerificationWorkType.ESCALATE_UNKNOWN_OUTCOME,
                deterministic_identity=(
                    f"callback-contradiction:{existing_event.public_id}:"
                    f"{contradiction_event.public_id}"
                ),
                correlation_id=receipt.correlation_id,
                causation_id=existing_event.public_id,
            )
            work_id = work.pk
        return CallbackIngestionResult(receipt, contradiction_event, work_id)

    event = ProviderEvent.objects.create(
        provider=provider,
        capability_version=capability,
        merchant_account_version=account,
        transaction=(transaction_obj if not quarantine_reason else None),
        original_event=None,
        adapter_contract_version=capability.adapter_contract_version,
        provider_event_id=(
            str(auth.trustworthy_provider_event_id)[:128] if authenticated else ""
        ),
        canonical_envelope_hash=raw_hash,
        merchant_reference=str(normalized.merchant_reference)[:128],
        provider_authority=str(normalized.provider_authority)[:128],
        provider_reference=str(normalized.provider_reference)[:128],
        operation_type_hint=str(normalized.operation_type_hint)[:16],
        provider_amount_hint=amount,
        provider_unit_hint=unit,
        normalized_hint=str(normalized.normalized_hint)[:64],
        provider_occurred_at=normalized.provider_occurred_at,
        authentication_strength=auth.strength,
        deduplication_identity=dedupe,
        resolution_status=(
            ProviderEventResolutionStatus.QUARANTINED
            if quarantine_reason
            else ProviderEventResolutionStatus.VERIFICATION_REQUIRED
        ),
        quarantine_reason=str(quarantine_reason)[:64],
        correlation_id=receipt.correlation_id,
    )
    ProviderEventReceipt.objects.create(
        provider_event=event,
        callback_receipt=receipt,
        linkage_fingerprint=_sha_bytes(f"{event.pk}:{receipt.pk}".encode("utf-8")),
    )
    work_id = None
    if event.transaction_id:
        work, _ = enqueue_verification_work(
            transaction_obj=transaction_obj,
            provider_event=event,
            work_type=VerificationWorkType.VERIFY_AFTER_CALLBACK,
            deterministic_identity=f"verify-callback:{event.public_id}",
            correlation_id=receipt.correlation_id,
            causation_id=event.public_id,
        )
        work_id = work.pk
    return CallbackIngestionResult(receipt, event, work_id)
