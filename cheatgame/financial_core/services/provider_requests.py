import hashlib
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid4, uuid5

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from cheatgame.digital_products.models import DigitalInventoryReservation, DigitalInventoryReservationState
from cheatgame.financial_core.models import (
    FinancialActorType,
    IdempotencyRecord,
    IdempotencyStatus,
    MerchantAccountVersion,
    Payment,
    PaymentAttempt,
    PaymentAttemptStatus,
    PaymentCollectionStatus,
    PaymentTenderType,
    PaymentTransaction,
    PaymentTransactionOperation,
    PaymentTransactionStatus,
    ProviderRequestClaim,
    ProviderRequestOutcome,
    ProviderRequestResult,
    ReviewCase,
    ReviewCaseReason,
    ReviewCaseSeverity,
    Verification,
    VerificationApplicationState,
)
from cheatgame.financial_core.services.adapters import ImmutableProviderRequestEnvelope
from cheatgame.financial_core.services.events import append_financial_event
from cheatgame.financial_core.services.idempotency import IdempotencyConflict, canonical_request_hash
from cheatgame.financial_core.services.locks import LockRank, lock_many, lock_one, ordered_lock_scope, register_lock
from cheatgame.financial_core.services.money import exact_integer_money, represent_provider_money
from cheatgame.financial_core.services.outbox import append_outbox_message
from cheatgame.financial_core.services.state_machines import (
    assert_payment_attempt_transition,
    assert_payment_transaction_transition,
    assert_payment_transition,
)
from cheatgame.shop.models import Order, StockReservation, StockReservationState


C2A_ALLOWED_REQUEST_OUTCOMES = frozenset(
    {
        ProviderRequestOutcome.CUSTOMER_ACTION_REQUIRED,
        ProviderRequestOutcome.ACCEPTED_PENDING,
        ProviderRequestOutcome.CONFIRMED_DECLINE,
        ProviderRequestOutcome.CONFIRMED_CANCELED,
        ProviderRequestOutcome.CONFIRMED_EXPIRED,
        ProviderRequestOutcome.NO_EFFECT_RETRYABLE,
        ProviderRequestOutcome.OUTCOME_UNKNOWN,
        ProviderRequestOutcome.SECURITY_FAILURE,
        ProviderRequestOutcome.CONFIGURATION_FAILURE,
        ProviderRequestOutcome.PROTOCOL_FAILURE,
    }
)


class CollectionBlocked(ValidationError):
    pass


class RequestClaimConflict(ValidationError):
    pass


class StaleRequestClaim(ValidationError):
    pass


@dataclass(frozen=True)
class AttemptCreationResult:
    attempt: PaymentAttempt
    replayed: bool


@dataclass(frozen=True)
class TransactionCreationResult:
    transaction: PaymentTransaction
    replayed: bool


@dataclass(frozen=True)
class RequestClaimResult:
    claim: ProviderRequestClaim
    envelope: ImmutableProviderRequestEnvelope
    replayed: bool


def _create_completed_idempotency(*, scope, key, request_hash, result_type, result_id, safe_response):
    existing = IdempotencyRecord.objects.filter(scope=scope, key=str(key)).first()
    if existing:
        if existing.request_hash != request_hash:
            raise IdempotencyConflict("Idempotency key was reused with a different request.")
        return existing, False
    try:
        record = IdempotencyRecord.objects.create(
            scope=scope,
            key=str(key),
            request_hash=request_hash,
            status=IdempotencyStatus.COMPLETED,
            result_type=result_type,
            result_id=str(result_id),
            safe_response=safe_response,
            completed_at=timezone.now(),
        )
    except IntegrityError as exc:
        raise IdempotencyConflict("Concurrent idempotency ownership conflict.") from exc
    return record, True


def _event(*, aggregate, event_type, command_key, actor_type, actor_id, metadata):
    register_lock(LockRank.EVENT_OUTBOX, "event-outbox")
    return append_financial_event(
        aggregate_type=aggregate._meta.label_lower,
        aggregate_id=aggregate.public_id,
        aggregate_version=aggregate.version,
        event_type=event_type,
        actor_type=actor_type,
        actor_id=actor_id,
        idempotency_key=command_key,
        correlation_id=getattr(aggregate, "correlation_id", None),
        causation_id=getattr(aggregate, "causation_id", None),
        metadata=metadata,
    )


