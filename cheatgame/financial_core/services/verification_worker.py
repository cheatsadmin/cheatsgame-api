import hashlib
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from uuid import UUID, uuid5

from django.conf import settings
from django.core.exceptions import ValidationError

from cheatgame.financial_core.models import (
    CallbackAuthenticationStatus,
    CallbackAuthenticationStrength,
    CallbackReplayWindowStatus,
    MoneyUnit,
    ProviderEventResolutionStatus,
    Verification,
    VerificationApplicationState,
    VerificationEvidenceBasis,
    VerificationFinality,
    VerificationFinancialEffect,
    VerificationOutcome,
    VerificationTransportClassification,
    VerificationTriggerSource,
    VerificationWorkItem,
    VerificationWorkType,
)
from cheatgame.financial_core.services.adapters import (
    NormalizedVerificationResult,
    PRODUCTION_ADAPTER_REGISTRY,
    execute_verification_outside_transaction,
)
from cheatgame.financial_core.services.idempotency import canonical_request_hash
from cheatgame.financial_core.services.verification import (
    VerificationBlocked,
    apply_verification_result,
    claim_verification_work,
)


VERIFICATION_WORKER_NAMESPACE = UUID("3497d926-9ac8-43a0-9e7d-64a005999d67")
EXECUTABLE_WORK_TYPES = frozenset(
    {
        VerificationWorkType.VERIFY_AFTER_CALLBACK,
        VerificationWorkType.VERIFY_AFTER_BROWSER_HINT,
        VerificationWorkType.POLL_PENDING_OPERATION,
        VerificationWorkType.VERIFY_UNKNOWN_OUTCOME,
        VerificationWorkType.RETRY_PROVIDER_QUERY,
        VerificationWorkType.ESCALATE_UNKNOWN_OUTCOME,
    }
)


@dataclass(frozen=True)
class VerificationWorkerResult:
    verification: Verification
    replayed: bool
    used_provider_query: bool


class VerificationInterpretationState:
    UNVERIFIED = "unverified"
    WAITING = "waiting"
    FINAL_UNPAID = "final_unpaid"
    ELIGIBLE_FINAL_PAID = "eligible_final_paid"
    BLOCKED_REVIEW = "blocked_review"


@dataclass(frozen=True)
class VerificationInterpretation:
    state: str
    controlling_verification: Verification = None


def derive_current_verification_interpretation(*, transaction_id):
    """Derive policy interpretation from immutable history; never use last-write-wins."""
    observations = Verification.objects.filter(transaction_id=transaction_id)
    review = observations.filter(
        application_state=VerificationApplicationState.REVIEW_REQUIRED
    ).order_by("-sequence", "-pk").first()
    if review is not None:
        return VerificationInterpretation(VerificationInterpretationState.BLOCKED_REVIEW, review)
    success = observations.filter(
        application_state=VerificationApplicationState.APPLIED_BLOCKING_SUCCESS,
        normalized_outcome=VerificationOutcome.CONFIRMED_SUCCESS,
        normalized_financial_effect=VerificationFinancialEffect.PAID,
        finality=VerificationFinality.FINAL,
        evidence_basis__in=(
            VerificationEvidenceBasis.SERVER_TO_SERVER,
            VerificationEvidenceBasis.AUTHENTICATED_SETTLEMENT,
        ),
        observed_provider_amount__isnull=False,
    ).exclude(provider_reference="").filter(
        provider_reference_allocations__transaction_id=transaction_id,
    ).order_by("-sequence", "-pk").first()
    if success is not None:
        return VerificationInterpretation(
            VerificationInterpretationState.ELIGIBLE_FINAL_PAID,
            success,
        )
    unpaid = observations.filter(
        application_state=VerificationApplicationState.APPLIED_UNPAID
    ).order_by("-sequence", "-pk").first()
    if unpaid is not None:
        return VerificationInterpretation(VerificationInterpretationState.FINAL_UNPAID, unpaid)
    latest = observations.order_by("-sequence", "-pk").first()
    if latest is None:
        return VerificationInterpretation(VerificationInterpretationState.UNVERIFIED)
    return VerificationInterpretation(
        VerificationInterpretationState.WAITING,
        latest,
    )


def _stage_key(*, root_key, work, transaction_obj, stage):
    frozen_identity = canonical_request_hash(
        {
            "root_key": str(root_key),
            "work_public_id": str(work.public_id),
            "transaction_public_id": str(transaction_obj.public_id),
            "provider_id": transaction_obj.capability_version.provider_id,
            "merchant_account_version_id": transaction_obj.merchant_account_version_id,
            "capability_version_id": transaction_obj.capability_version_id,
            "operation_type": transaction_obj.operation_type,
            "stage": stage,
        }
    )
    return uuid5(VERIFICATION_WORKER_NAMESPACE, frozen_identity)


