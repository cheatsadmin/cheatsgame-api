import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4, uuid5

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from cheatgame.financial_core.models import (
    CANONICAL_CURRENCY,
    FinancialActorType,
    IdempotencyRecord,
    IdempotencyStatus,
    Payment,
    PaymentAttempt,
    PaymentAttemptStatus,
    PaymentCollectionStatus,
    PaymentTransaction,
    PaymentTransactionStatus,
    ProviderReferenceAllocation,
    ReviewCase,
    ReviewCaseReason,
    ReviewCaseSeverity,
    ReviewCaseStatus,
    Verification,
    VerificationApplicationState,
    VerificationClaim,
    VerificationFinality,
    VerificationFinancialEffect,
    VerificationOutcome,
    VerificationTransportClassification,
    VerificationTriggerSource,
    VerificationWorkItem,
    VerificationWorkStatus,
    VerificationWorkType,
)
from cheatgame.financial_core.services.adapters import VerificationEnvelope
from cheatgame.financial_core.services.events import append_financial_event
from cheatgame.financial_core.services.idempotency import IdempotencyConflict, canonical_request_hash
from cheatgame.financial_core.services.locks import LockRank, lock_many, lock_one, ordered_lock_scope, register_lock
from cheatgame.financial_core.services.money import exact_integer_money
from cheatgame.financial_core.services.outbox import append_outbox_message
from cheatgame.financial_core.services.state_machines import (
    assert_payment_attempt_transition,
    assert_payment_transaction_transition,
    assert_payment_transition,
)
from cheatgame.shop.models import Order


VERIFICATION_NAMESPACE = UUID("7ee49c1b-3f36-47d9-87d2-90c2894943a1")


class VerificationBlocked(ValidationError):
    pass


class VerificationClaimConflict(ValidationError):
    pass


class StaleVerificationClaim(ValidationError):
    pass


@dataclass(frozen=True)
class VerificationClaimResult:
    claim: VerificationClaim
    envelope: VerificationEnvelope
    replayed: bool


def _sha(payload):
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _complete_idempotency(*, scope, key, request_hash, result_type, result_id, safe_response):
    existing = IdempotencyRecord.objects.filter(scope=scope, key=str(key)).first()
    if existing:
        if existing.request_hash != request_hash:
            raise IdempotencyConflict("Idempotency identity conflicts with different verification evidence.")
        return existing
    return IdempotencyRecord.objects.create(
        scope=scope,
        key=str(key),
        request_hash=request_hash,
        status=IdempotencyStatus.COMPLETED,
        result_type=result_type,
        result_id=str(result_id),
        safe_response=safe_response,
        completed_at=timezone.now(),
    )


def enqueue_verification_work(
    *,
    transaction_obj,
    work_type,
    deterministic_identity,
    correlation_id,
    provider_event=None,
    causation_id=None,
    next_attempt_at=None,
    max_attempts=8,
):
    if work_type not in VerificationWorkType.values:
        raise ValidationError("Unsupported verification work type.")
    fingerprint = _sha(
        {
            "transaction_id": transaction_obj.pk,
            "provider_event_id": provider_event.pk if provider_event else None,
            "work_type": work_type,
            "deterministic_identity": deterministic_identity,
        }
    )
    existing = VerificationWorkItem.objects.filter(deterministic_identity=deterministic_identity).first()
    if existing:
        existing_fingerprint = _sha(
            {
                "transaction_id": existing.transaction_id,
                "provider_event_id": existing.provider_event_id,
                "work_type": existing.work_type,
                "deterministic_identity": existing.deterministic_identity,
            }
        )
        if existing_fingerprint != fingerprint:
            raise IdempotencyConflict("Verification work identity conflicts with another subject.")
        return existing, True
    try:
        return (
            VerificationWorkItem.objects.create(
                transaction=transaction_obj,
                provider_event=provider_event,
                work_type=work_type,
                deterministic_identity=deterministic_identity,
                next_attempt_at=next_attempt_at or timezone.now(),
                max_attempts=max_attempts,
                correlation_id=correlation_id,
                causation_id=causation_id,
            ),
            False,
        )
    except IntegrityError as exc:
        raise IdempotencyConflict("Concurrent verification work ownership conflict.") from exc