def _lock_payment_graph(*, payment_id, attempt_id=None, transaction_id=None):
    payment_ref = Payment.objects.only("order_id").get(pk=payment_id)
    order = lock_one(queryset=Order.objects.all(), rank=LockRank.PAYABLE, pk=payment_ref.order_id)
    payment = lock_one(queryset=Payment.objects.all(), rank=LockRank.PAYMENT, pk=payment_id)
    attempts = lock_many(
        queryset=PaymentAttempt.objects.all(),
        rank=LockRank.PAYMENT_ATTEMPT,
        pks=PaymentAttempt.objects.filter(payment=payment).values_list("pk", flat=True),
    )
    attempt = next((row for row in attempts if row.pk == attempt_id), None)
    transactions = []
    transaction_obj = None
    if transaction_id is not None or attempt_id is not None:
        transactions = lock_many(
            queryset=PaymentTransaction.objects.all(),
            rank=LockRank.PAYMENT_TRANSACTION,
            pks=PaymentTransaction.objects.filter(attempt__payment=payment).values_list("pk", flat=True),
        )
        transaction_obj = next((row for row in transactions if row.pk == transaction_id), None)
    return order, payment, attempts, attempt, transactions, transaction_obj


def _assert_holds_valid(payment):
    source = getattr(payment, "obligation_source", None)
    if source is None or source.source_kind == "legacy_order_adoption":
        return
    standard_ids = list(StockReservation.objects.filter(order=payment.order).values_list("pk", flat=True))
    digital_ids = list(
        DigitalInventoryReservation.objects.filter(order=payment.order).values_list("pk", flat=True)
    )
    if bool(standard_ids) == bool(digital_ids):
        raise CollectionBlocked("Placed obligation has missing or mixed reservation authority.")
    standard = (
        lock_many(
            queryset=StockReservation.objects.all(),
            rank=LockRank.RESERVATION,
            pks=standard_ids,
        )
        if standard_ids
        else []
    )
    digital = (
        lock_many(
            queryset=DigitalInventoryReservation.objects.all(),
            rank=LockRank.RESERVATION,
            pks=digital_ids,
        )
        if digital_ids
        else []
    )
    if standard and any(item.state != StockReservationState.PAYMENT_HOLD for item in standard):
        raise CollectionBlocked("Standard commercial holds are not payment-protected.")
    if digital and any(item.state != DigitalInventoryReservationState.PAYMENT_HOLD for item in digital):
        raise CollectionBlocked("Digital commercial holds are not payment-protected.")


def _validate_account_for_new_request(account):
    capability = account.capability_version
    provider = account.provider
    if capability.provider_id != provider.id:
        raise CollectionBlocked("Merchant-account capability ownership is inconsistent.")
    if not provider.is_enabled or not provider.new_requests_enabled:
        raise CollectionBlocked("Provider kill switch blocks new requests.")
    if not account.is_enabled or not account.new_requests_enabled:
        raise CollectionBlocked("Merchant-account kill switch blocks new requests.")
    return capability


def _lock_review_blockers(payment):
    blockers = list(
        ReviewCase.objects.filter(payment=payment, status__in=("open", "investigating", "approval_pending"))
        .select_for_update()
        .order_by("pk")
    )
    for blocker in blockers:
        register_lock(LockRank.REVIEW_CASE, f"{blocker.pk:020d}")
    return blockers