def _failure_result(*, envelope, outcome, error_classification, retryable):
    evidence_hash = hashlib.sha256(
        (
            f"{envelope.transaction_public_id}:{envelope.claim_token}:"
            f"{outcome}:{error_classification}"
        ).encode("utf-8")
    ).hexdigest()
    return NormalizedVerificationResult(
        outcome=outcome,
        financial_effect=VerificationFinancialEffect.UNKNOWN,
        finality=VerificationFinality.UNKNOWN,
        transport_classification=VerificationTransportClassification.NOT_EXECUTED,
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
        retryable=retryable,
        evidence_basis=VerificationEvidenceBasis.NONE,
    )


def _callback_final_receipt(work):
    event = work.provider_event
    transaction_obj = work.transaction
    capability = transaction_obj.capability_version
    if (
        event is None
        or not capability.callback_verification_is_final
        or event.resolution_status != ProviderEventResolutionStatus.VERIFICATION_REQUIRED
        or event.transaction_id != transaction_obj.pk
        or event.provider_id != capability.provider_id
        or event.capability_version_id != capability.pk
        or event.merchant_account_version_id != transaction_obj.merchant_account_version_id
        or capability.callback_authentication == CallbackAuthenticationStrength.NONE
        or event.authentication_strength != capability.callback_authentication
        or not event.provider_event_id
        or not event.merchant_reference
        or not event.provider_reference
        or not event.operation_type_hint
        or event.provider_amount_hint is None
        or event.provider_unit_hint not in MoneyUnit.values
        or event.financial_effect_hint
        not in (VerificationFinancialEffect.PAID, VerificationFinancialEffect.UNPAID)
        or event.finality_hint != VerificationFinality.FINAL
        or event.provider_occurred_at is None
        or not capability.callback_authentication_method
        or not capability.callback_authentication_version
        or not transaction_obj.merchant_account_version.callback_signing_key_reference_hash
    ):
        return None
    link = event.receipt_links.select_related("callback_receipt").filter(
        callback_receipt__provider_id=event.provider_id,
        callback_receipt__capability_version_id=event.capability_version_id,
        callback_receipt__merchant_account_version_id=event.merchant_account_version_id,
        callback_receipt__authentication_status=CallbackAuthenticationStatus.AUTHENTICATED,
        callback_receipt__authentication_strength=capability.callback_authentication,
        callback_receipt__authentication_method=capability.callback_authentication_method,
        callback_receipt__authentication_version=capability.callback_authentication_version,
        callback_receipt__signing_key_reference_hash=(
            transaction_obj.merchant_account_version.callback_signing_key_reference_hash
        ),
        callback_receipt__replay_window_status=CallbackReplayWindowStatus.VALID,
    ).order_by("pk").first()
    return link.callback_receipt if link is not None else None


def _authenticated_callback_is_sufficient(work):
    return _callback_final_receipt(work) is not None


def _callback_result_mismatch(*, event, result):
    try:
        observed_amount = Decimal(str(result.observed_provider_amount))
    except (InvalidOperation, TypeError, ValueError):
        return "callback_provider_amount_missing"
    exact_pairs = (
        (result.merchant_reference, event.merchant_reference, "callback_merchant_reference_mismatch"),
        (result.provider_reference, event.provider_reference, "callback_provider_reference_mismatch"),
        (result.operation_type, event.operation_type_hint, "callback_operation_mismatch"),
        (observed_amount, event.provider_amount_hint, "callback_provider_amount_mismatch"),
        (str(result.observed_provider_unit).upper(), event.provider_unit_hint, "callback_provider_unit_mismatch"),
        (result.financial_effect, event.financial_effect_hint, "callback_financial_effect_mismatch"),
        (result.finality, event.finality_hint, "callback_finality_mismatch"),
        (result.provider_occurred_at, event.provider_occurred_at, "callback_occurrence_mismatch"),
    )
    for actual, expected, reason in exact_pairs:
        if actual != expected:
            return reason
    if result.evidence_basis != VerificationEvidenceBasis.AUTHENTICATED_SETTLEMENT:
        return "callback_evidence_basis_not_settlement_grade"
    return ""