def _lock_graph(transaction_id):
    tx_ref = PaymentTransaction.objects.select_related("attempt__payment").only(
        "attempt_id", "attempt__payment_id", "attempt__payment__order_id"
    ).get(pk=transaction_id)
    order = lock_one(
        queryset=Order.objects.all(),
        rank=LockRank.PAYABLE,
        pk=tx_ref.attempt.payment.order_id,
    )
    payment = lock_one(
        queryset=Payment.objects.all(), rank=LockRank.PAYMENT, pk=tx_ref.attempt.payment_id
    )
    attempts = lock_many(
        queryset=PaymentAttempt.objects.all(),
        rank=LockRank.PAYMENT_ATTEMPT,
        pks=PaymentAttempt.objects.filter(payment=payment).values_list("pk", flat=True),
    )
    transactions = lock_many(
        queryset=PaymentTransaction.objects.all(),
        rank=LockRank.PAYMENT_TRANSACTION,
        pks=PaymentTransaction.objects.filter(attempt__payment=payment).values_list("pk", flat=True),
    )
    transaction_obj = next(item for item in transactions if item.pk == transaction_id)
    attempt = next(item for item in attempts if item.pk == transaction_obj.attempt_id)
    return order, payment, attempts, attempt, transactions, transaction_obj


def _verification_envelope(transaction_obj, claim):
    account = transaction_obj.merchant_account_version
    capability = transaction_obj.capability_version
    return VerificationEnvelope(
        transaction_public_id=str(transaction_obj.public_id),
        operation_type=transaction_obj.operation_type,
        provider_key=transaction_obj.provider,
        adapter_key=capability.adapter_key,
        adapter_contract_version=transaction_obj.adapter_contract_version,
        merchant_account_key=account.account_key,
        merchant_account_version=account.version,
        credential_reference=account.credential_reference,
        merchant_reference=transaction_obj.merchant_reference,
        provider_authority=transaction_obj.provider_authority or "",
        provider_reference=transaction_obj.provider_reference or "",
        requested_provider_amount=str(transaction_obj.provider_amount),
        requested_provider_unit=transaction_obj.provider_unit,
        canonical_amount=str(transaction_obj.amount),
        canonical_currency=transaction_obj.currency,
        claim_token=str(claim.claim_token),
        correlation_id=str(transaction_obj.correlation_id),
    )