@transaction.atomic
def create_or_replay_payment_attempt(
    *,
    payment_id,
    merchant_account_version_id,
    tender_type,
    requested_amount,
    idempotency_key,
    actor_type=FinancialActorType.CUSTOMER,
    actor_id=None,
):
    amount = exact_integer_money(requested_amount, field="requested_amount")
    request_payload = {
        "payment_id": int(payment_id),
        "merchant_account_version_id": int(merchant_account_version_id),
        "tender_type": str(tender_type),
        "requested_amount": str(amount),
    }
    request_hash = canonical_request_hash(request_payload)
    scope = f"financial_core:create_attempt:{payment_id}"
    account = MerchantAccountVersion.objects.select_related("provider", "capability_version").get(
        pk=merchant_account_version_id
    )
    capability = _validate_account_for_new_request(account)
    if tender_type not in (PaymentTenderType.EXTERNAL_PROVIDER, PaymentTenderType.INSTALLMENT):
        raise ValidationError("C2A provider attempts require external-provider or installment tender.")
    with ordered_lock_scope():
        _, payment, attempts, _, _, _ = _lock_payment_graph(payment_id=payment_id)
        replay = next((item for item in attempts if item.idempotency_key == UUID(str(idempotency_key))), None)
        if replay:
            if replay.request_hash != request_hash:
                raise IdempotencyConflict("PaymentAttempt key conflicts with another request.")
            return AttemptCreationResult(replay, True)
        if payment.collection_status not in (
            PaymentCollectionStatus.OPEN,
            PaymentCollectionStatus.PARTIALLY_PAID,
        ):
            raise CollectionBlocked("Payment is not collectible.")
        remaining = payment.amount_due - payment.confirmed_amount
        if remaining <= 0 or amount != remaining:
            raise CollectionBlocked("C2A requires the exact positive remaining Payment amount.")
        if any(
            attempt.status
            in (
                PaymentAttemptStatus.CREATED,
                PaymentAttemptStatus.REQUIRES_CUSTOMER_ACTION,
                PaymentAttemptStatus.PROCESSING,
                PaymentAttemptStatus.SUCCEEDED,
                PaymentAttemptStatus.OUTCOME_UNKNOWN,
                PaymentAttemptStatus.REVIEW,
            )
            for attempt in attempts
        ):
            raise CollectionBlocked("A live, successful, unknown, or review Attempt blocks collection.")
        if Verification.objects.filter(
            transaction__attempt__payment=payment,
            application_state__in=(
                VerificationApplicationState.UNAPPLIED,
                VerificationApplicationState.APPLIED_BLOCKING_SUCCESS,
                VerificationApplicationState.REVIEW_REQUIRED,
            ),
        ).exists():
            raise CollectionBlocked("Unapplied, successful, or review Verification evidence blocks collection.")
        blockers = _lock_review_blockers(payment)
        if blockers:
            raise CollectionBlocked("An unresolved ReviewCase blocks collection.")
        sequence = attempts[-1].sequence + 1 if attempts else 1
        attempt = PaymentAttempt.objects.create(
            payment=payment,
            sequence=sequence,
            requested_amount=amount,
            currency=payment.currency,
            tender_type=tender_type,
            provider=account.provider.key,
            merchant_account_ref=account.account_key,
            capability_version=capability,
            merchant_account_version=account,
            idempotency_key=UUID(str(idempotency_key)),
            request_hash=request_hash,
        )
        _assert_holds_valid(payment)
        payment_previous = payment.collection_status
        assert_payment_transition(payment_previous, PaymentCollectionStatus.PROCESSING)
        payment.collection_status = PaymentCollectionStatus.PROCESSING
        payment.version += 1
        payment.save(update_fields=("collection_status", "version", "updated_at"))
        _create_completed_idempotency(
            scope=scope,
            key=idempotency_key,
            request_hash=request_hash,
            result_type=attempt._meta.label_lower,
            result_id=attempt.pk,
            safe_response={"attempt_public_id": str(attempt.public_id), "sequence": sequence},
        )
        _event(
            aggregate=payment,
            event_type="payment.collection_started",
            command_key=f"attempt-payment:{idempotency_key}",
            actor_type=actor_type,
            actor_id=actor_id,
            metadata={"previous_status": payment_previous, "new_status": payment.collection_status},
        )
        _event(
            aggregate=attempt,
            event_type="payment_attempt.created",
            command_key=f"attempt:{idempotency_key}",
            actor_type=actor_type,
            actor_id=actor_id,
            metadata={
                "new_status": attempt.status,
                "provider": attempt.provider,
                "amount": attempt.requested_amount,
                "currency": attempt.currency,
                "sequence": attempt.sequence,
            },
        )
        return AttemptCreationResult(attempt, False)


def _merchant_reference(*, account, attempt, operation_type):
    digest = hashlib.sha256(
        f"{account.provider.key}:{account.pk}:{attempt.payment.public_id}:{attempt.sequence}:{operation_type}".encode(
            "utf-8"
        )
    ).hexdigest()[:32]
    return f"cg-{digest}"