def execute_verification_work_item(
    *,
    work_item_id,
    trigger_source,
    execution_idempotency_key,
    adapter_registry=PRODUCTION_ADAPTER_REGISTRY,
    lease_seconds=60,
    retry_after_seconds=None,
):
    """Execute one dormant Financial Truth Verification unit without recognizing funds."""
    if trigger_source not in VerificationTriggerSource.values:
        raise ValidationError("Unsupported verification trigger source.")
    work = VerificationWorkItem.objects.select_related(
        "transaction__attempt__payment",
        "transaction__capability_version__provider",
        "transaction__merchant_account_version",
        "provider_event",
    ).get(pk=work_item_id)
    if work.work_type not in EXECUTABLE_WORK_TYPES:
        raise VerificationBlocked("This work type belongs to a different financial boundary.")
    transaction_obj = work.transaction
    root_key = UUID(str(execution_idempotency_key))
    claim_key = _stage_key(
        root_key=root_key,
        work=work,
        transaction_obj=transaction_obj,
        stage="claim",
    )
    result_key = _stage_key(
        root_key=root_key,
        work=work,
        transaction_obj=transaction_obj,
        stage="result",
    )
    claim_result = claim_verification_work(
        work_item_id=work.pk,
        trigger_source=trigger_source,
        claim_idempotency_key=claim_key,
        lease_seconds=lease_seconds,
    )
    replay = Verification.objects.filter(
        claim=claim_result.claim,
        result_idempotency_key=result_key,
    ).first()
    if replay is not None:
        return VerificationWorkerResult(
            verification=replay,
            replayed=True,
            used_provider_query=not _authenticated_callback_is_sufficient(work),
        )

    envelope = claim_result.envelope
    callback_receipt = _callback_final_receipt(work)
    callback_sufficient = callback_receipt is not None
    if callback_receipt is not None:
        envelope = replace(
            envelope,
            callback_financial_effect=work.provider_event.financial_effect_hint,
            callback_finality=work.provider_event.finality_hint,
            callback_provider_occurred_at=work.provider_event.provider_occurred_at.isoformat(),
            callback_authentication_method=callback_receipt.authentication_method,
            callback_authentication_version=callback_receipt.authentication_version,
            callback_signing_key_reference_hash=callback_receipt.signing_key_reference_hash,
        )
    use_query = not callback_sufficient
    capability = transaction_obj.capability_version
    provider = capability.provider
    account = transaction_obj.merchant_account_version
    if (
        not provider.is_enabled
        or not account.is_enabled
        or account.provider_id != provider.pk
        or account.capability_version_id != capability.pk
        or transaction_obj.operation_type not in capability.supported_operations
    ):
        normalized = _failure_result(
            envelope=envelope,
            outcome=VerificationOutcome.CONFIGURATION_FAILURE,
            error_classification="frozen_provider_policy_unavailable",
            retryable=False,
        )
    elif use_query and not capability.supports_lookup:
        normalized = _failure_result(
            envelope=envelope,
            outcome=VerificationOutcome.CONFIGURATION_FAILURE,
            error_classification="provider_lookup_not_supported",
            retryable=False,
        )
    else:
        try:
            adapter = adapter_registry.resolve(
                adapter_key=capability.adapter_key,
                contract_version=transaction_obj.adapter_contract_version,
            )
        except ValidationError:
            normalized = _failure_result(
                envelope=envelope,
                outcome=VerificationOutcome.CONFIGURATION_FAILURE,
                error_classification="frozen_provider_adapter_unavailable",
                retryable=False,
            )
        else:
            try:
                normalized = execute_verification_outside_transaction(
                    adapter=adapter,
                    envelope=envelope,
                    use_query=use_query,
                )
                if callback_sufficient:
                    mismatch = _callback_result_mismatch(
                        event=work.provider_event,
                        result=normalized,
                    )
                    if mismatch:
                        normalized = replace(
                            normalized,
                            outcome=VerificationOutcome.MISMATCH,
                            error_classification=mismatch,
                            retryable=False,
                        )
            except ValidationError:
                normalized = _failure_result(
                    envelope=envelope,
                    outcome=VerificationOutcome.PROTOCOL_FAILURE,
                    error_classification="malformed_provider_verification_response",
                    retryable=False,
                )

    verification = apply_verification_result(
        claim_token=claim_result.claim.claim_token,
        result=normalized,
        result_idempotency_key=result_key,
        trigger_source=trigger_source,
        truth_only=True,
        retry_after_seconds=(
            retry_after_seconds
            if retry_after_seconds is not None
            else int(getattr(settings, "FINANCIAL_VERIFICATION_RETRY_DELAY_SECONDS", 300))
        ),
    )
    return VerificationWorkerResult(
        verification=verification,
        replayed=False,
        used_provider_query=use_query,
    )