@transaction.atomic
def claim_verification_work(
    *,
    work_item_id,
    trigger_source,
    claim_idempotency_key,
    lease_seconds=60,
    actor_type=FinancialActorType.SYSTEM,
    actor_id=None,
):
    if trigger_source not in VerificationTriggerSource.values:
        raise ValidationError("Unsupported verification trigger source.")
    if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or not 5 <= lease_seconds <= 300:
        raise ValidationError("Verification lease must be an integer between 5 and 300 seconds.")
    work_ref = VerificationWorkItem.objects.only("transaction_id").get(pk=work_item_id)
    request_hash = canonical_request_hash(
        {
            "work_item_id": int(work_item_id),
            "trigger_source": str(trigger_source),
            "lease_seconds": lease_seconds,
        }
    )
    scope = f"financial_core:verification_claim:{work_item_id}"
    with ordered_lock_scope():
        _, payment, _, attempt, _, transaction_obj = _lock_graph(work_ref.transaction_id)
        work = VerificationWorkItem.objects.get(pk=work_item_id)
        replay = VerificationClaim.objects.filter(idempotency_key=claim_idempotency_key).first()
        if replay:
            record = IdempotencyRecord.objects.filter(scope=scope, key=str(claim_idempotency_key)).first()
            if replay.work_item_id != work.pk or record is None or record.request_hash != request_hash:
                raise IdempotencyConflict("Verification claim key conflicts with another request.")
            return VerificationClaimResult(replay, _verification_envelope(transaction_obj, replay), True)
        now = timezone.now()
        if work.transaction_id != transaction_obj.pk:
            raise VerificationBlocked("Verification work ownership is inconsistent.")
        if work.status == VerificationWorkStatus.CLAIMED:
            if work.claim_expires_at and work.claim_expires_at <= now:
                # Verification/query operations are read-only at the provider.
                # Preserve the old immutable claim, invalidate its token, and
                # permit a new bounded query without inferring an unpaid result.
                work.status = VerificationWorkStatus.WAITING
                work.claim_token = None
                work.claimed_at = None
                work.claim_expires_at = None
                work.version += 1
                work.save(
                    update_fields=(
                        "status",
                        "claim_token",
                        "claimed_at",
                        "claim_expires_at",
                        "version",
                        "updated_at",
                    )
                )
            else:
                raise VerificationClaimConflict("Verification work already has an active claim.")
        if work.status not in (VerificationWorkStatus.PENDING, VerificationWorkStatus.WAITING):
            raise VerificationClaimConflict("Verification work is not claimable.")
        if work.next_attempt_at > now or work.attempt_count >= work.max_attempts:
            raise VerificationClaimConflict("Verification work is not due or exhausted.")
        if transaction_obj.merchant_account_version_id is None or transaction_obj.capability_version_id is None:
            raise VerificationBlocked("Only versioned C2A provider operations can be verified.")
        if payment.collection_status in (PaymentCollectionStatus.PAID, PaymentCollectionStatus.CANCELED):
            raise VerificationBlocked("Terminal Payment cannot enter C2B1 verification.")
        sequence = work.claims.count() + 1
        claim = VerificationClaim.objects.create(
            work_item=work,
            transaction=transaction_obj,
            sequence=sequence,
            claim_token=uuid4(),
            claimed_at=now,
            expires_at=now + timedelta(seconds=lease_seconds),
            request_fingerprint=request_hash,
            idempotency_key=UUID(str(claim_idempotency_key)),
            correlation_id=work.correlation_id,
            causation_id=work.causation_id,
        )
        work.status = VerificationWorkStatus.CLAIMED
        work.attempt_count += 1
        work.claim_token = claim.claim_token
        work.claimed_at = claim.claimed_at
        work.claim_expires_at = claim.expires_at
        work.version += 1
        work.save(
            update_fields=(
                "status",
                "attempt_count",
                "claim_token",
                "claimed_at",
                "claim_expires_at",
                "version",
                "updated_at",
            )
        )
        _complete_idempotency(
            scope=scope,
            key=claim_idempotency_key,
            request_hash=request_hash,
            result_type=claim._meta.label_lower,
            result_id=claim.pk,
            safe_response={"work_public_id": str(work.public_id), "claim_sequence": claim.sequence},
        )
        register_lock(LockRank.EVENT_OUTBOX, "verification-claim-event")
        append_financial_event(
            aggregate_type=work._meta.label_lower,
            aggregate_id=work.public_id,
            aggregate_version=work.version,
            event_type="provider_verification.claimed",
            actor_type=actor_type,
            actor_id=actor_id,
            idempotency_key=f"verification-claim:{claim_idempotency_key}",
            correlation_id=work.correlation_id,
            causation_id=work.causation_id,
            metadata={"provider": transaction_obj.provider, "sequence": claim.sequence},
        )
        return VerificationClaimResult(claim, _verification_envelope(transaction_obj, claim), False)


def _logical_review_key(transaction_obj, reason):
    return uuid5(VERIFICATION_NAMESPACE, f"review:{transaction_obj.public_id}:{reason}")


def _ensure_review(*, order, payment, attempt, transaction_obj, reason, severity, summary, correlation_id):
    key = _logical_review_key(transaction_obj, reason)
    register_lock(LockRank.REVIEW_CASE, f"review:{key}")
    existing = ReviewCase.objects.select_for_update().filter(idempotency_key=key).first()
    if existing:
        existing.version += 1
        existing.save(update_fields=("version", "updated_at"))
        return existing, False
    review = ReviewCase.objects.create(
        reason=reason,
        severity=severity,
        summary=summary[:1000],
        order=order,
        payment=payment,
        attempt=attempt,
        transaction=transaction_obj,
        opened_by_type=FinancialActorType.SYSTEM,
        idempotency_key=key,
    )
    return review, True