@transaction.atomic
def create_or_replay_request_transaction(
    *,
    attempt_id,
    operation_type,
    idempotency_key,
    correlation_id=None,
    causation_id=None,
    actor_type=FinancialActorType.SYSTEM,
    actor_id=None,
):
    attempt_ref = PaymentAttempt.objects.only("payment_id").get(pk=attempt_id)
    request_payload = {
        "attempt_id": int(attempt_id),
        "operation_type": str(operation_type),
        "correlation_id": str(correlation_id) if correlation_id else "",
        "causation_id": str(causation_id) if causation_id else "",
    }
    request_hash = canonical_request_hash(request_payload)
    scope = f"financial_core:create_request_transaction:{attempt_id}"
    with ordered_lock_scope():
        _, payment, _, attempt, transactions, _ = _lock_payment_graph(
            payment_id=attempt_ref.payment_id,
            attempt_id=attempt_id,
        )
        if attempt is None:
            raise PaymentAttempt.DoesNotExist()
        replay = next((item for item in transactions if item.idempotency_key == UUID(str(idempotency_key))), None)
        if replay:
            if replay.request_fingerprint != request_hash or replay.operation_type != operation_type:
                raise IdempotencyConflict("PaymentTransaction key conflicts with another request.")
            return TransactionCreationResult(replay, True)
        if attempt.status != PaymentAttemptStatus.CREATED:
            raise CollectionBlocked("PaymentAttempt is not eligible for a new request operation.")
        if _lock_review_blockers(payment):
            raise CollectionBlocked("An unresolved ReviewCase blocks request-operation creation.")
        account = MerchantAccountVersion.objects.select_related("provider", "capability_version").get(
            pk=attempt.merchant_account_version_id
        )
        capability = _validate_account_for_new_request(account)
        if operation_type not in capability.supported_operations:
            raise ValidationError("Provider capability does not support this operation.")
        if operation_type not in (PaymentTransactionOperation.SALE, PaymentTransactionOperation.AUTHORIZE):
            raise ValidationError("C2A creates collection request operations only.")
        representation = represent_provider_money(
            canonical_amount=attempt.requested_amount,
            capability_version=capability,
        )
        sequence = transactions[-1].sequence + 1 if transactions else 1
        merchant_reference = _merchant_reference(
            account=account,
            attempt=attempt,
            operation_type=operation_type,
        )
        provider_idempotency_reference = (
            merchant_reference if capability.supports_request_idempotency else None
        )
        transaction_obj = PaymentTransaction.objects.create(
            attempt=attempt,
            sequence=sequence,
            operation_type=operation_type,
            provider=account.provider.key,
            merchant_account_ref=account.account_key,
            capability_version=capability,
            merchant_account_version=account,
            adapter_contract_version=capability.adapter_contract_version,
            merchant_reference=merchant_reference,
            amount=representation.canonical_amount,
            currency=representation.canonical_currency,
            provider_amount=representation.provider_amount,
            provider_unit=representation.provider_unit,
            provider_conversion_policy_version=representation.conversion_policy_version,
            provider_idempotency_reference=provider_idempotency_reference,
            request_fingerprint=request_hash,
            correlation_id=correlation_id or uuid4(),
            causation_id=causation_id,
            idempotency_key=UUID(str(idempotency_key)),
        )
        _create_completed_idempotency(
            scope=scope,
            key=idempotency_key,
            request_hash=request_hash,
            result_type=transaction_obj._meta.label_lower,
            result_id=transaction_obj.pk,
            safe_response={"transaction_public_id": str(transaction_obj.public_id)},
        )
        _event(
            aggregate=transaction_obj,
            event_type="payment_transaction.created",
            command_key=f"request-transaction:{idempotency_key}",
            actor_type=actor_type,
            actor_id=actor_id,
            metadata={
                "new_status": transaction_obj.status,
                "provider": transaction_obj.provider,
                "operation_type": transaction_obj.operation_type,
                "amount": transaction_obj.amount,
                "currency": transaction_obj.currency,
                "sequence": transaction_obj.sequence,
            },
        )
        return TransactionCreationResult(transaction_obj, False)