def _comparison_failure(transaction_obj, result, observed_amount, observed_unit):
    if result.provider_key != transaction_obj.provider:
        return "provider_identity_mismatch"
    if result.adapter_contract_version != transaction_obj.adapter_contract_version:
        return "adapter_version_mismatch"
    if result.merchant_account_key != transaction_obj.merchant_account_ref:
        return "merchant_account_mismatch"
    if result.merchant_account_version != transaction_obj.merchant_account_version.version:
        return "merchant_account_version_mismatch"
    if result.merchant_reference != transaction_obj.merchant_reference:
        return "merchant_reference_mismatch"
    if result.operation_type != transaction_obj.operation_type:
        return "operation_type_mismatch"
    if transaction_obj.provider_authority and result.provider_authority != transaction_obj.provider_authority:
        return "provider_authority_mismatch"
    if transaction_obj.provider_reference and result.provider_reference != transaction_obj.provider_reference:
        return "provider_reference_mismatch"
    if observed_amount is None or observed_amount != transaction_obj.provider_amount:
        return "provider_amount_mismatch"
    if observed_unit != transaction_obj.provider_unit:
        return "provider_unit_mismatch"
    if transaction_obj.currency != CANONICAL_CURRENCY or transaction_obj.amount != transaction_obj.attempt.requested_amount:
        return "canonical_allocation_mismatch"
    if not result.provider_reference:
        return "provider_reference_missing"
    return ""


def _other_blocker(*, payment, attempt, transaction_obj):
    if Verification.objects.filter(
        transaction__attempt__payment=payment,
        application_state__in=(
            VerificationApplicationState.APPLIED_BLOCKING_SUCCESS,
            VerificationApplicationState.REVIEW_REQUIRED,
            VerificationApplicationState.UNAPPLIED,
        ),
    ).exclude(transaction=transaction_obj).exists():
        return True
    if PaymentAttempt.objects.filter(payment=payment).exclude(pk=attempt.pk).filter(
        status__in=(
            PaymentAttemptStatus.CREATED,
            PaymentAttemptStatus.REQUIRES_CUSTOMER_ACTION,
            PaymentAttemptStatus.PROCESSING,
            PaymentAttemptStatus.SUCCEEDED,
            PaymentAttemptStatus.OUTCOME_UNKNOWN,
            PaymentAttemptStatus.REVIEW,
        )
    ).exists():
        return True
    return PaymentTransaction.objects.filter(attempt=attempt).exclude(pk=transaction_obj.pk).filter(
        status__in=(
            PaymentTransactionStatus.REQUESTING,
            PaymentTransactionStatus.PENDING_CUSTOMER,
            PaymentTransactionStatus.PENDING_PROVIDER,
            PaymentTransactionStatus.CALLBACK_RECEIVED,
            PaymentTransactionStatus.VERIFYING,
            PaymentTransactionStatus.SUCCEEDED,
            PaymentTransactionStatus.OUTCOME_UNKNOWN,
            PaymentTransactionStatus.REVIEW,
        )
    ).exists()