def _request_envelope(transaction_obj, claim):
    account = transaction_obj.merchant_account_version
    capability = transaction_obj.capability_version
    return ImmutableProviderRequestEnvelope(
        transaction_public_id=str(transaction_obj.public_id),
        operation_type=transaction_obj.operation_type,
        provider_key=transaction_obj.provider,
        adapter_key=capability.adapter_key,
        adapter_contract_version=transaction_obj.adapter_contract_version,
        provider_capability_version=capability.version,
        merchant_account_key=account.account_key,
        merchant_account_version=account.version,
        credential_reference=account.credential_reference,
        merchant_reference=transaction_obj.merchant_reference,
        provider_reference=transaction_obj.provider_reference or "",
        canonical_amount=str(transaction_obj.amount),
        canonical_currency=transaction_obj.currency,
        provider_amount=str(transaction_obj.provider_amount),
        provider_unit=transaction_obj.provider_unit,
        provider_idempotency_reference=transaction_obj.provider_idempotency_reference or "",
        request_fingerprint=transaction_obj.request_fingerprint,
        claim_token=str(claim.claim_token),
        callback_identity=f"financial-payment:{transaction_obj.public_id}",
        correlation_id=str(transaction_obj.correlation_id),
    )


@transaction.atomic
def claim_provider_request(
    *, transaction_id, claim_idempotency_key, lease_seconds=60, actor_type=FinancialActorType.SYSTEM, actor_id=None
):
    if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or not 5 <= lease_seconds <= 300:
        raise ValidationError("Request lease must be an integer between 5 and 300 seconds.")
    transaction_ref = PaymentTransaction.objects.select_related("attempt").only("attempt__payment_id").get(
        pk=transaction_id
    )
    request_hash = canonical_request_hash(
        {"transaction_id": int(transaction_id), "lease_seconds": lease_seconds}
    )
    scope = f"financial_core:claim_provider_request:{transaction_id}"
    with ordered_lock_scope():
        _, _, _, attempt, _, transaction_obj = _lock_payment_graph(
            payment_id=transaction_ref.attempt.payment_id,
            attempt_id=transaction_ref.attempt_id,
            transaction_id=transaction_id,
        )
        if transaction_obj is None:
            raise PaymentTransaction.DoesNotExist()
        replay = ProviderRequestClaim.objects.filter(idempotency_key=claim_idempotency_key).first()
        if replay:
            record = IdempotencyRecord.objects.filter(scope=scope, key=str(claim_idempotency_key)).first()
            if (
                replay.transaction_id != transaction_obj.pk
                or record is None
                or record.request_hash != request_hash
            ):
                raise IdempotencyConflict("Request claim key conflicts with a different claim request.")
            return RequestClaimResult(replay, _request_envelope(transaction_obj, replay), True)
        account = MerchantAccountVersion.objects.select_related("provider", "capability_version").get(
            pk=transaction_obj.merchant_account_version_id
        )
        _validate_account_for_new_request(account)
        if _lock_review_blockers(transaction_obj.attempt.payment):
            raise CollectionBlocked("An unresolved ReviewCase blocks provider request execution.")
        if transaction_obj.status != PaymentTransactionStatus.CREATED:
            raise RequestClaimConflict("PaymentTransaction is not claimable.")
        if transaction_obj.claim_token is not None:
            raise RequestClaimConflict("PaymentTransaction already has an active request claim.")
        now = timezone.now()
        claim = ProviderRequestClaim.objects.create(
            transaction=transaction_obj,
            sequence=transaction_obj.request_claims.count() + 1,
            claim_token=uuid4(),
            claimed_at=now,
            expires_at=now + timedelta(seconds=lease_seconds),
            idempotency_key=UUID(str(claim_idempotency_key)),
            correlation_id=transaction_obj.correlation_id,
            causation_id=transaction_obj.causation_id,
        )
        assert_payment_transaction_transition(transaction_obj.status, PaymentTransactionStatus.REQUESTING)
        transaction_obj.status = PaymentTransactionStatus.REQUESTING
        transaction_obj.claim_token = claim.claim_token
        transaction_obj.claimed_at = claim.claimed_at
        transaction_obj.claim_expires_at = claim.expires_at
        transaction_obj.version += 1
        transaction_obj.save(
            update_fields=(
                "status",
                "claim_token",
                "claimed_at",
                "claim_expires_at",
                "version",
                "updated_at",
            )
        )
        _create_completed_idempotency(
            scope=scope,
            key=claim_idempotency_key,
            request_hash=request_hash,
            result_type=claim._meta.label_lower,
            result_id=claim.pk,
            safe_response={
                "transaction_public_id": str(transaction_obj.public_id),
                "claim_sequence": claim.sequence,
            },
        )
        if attempt.status == PaymentAttemptStatus.CREATED:
            assert_payment_attempt_transition(attempt.status, PaymentAttemptStatus.PROCESSING)
            attempt.status = PaymentAttemptStatus.PROCESSING
            attempt.version += 1
            attempt.save(update_fields=("status", "version", "updated_at"))
            _event(
                aggregate=attempt,
                event_type="payment_attempt.processing",
                command_key=f"request-claim-attempt:{claim_idempotency_key}",
                actor_type=actor_type,
                actor_id=actor_id,
                metadata={"new_status": attempt.status},
            )
        _event(
            aggregate=transaction_obj,
            event_type="provider_request.claimed",
            command_key=f"request-claim:{claim_idempotency_key}",
            actor_type=actor_type,
            actor_id=actor_id,
            metadata={"new_status": transaction_obj.status, "provider": transaction_obj.provider},
        )
        append_outbox_message(
            topic="provider.request.claimed",
            aggregate_type=transaction_obj._meta.label_lower,
            aggregate_id=transaction_obj.public_id,
            idempotency_key=f"outbox:request-claim:{claim_idempotency_key}",
            correlation_id=transaction_obj.correlation_id,
            causation_id=transaction_obj.causation_id,
            payload={
                "event_type": "provider_request.claimed",
                "provider": transaction_obj.provider,
                "operation_type": transaction_obj.operation_type,
                "transaction_public_id": str(transaction_obj.public_id),
                "new_status": transaction_obj.status,
            },
        )
        return RequestClaimResult(claim, _request_envelope(transaction_obj, claim), False)


def _safe_result_metadata(metadata):
    allowed = {"result_code", "result_category"}
    clean = {}
    for key, value in (metadata or {}).items():
        if key in allowed and isinstance(value, (str, int, bool)):
            clean[key] = value if not isinstance(value, str) else value[:128]
    return clean


@transaction.atomic
def apply_provider_request_result(
    *,
    transaction_id,
    claim_token,
    outcome,
    evidence_hash,
    result_idempotency_key,
    reason_code="",
    safe_metadata=None,
    actor_type=FinancialActorType.SYSTEM,
    actor_id=None,
):
    if outcome == ProviderRequestOutcome.CONFIRMED_SUCCESS:
        raise ValidationError("C2A cannot apply provider success or confirmed funds.")
    if outcome not in C2A_ALLOWED_REQUEST_OUTCOMES:
        raise ValidationError("Unsupported C2A provider request result.")
    if not evidence_hash or len(str(evidence_hash)) != 64:
        raise ValidationError("A 64-character request-result evidence hash is required.")
    clean_metadata = _safe_result_metadata(safe_metadata)
    request_hash = canonical_request_hash(
        {
            "transaction_id": int(transaction_id),
            "claim_token": str(claim_token),
            "outcome": str(outcome),
            "evidence_hash": str(evidence_hash),
            "reason_code": str(reason_code)[:100],
            "safe_metadata": clean_metadata,
        }
    )
    scope = f"financial_core:apply_provider_request_result:{transaction_id}"
    transaction_ref = PaymentTransaction.objects.select_related("attempt").only("attempt__payment_id").get(
        pk=transaction_id
    )
    with ordered_lock_scope():
        _, payment, _, attempt, _, transaction_obj = _lock_payment_graph(
            payment_id=transaction_ref.attempt.payment_id,
            attempt_id=transaction_ref.attempt_id,
            transaction_id=transaction_id,
        )
        replay = ProviderRequestResult.objects.filter(idempotency_key=result_idempotency_key).first()
        if replay:
            record = IdempotencyRecord.objects.filter(scope=scope, key=str(result_idempotency_key)).first()
            if (
                replay.transaction_id != transaction_obj.pk
                or replay.claim_token != UUID(str(claim_token))
                or replay.outcome != outcome
                or replay.evidence_hash != str(evidence_hash)
                or record is None
                or record.request_hash != request_hash
            ):
                raise IdempotencyConflict("Request result key conflicts with different evidence.")
            return replay
        if transaction_obj.claim_token != UUID(str(claim_token)):
            raise StaleRequestClaim("Request result does not own the active claim token.")
        if transaction_obj.status != PaymentTransactionStatus.REQUESTING:
            raise StaleRequestClaim("Request result cannot overwrite stronger or terminal evidence.")

        mapping = {
            ProviderRequestOutcome.CUSTOMER_ACTION_REQUIRED: (
                PaymentTransactionStatus.PENDING_CUSTOMER,
                PaymentAttemptStatus.REQUIRES_CUSTOMER_ACTION,
                PaymentCollectionStatus.PROCESSING,
            ),
            ProviderRequestOutcome.ACCEPTED_PENDING: (
                PaymentTransactionStatus.PENDING_PROVIDER,
                PaymentAttemptStatus.PROCESSING,
                PaymentCollectionStatus.PROCESSING,
            ),
            ProviderRequestOutcome.CONFIRMED_DECLINE: (
                PaymentTransactionStatus.DECLINED,
                PaymentAttemptStatus.DEFINITIVE_FAILED,
                PaymentCollectionStatus.OPEN,
            ),
            ProviderRequestOutcome.CONFIRMED_CANCELED: (
                PaymentTransactionStatus.CANCELED,
                PaymentAttemptStatus.DEFINITIVE_FAILED,
                PaymentCollectionStatus.OPEN,
            ),
            ProviderRequestOutcome.CONFIRMED_EXPIRED: (
                PaymentTransactionStatus.EXPIRED,
                PaymentAttemptStatus.DEFINITIVE_FAILED,
                PaymentCollectionStatus.OPEN,
            ),
            ProviderRequestOutcome.NO_EFFECT_RETRYABLE: (
                PaymentTransactionStatus.CREATED,
                PaymentAttemptStatus.PROCESSING,
                PaymentCollectionStatus.PROCESSING,
            ),
            ProviderRequestOutcome.OUTCOME_UNKNOWN: (
                PaymentTransactionStatus.OUTCOME_UNKNOWN,
                PaymentAttemptStatus.OUTCOME_UNKNOWN,
                PaymentCollectionStatus.REVIEW,
            ),
            ProviderRequestOutcome.SECURITY_FAILURE: (
                PaymentTransactionStatus.REVIEW,
                PaymentAttemptStatus.REVIEW,
                PaymentCollectionStatus.REVIEW,
            ),
            ProviderRequestOutcome.CONFIGURATION_FAILURE: (
                PaymentTransactionStatus.REVIEW,
                PaymentAttemptStatus.REVIEW,
                PaymentCollectionStatus.REVIEW,
            ),
            ProviderRequestOutcome.PROTOCOL_FAILURE: (
                PaymentTransactionStatus.REVIEW,
                PaymentAttemptStatus.REVIEW,
                PaymentCollectionStatus.REVIEW,
            ),
        }
        tx_target, attempt_target, payment_target = mapping[outcome]
        tx_previous = transaction_obj.status
        attempt_previous = attempt.status
        payment_previous = payment.collection_status
        assert_payment_transaction_transition(tx_previous, tx_target)
        assert_payment_attempt_transition(attempt_previous, attempt_target)
        assert_payment_transition(payment_previous, payment_target)
        result = ProviderRequestResult.objects.create(
            transaction=transaction_obj,
            outcome=outcome,
            claim_token=UUID(str(claim_token)),
            request_fingerprint=transaction_obj.request_fingerprint,
            evidence_hash=str(evidence_hash),
            reason_code=str(reason_code)[:100],
            safe_metadata=clean_metadata,
            idempotency_key=UUID(str(result_idempotency_key)),
            correlation_id=transaction_obj.correlation_id,
            causation_id=transaction_obj.causation_id,
        )
        _create_completed_idempotency(
            scope=scope,
            key=result_idempotency_key,
            request_hash=request_hash,
            result_type=result._meta.label_lower,
            result_id=result.pk,
            safe_response={"transaction_public_id": str(transaction_obj.public_id), "outcome": outcome},
        )
        transaction_obj.status = tx_target
        transaction_obj.claim_token = None
        transaction_obj.claimed_at = None
        transaction_obj.claim_expires_at = None
        if tx_target in (
            PaymentTransactionStatus.DECLINED,
            PaymentTransactionStatus.CANCELED,
            PaymentTransactionStatus.EXPIRED,
        ):
            transaction_obj.completed_at = timezone.now()
        transaction_obj.version += 1
        transaction_obj.save(
            update_fields=(
                "status",
                "claim_token",
                "claimed_at",
                "claim_expires_at",
                "completed_at",
                "version",
                "updated_at",
            )
        )
        if attempt.status != attempt_target:
            attempt.status = attempt_target
            attempt.version += 1
            attempt.save(update_fields=("status", "version", "updated_at"))
        if payment.collection_status != payment_target:
            payment.collection_status = payment_target
            payment.version += 1
            payment.save(update_fields=("collection_status", "version", "updated_at"))

        review = None
        if outcome in (
            ProviderRequestOutcome.OUTCOME_UNKNOWN,
            ProviderRequestOutcome.SECURITY_FAILURE,
            ProviderRequestOutcome.CONFIGURATION_FAILURE,
            ProviderRequestOutcome.PROTOCOL_FAILURE,
        ):
            review_key = uuid5(transaction_obj.public_id, f"request-result-review:{result.idempotency_key}")
            register_lock(LockRank.REVIEW_CASE, f"review:new:{review_key}")
            review = ReviewCase.objects.create(
                reason=(
                    ReviewCaseReason.PROVIDER_STATE_UNCLEAR
                    if outcome == ProviderRequestOutcome.OUTCOME_UNKNOWN
                    else ReviewCaseReason.INVARIANT_VIOLATION
                ),
                severity=(
                    ReviewCaseSeverity.HIGH
                    if outcome == ProviderRequestOutcome.OUTCOME_UNKNOWN
                    else ReviewCaseSeverity.CRITICAL
                ),
                summary="Provider request requires controlled follow-up.",
                order=payment.order,
                payment=payment,
                attempt=attempt,
                transaction=transaction_obj,
                opened_by_type=actor_type,
                opened_by_id=actor_id,
                idempotency_key=review_key,
            )

        _event(
            aggregate=transaction_obj,
            event_type="provider_request.result_recorded",
            command_key=f"request-result:{result_idempotency_key}",
            actor_type=actor_type,
            actor_id=actor_id,
            metadata={
                "previous_status": tx_previous,
                "new_status": transaction_obj.status,
                "outcome": outcome,
                "reason_code": reason_code,
            },
        )
        if attempt_previous != attempt.status:
            _event(
                aggregate=attempt,
                event_type=(
                    "payment.outcome_unknown"
                    if attempt.status == PaymentAttemptStatus.OUTCOME_UNKNOWN
                    else "payment_attempt.status_changed"
                ),
                command_key=f"request-result-attempt:{result_idempotency_key}",
                actor_type=actor_type,
                actor_id=actor_id,
                metadata={"previous_status": attempt_previous, "new_status": attempt.status},
            )
        if payment_previous != payment.collection_status:
            _event(
                aggregate=payment,
                event_type=(
                    "payment.collection_blocked"
                    if payment.collection_status == PaymentCollectionStatus.REVIEW
                    else "payment.collection_status_changed"
                ),
                command_key=f"request-result-payment:{result_idempotency_key}",
                actor_type=actor_type,
                actor_id=actor_id,
                metadata={
                    "previous_status": payment_previous,
                    "new_status": payment.collection_status,
                    "reason_code": reason_code,
                },
            )
        if review is not None:
            register_lock(LockRank.EVENT_OUTBOX, "event-outbox")
            append_financial_event(
                aggregate_type=review._meta.label_lower,
                aggregate_id=review.public_id,
                aggregate_version=review.version,
                event_type="review_case.opened",
                actor_type=actor_type,
                actor_id=actor_id,
                idempotency_key=f"request-result-review:{result_idempotency_key}",
                correlation_id=transaction_obj.correlation_id,
                causation_id=transaction_obj.public_id,
                metadata={
                    "reason_code": review.reason,
                    "severity": review.severity,
                    "new_status": review.status,
                },
            )
        return result