@transaction.atomic
def apply_verification_result(
    *,
    claim_token,
    result,
    result_idempotency_key,
    trigger_source,
    actor_type=FinancialActorType.SYSTEM,
    actor_id=None,
):
    if trigger_source not in VerificationTriggerSource.values:
        raise ValidationError("Unsupported verification trigger source.")
    if result.outcome not in VerificationOutcome.values:
        raise ValidationError("Unsupported normalized verification outcome.")
    if result.financial_effect not in VerificationFinancialEffect.values:
        raise ValidationError("Unsupported normalized verification financial effect.")
    if result.finality not in VerificationFinality.values:
        raise ValidationError("Unsupported normalized verification finality.")
    if result.transport_classification not in VerificationTransportClassification.values:
        raise ValidationError("Unsupported verification transport classification.")
    if not result.evidence_hash or len(str(result.evidence_hash)) != 64:
        raise ValidationError("Verification evidence requires a 64-character sanitized hash.")
    observed_amount = None
    observed_unit = ""
    malformed_observed_money = False
    if result.observed_provider_amount is not None:
        try:
            observed_amount = exact_integer_money(
                result.observed_provider_amount,
                field="observed_provider_amount",
            )
        except ValidationError:
            malformed_observed_money = True
        observed_unit = str(result.observed_provider_unit).upper()
        if observed_unit not in ("IRR", "IRT"):
            malformed_observed_money = True
        if malformed_observed_money:
            observed_amount = None
            observed_unit = ""
    elif result.observed_provider_unit:
        malformed_observed_money = True
    claim_ref = VerificationClaim.objects.select_related("transaction").get(claim_token=claim_token)
    clean_payload = {
        "claim_token": str(claim_token),
        "outcome": result.outcome,
        "financial_effect": result.financial_effect,
        "finality": result.finality,
        "transport": result.transport_classification,
        "provider_key": result.provider_key,
        "adapter_contract_version": result.adapter_contract_version,
        "merchant_account_key": result.merchant_account_key,
        "merchant_account_version": result.merchant_account_version,
        "merchant_reference": result.merchant_reference,
        "provider_authority": result.provider_authority,
        "provider_reference": result.provider_reference,
        "operation_type": result.operation_type,
        "observed_provider_amount": str(observed_amount) if observed_amount is not None else None,
        "observed_provider_unit": observed_unit,
        "observed_money_malformed": malformed_observed_money,
        "evidence_hash": result.evidence_hash,
        "error_classification": result.error_classification,
        "retryable": bool(result.retryable),
        "already_verified_fresh_query": bool(result.already_verified_fresh_query),
    }
    result_fingerprint = canonical_request_hash(clean_payload)
    scope = f"financial_core:verification_result:{claim_ref.transaction_id}"
    with ordered_lock_scope():
        order, payment, _, attempt, _, transaction_obj = _lock_graph(claim_ref.transaction_id)
        claim = VerificationClaim.objects.select_related("work_item").get(pk=claim_ref.pk)
        replay = Verification.objects.filter(result_idempotency_key=result_idempotency_key).first()
        if replay:
            record = IdempotencyRecord.objects.filter(scope=scope, key=str(result_idempotency_key)).first()
            if replay.claim_id != claim.pk or replay.result_fingerprint != result_fingerprint or record is None:
                raise IdempotencyConflict("Verification result identity conflicts with different evidence.")
            return replay
        now = timezone.now()
        work = VerificationWorkItem.objects.get(pk=claim.work_item_id)
        if claim.claim_token != UUID(str(claim_token)) or work.claim_token != claim.claim_token:
            raise StaleVerificationClaim("Verification result does not own the active claim.")
        if claim.expires_at <= now or work.status != VerificationWorkStatus.CLAIMED:
            raise StaleVerificationClaim("Verification claim is stale and cannot apply evidence.")
        if Verification.objects.filter(claim=claim).exists():
            raise StaleVerificationClaim("Verification claim already produced immutable evidence.")

        outcome = result.outcome
        error_classification = str(result.error_classification)[:64]
        comparison_failure = ""
        if malformed_observed_money:
            outcome = VerificationOutcome.PROTOCOL_FAILURE
            error_classification = "malformed_observed_provider_money"
        if outcome == VerificationOutcome.CONFIRMED_SUCCESS:
            comparison_failure = _comparison_failure(
                transaction_obj,
                result,
                observed_amount,
                observed_unit,
            )
            if result.financial_effect != VerificationFinancialEffect.PAID or result.finality != VerificationFinality.FINAL:
                comparison_failure = comparison_failure or "success_not_final_paid"
            if result.already_verified_fresh_query is False and error_classification == "already_verified":
                comparison_failure = comparison_failure or "already_verified_not_fresh_query"
            if comparison_failure:
                outcome = VerificationOutcome.MISMATCH
                error_classification = comparison_failure
        if outcome == VerificationOutcome.NOT_FOUND_FINAL and not (
            transaction_obj.capability_version.supports_lookup
            and transaction_obj.capability_version.not_found_is_final_unpaid
        ):
            outcome = VerificationOutcome.OUTCOME_UNKNOWN
            error_classification = "not_found_finality_not_guaranteed"

        existing_success = Verification.objects.filter(
            transaction__attempt__payment=payment,
            application_state=VerificationApplicationState.APPLIED_BLOCKING_SUCCESS,
        ).exists()
        if existing_success and outcome != VerificationOutcome.CONFIRMED_SUCCESS:
            outcome = VerificationOutcome.CONTRADICTORY_EVIDENCE
            error_classification = "weaker_evidence_after_verified_success"

        reference_allocation = None
        if outcome == VerificationOutcome.CONFIRMED_SUCCESS:
            reference_allocation = ProviderReferenceAllocation.objects.filter(
                merchant_account_version=transaction_obj.merchant_account_version,
                provider_reference=result.provider_reference,
            ).first()
            if reference_allocation and reference_allocation.transaction_id != transaction_obj.pk:
                outcome = VerificationOutcome.CONTRADICTORY_EVIDENCE
                error_classification = "provider_reference_owned_by_another_obligation"

        definitive_map = {
            VerificationOutcome.CONFIRMED_DECLINE: PaymentTransactionStatus.DECLINED,
            VerificationOutcome.CONFIRMED_CANCELED: PaymentTransactionStatus.CANCELED,
            VerificationOutcome.CONFIRMED_EXPIRED: PaymentTransactionStatus.EXPIRED,
            VerificationOutcome.NOT_FOUND_FINAL: PaymentTransactionStatus.EXPIRED,
        }
        definitive_unpaid = (
            outcome in definitive_map
            and result.financial_effect == VerificationFinancialEffect.UNPAID
            and result.finality == VerificationFinality.FINAL
        )
        if outcome in definitive_map and not definitive_unpaid:
            outcome = VerificationOutcome.OUTCOME_UNKNOWN
            error_classification = error_classification or "unpaid_finality_not_proven"

        if outcome == VerificationOutcome.CONFIRMED_SUCCESS:
            application_state = VerificationApplicationState.APPLIED_BLOCKING_SUCCESS
        elif definitive_unpaid and not _other_blocker(
            payment=payment, attempt=attempt, transaction_obj=transaction_obj
        ):
            application_state = VerificationApplicationState.APPLIED_UNPAID
        elif outcome in (VerificationOutcome.PENDING, VerificationOutcome.NO_EFFECT_RETRYABLE):
            application_state = VerificationApplicationState.UNAPPLIED
        else:
            application_state = VerificationApplicationState.REVIEW_REQUIRED

        sequence = Verification.objects.filter(transaction=transaction_obj).count() + 1
        verification = Verification.objects.create(
            transaction=transaction_obj,
            claim=claim,
            work_item=work,
            provider_event=work.provider_event,
            provider=transaction_obj.capability_version.provider,
            capability_version=transaction_obj.capability_version,
            merchant_account_version=transaction_obj.merchant_account_version,
            sequence=sequence,
            trigger_source=trigger_source,
            adapter_contract_version=transaction_obj.adapter_contract_version,
            merchant_reference=result.merchant_reference,
            provider_authority=result.provider_authority,
            provider_reference=result.provider_reference,
            operation_type=result.operation_type,
            requested_provider_amount=transaction_obj.provider_amount,
            requested_provider_unit=transaction_obj.provider_unit,
            observed_provider_amount=observed_amount,
            observed_provider_unit=observed_unit,
            canonical_allocation_amount=transaction_obj.amount,
            canonical_currency=transaction_obj.currency,
            normalized_outcome=outcome,
            normalized_financial_effect=result.financial_effect,
            finality=result.finality,
            provider_occurred_at=result.provider_occurred_at,
            transport_classification=result.transport_classification,
            evidence_hash=str(result.evidence_hash),
            request_evidence_reference=str(claim.public_id if hasattr(claim, "public_id") else claim.pk),
            response_evidence_reference=str(result.response_evidence_reference)[:128],
            correlation_id=claim.correlation_id,
            causation_id=claim.causation_id,
            verified_at=now,
            application_state=application_state,
            error_classification=error_classification,
            retryable=bool(result.retryable),
            result_idempotency_key=UUID(str(result_idempotency_key)),
            result_fingerprint=result_fingerprint,
        )

        review_reason = None
        enqueue_apply_work = False
        if outcome == VerificationOutcome.CONFIRMED_SUCCESS:
            if reference_allocation is None:
                ProviderReferenceAllocation.objects.create(
                    merchant_account_version=transaction_obj.merchant_account_version,
                    transaction=transaction_obj,
                    verification=verification,
                    provider_reference=result.provider_reference,
                    allocation_fingerprint=_sha(
                        {
                            "account": transaction_obj.merchant_account_version_id,
                            "reference": result.provider_reference,
                            "transaction": transaction_obj.pk,
                        }
                    ),
                )
            if not transaction_obj.provider_authority:
                transaction_obj.provider_authority = result.provider_authority or None
            if not transaction_obj.provider_reference:
                transaction_obj.provider_reference = result.provider_reference
            late_terminal = transaction_obj.status in (
                PaymentTransactionStatus.DECLINED,
                PaymentTransactionStatus.CANCELED,
                PaymentTransactionStatus.EXPIRED,
            ) or attempt.status == PaymentAttemptStatus.DEFINITIVE_FAILED
            tx_target = transaction_obj.status if late_terminal else PaymentTransactionStatus.REVIEW
            attempt_target = attempt.status if late_terminal else PaymentAttemptStatus.REVIEW
            payment_target = PaymentCollectionStatus.REVIEW
            review_reason = (
                ReviewCaseReason.LATE_PAYMENT
                if late_terminal
                else ReviewCaseReason.PAID_FINALIZATION_PENDING
            )
            enqueue_apply_work = True
        elif application_state == VerificationApplicationState.APPLIED_UNPAID:
            tx_target = definitive_map[outcome]
            attempt_target = PaymentAttemptStatus.DEFINITIVE_FAILED
            payment_target = PaymentCollectionStatus.OPEN
        elif outcome in (VerificationOutcome.PENDING, VerificationOutcome.NO_EFFECT_RETRYABLE):
            tx_target = PaymentTransactionStatus.PENDING_PROVIDER
            attempt_target = PaymentAttemptStatus.PROCESSING
            payment_target = PaymentCollectionStatus.PROCESSING
        else:
            tx_target = PaymentTransactionStatus.OUTCOME_UNKNOWN if outcome == VerificationOutcome.OUTCOME_UNKNOWN else PaymentTransactionStatus.REVIEW
            attempt_target = PaymentAttemptStatus.OUTCOME_UNKNOWN if outcome == VerificationOutcome.OUTCOME_UNKNOWN else PaymentAttemptStatus.REVIEW
            payment_target = PaymentCollectionStatus.REVIEW
            if outcome == VerificationOutcome.MISMATCH:
                review_reason = (
                    ReviewCaseReason.CURRENCY_MISMATCH
                    if "unit" in error_classification
                    else ReviewCaseReason.AMOUNT_MISMATCH
                )
            elif outcome == VerificationOutcome.CONTRADICTORY_EVIDENCE:
                review_reason = ReviewCaseReason.DUPLICATE_PROVIDER_REFERENCE
            elif outcome == VerificationOutcome.SECURITY_FAILURE:
                review_reason = ReviewCaseReason.FRAUD_RISK
            else:
                review_reason = ReviewCaseReason.PROVIDER_STATE_UNCLEAR

        assert_payment_transaction_transition(transaction_obj.status, tx_target)
        assert_payment_attempt_transition(attempt.status, attempt_target)
        assert_payment_transition(payment.collection_status, payment_target)
        transaction_obj.status = tx_target
        transaction_obj.version += 1
        tx_fields = ["status", "version", "updated_at"]
        if transaction_obj.provider_authority:
            tx_fields.append("provider_authority")
        if transaction_obj.provider_reference:
            tx_fields.append("provider_reference")
        if tx_target in (
            PaymentTransactionStatus.DECLINED,
            PaymentTransactionStatus.CANCELED,
            PaymentTransactionStatus.EXPIRED,
        ):
            transaction_obj.completed_at = now
            tx_fields.append("completed_at")
        transaction_obj.save(update_fields=tuple(dict.fromkeys(tx_fields)))
        attempt.status = attempt_target
        attempt.version += 1
        attempt.save(update_fields=("status", "version", "updated_at"))
        payment.collection_status = payment_target
        payment.version += 1
        payment.save(update_fields=("collection_status", "version", "updated_at"))

        review = None
        review_created = False
        if review_reason:
            review, review_created = _ensure_review(
                order=order,
                payment=payment,
                attempt=attempt,
                transaction_obj=transaction_obj,
                reason=review_reason,
                severity=(
                    ReviewCaseSeverity.CRITICAL
                    if outcome in (VerificationOutcome.CONFIRMED_SUCCESS, VerificationOutcome.SECURITY_FAILURE)
                    else ReviewCaseSeverity.HIGH
                ),
                summary="Provider verification evidence requires controlled financial follow-up.",
                correlation_id=claim.correlation_id,
            )

        if enqueue_apply_work:
            enqueue_verification_work(
                transaction_obj=transaction_obj,
                provider_event=work.provider_event,
                work_type=VerificationWorkType.APPLY_VERIFIED_FUNDS,
                deterministic_identity=f"apply-verified-funds:{transaction_obj.public_id}",
                correlation_id=claim.correlation_id,
                causation_id=verification.public_id,
            )

        work.status = (
            VerificationWorkStatus.WAITING
            if outcome in (VerificationOutcome.PENDING, VerificationOutcome.NO_EFFECT_RETRYABLE)
            else VerificationWorkStatus.COMPLETED
        )
        work.claim_token = None
        work.claimed_at = None
        work.claim_expires_at = None
        work.completed_at = None if work.status == VerificationWorkStatus.WAITING else now
        work.next_attempt_at = now + timedelta(minutes=5)
        work.last_error_classification = error_classification
        work.version += 1
        work.save(
            update_fields=(
                "status",
                "claim_token",
                "claimed_at",
                "claim_expires_at",
                "completed_at",
                "next_attempt_at",
                "last_error_classification",
                "version",
                "updated_at",
            )
        )
        _complete_idempotency(
            scope=scope,
            key=result_idempotency_key,
            request_hash=result_fingerprint,
            result_type=verification._meta.label_lower,
            result_id=verification.pk,
            safe_response={"verification_public_id": str(verification.public_id), "outcome": outcome},
        )
        register_lock(LockRank.EVENT_OUTBOX, "verification-result-event")
        append_financial_event(
            aggregate_type=transaction_obj._meta.label_lower,
            aggregate_id=transaction_obj.public_id,
            aggregate_version=transaction_obj.version,
            event_type="provider_verification.recorded",
            actor_type=actor_type,
            actor_id=actor_id,
            idempotency_key=f"verification-result:{result_idempotency_key}",
            correlation_id=claim.correlation_id,
            causation_id=claim.causation_id,
            metadata={
                "new_status": transaction_obj.status,
                "provider": transaction_obj.provider,
                "reason_code": error_classification or outcome,
            },
        )
        if review:
            append_financial_event(
                aggregate_type=review._meta.label_lower,
                aggregate_id=review.public_id,
                aggregate_version=review.version,
                event_type=("review_case.opened" if review_created else "review_case.escalated"),
                actor_type=FinancialActorType.SYSTEM,
                idempotency_key=f"verification-review:{verification.public_id}",
                correlation_id=claim.correlation_id,
                causation_id=verification.public_id,
                metadata={"reason_code": review.reason, "severity": review.severity},
            )
        append_outbox_message(
            topic="provider.verification.recorded",
            aggregate_type=transaction_obj._meta.label_lower,
            aggregate_id=transaction_obj.public_id,
            idempotency_key=f"outbox:verification-result:{result_idempotency_key}",
            correlation_id=claim.correlation_id,
            causation_id=verification.public_id,
            payload={
                "event_type": "provider_verification.recorded",
                "provider": transaction_obj.provider,
                "transaction_public_id": str(transaction_obj.public_id),
                "new_status": transaction_obj.status,
                "reason_code": error_classification or outcome,
            },
        )
        return verification
