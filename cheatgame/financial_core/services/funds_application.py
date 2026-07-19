import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID, uuid4, uuid5

from django.core.exceptions import ValidationError
from django.db import DatabaseError, transaction
from django.db.models import Sum
from django.utils import timezone

from cheatgame.financial_core.models import (
    CANONICAL_CURRENCY,
    CommercialFinalizationWorkItem,
    FinancialActorType,
    FinancialAllocation,
    FinancialEvent,
    FinalizationWorkStatus,
    IdempotencyRecord,
    IdempotencyStatus,
    Payment,
    PaymentAttempt,
    PaymentAttemptStatus,
    PaymentCollectionStatus,
    PaymentTransaction,
    PaymentTransactionOperation,
    PaymentTransactionStatus,
    PostingDirection,
    ProviderReferenceAllocation,
    ReceiptAccountingPolicyVersion,
    ReviewCase,
    ReviewCaseReason,
    ReviewCaseSeverity,
    ReviewCaseStatus,
    Verification,
    VerificationApplicationState,
    VerificationEvidenceBasis,
    VerificationFinality,
    VerificationFinancialEffect,
    VerificationOutcome,
    VerificationTransportClassification,
    VerificationWorkItem,
    VerificationWorkStatus,
    VerificationWorkType,
)
from cheatgame.financial_core.services.events import append_financial_event
from cheatgame.financial_core.services.idempotency import IdempotencyConflict, canonical_request_hash
from cheatgame.financial_core.services.journal import post_balanced_journal_entry_under_lock
from cheatgame.financial_core.services.locks import (
    LockRank,
    lock_many,
    lock_one,
    ordered_lock_scope,
    register_lock,
)
from cheatgame.financial_core.services.outbox import append_outbox_message
from cheatgame.financial_core.services.state_machines import (
    assert_payment_attempt_transition,
    assert_payment_transaction_transition,
    assert_payment_transition,
)
from cheatgame.financial_core.services.verification_worker import (
    VerificationInterpretationState,
    derive_current_verification_interpretation,
)
from cheatgame.shop.models import Order


APPLICATION_NAMESPACE = UUID("fe3600d3-e223-4263-85fd-f8aa8f55bed4")
FINALIZER_VERSION = "commercial-finalizer-v1-dormant"
RECOGNITION_CONTRACT_VERSION = "funds-recognition-v1"
RECEIPT_SOURCE_TYPE = "provider_receipt"


class FundsApplicationBlocked(ValidationError):
    def __init__(self, message, *, review_reason=ReviewCaseReason.VERIFIED_FUNDS_APPLICATION_FAILED):
        super().__init__(message)
        self.review_reason = review_reason


@dataclass(frozen=True)
class FundsApplicationResult:
    allocation: FinancialAllocation
    replayed: bool


def _deterministic_uuid(value):
    return uuid5(APPLICATION_NAMESPACE, str(value))


def _fingerprint(payload):
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _lock_graph(verification_id):
    ref = Verification.objects.select_related("transaction__attempt__payment").only(
        "transaction_id",
        "transaction__attempt_id",
        "transaction__attempt__payment_id",
        "transaction__attempt__payment__order_id",
    ).get(pk=verification_id)
    order = lock_one(
        queryset=Order.objects.all(),
        rank=LockRank.PAYABLE,
        pk=ref.transaction.attempt.payment.order_id,
    )
    payment = lock_one(
        queryset=Payment.objects.all(),
        rank=LockRank.PAYMENT,
        pk=ref.transaction.attempt.payment_id,
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
    verifications = lock_many(
        queryset=Verification.objects.all(),
        rank=LockRank.FINANCIAL_EVIDENCE,
        pks=Verification.objects.filter(transaction__attempt__payment=payment).values_list("pk", flat=True),
    )
    attempt = next(item for item in attempts if item.pk == ref.transaction.attempt_id)
    transaction_obj = next(item for item in transactions if item.pk == ref.transaction_id)
    verification = next(item for item in verifications if item.pk == verification_id)
    return order, payment, attempt, transaction_obj, verification


def _validate_exact_success(*, payment, attempt, transaction_obj, verification):
    interpretation = derive_current_verification_interpretation(transaction_id=transaction_obj.pk)
    if (
        interpretation.state != VerificationInterpretationState.ELIGIBLE_FINAL_PAID
        or interpretation.controlling_verification is None
        or interpretation.controlling_verification.pk != verification.pk
    ):
        raise FundsApplicationBlocked("Current Verification interpretation is not recognition eligible.")
    if verification.normalized_outcome != VerificationOutcome.CONFIRMED_SUCCESS:
        raise FundsApplicationBlocked("Only confirmed-success Verification evidence is eligible.")
    if verification.normalized_financial_effect != VerificationFinancialEffect.PAID:
        raise FundsApplicationBlocked("Verification does not prove a paid financial effect.")
    if verification.finality != VerificationFinality.FINAL:
        raise FundsApplicationBlocked("Non-final success evidence cannot recognize funds.")
    if verification.transport_classification != VerificationTransportClassification.SUCCESS:
        raise FundsApplicationBlocked("Successful provider transport evidence is required.")
    if verification.evidence_basis not in (
        VerificationEvidenceBasis.SERVER_TO_SERVER,
        VerificationEvidenceBasis.AUTHENTICATED_SETTLEMENT,
    ):
        raise FundsApplicationBlocked("Authenticated server evidence is required.")
    if verification.application_state != VerificationApplicationState.APPLIED_BLOCKING_SUCCESS:
        raise FundsApplicationBlocked("Verification is not in the unapplied blocking-success projection.")
    if verification.transaction_id != transaction_obj.pk or transaction_obj.attempt_id != attempt.pk:
        raise FundsApplicationBlocked(
            "Verification ownership is inconsistent.",
            review_reason=ReviewCaseReason.FINANCIAL_INVARIANT_VIOLATION,
        )
    if attempt.payment_id != payment.pk:
        raise FundsApplicationBlocked(
            "Attempt ownership is inconsistent.",
            review_reason=ReviewCaseReason.FINANCIAL_INVARIANT_VIOLATION,
        )
    exact_pairs = (
        (verification.provider_id, transaction_obj.capability_version.provider_id),
        (verification.capability_version_id, transaction_obj.capability_version_id),
        (verification.merchant_account_version_id, transaction_obj.merchant_account_version_id),
        (verification.adapter_contract_version, transaction_obj.adapter_contract_version),
        (verification.merchant_reference, transaction_obj.merchant_reference),
        (verification.provider_authority, transaction_obj.provider_authority or ""),
        (verification.provider_reference, transaction_obj.provider_reference or ""),
        (verification.operation_type, transaction_obj.operation_type),
        (verification.requested_provider_amount, transaction_obj.provider_amount),
        (verification.requested_provider_unit, transaction_obj.provider_unit),
        (verification.observed_provider_amount, transaction_obj.provider_amount),
        (verification.observed_provider_unit, transaction_obj.provider_unit),
        (verification.canonical_allocation_amount, transaction_obj.amount),
        (verification.canonical_currency, transaction_obj.currency),
        (payment.currency, CANONICAL_CURRENCY),
    )
    if any(left != right for left, right in exact_pairs):
        raise FundsApplicationBlocked(
            "Verification no longer exactly matches its immutable provider operation.",
            review_reason=ReviewCaseReason.FINANCIAL_INVARIANT_VIOLATION,
        )
    if transaction_obj.operation_type not in (
        PaymentTransactionOperation.SALE,
        PaymentTransactionOperation.CAPTURE,
    ):
        raise FundsApplicationBlocked("Only sale or capture financial effects can recognize receipt funds.")
    if not verification.provider_reference:
        raise FundsApplicationBlocked("A trustworthy provider reference is required.")
    ownership = ProviderReferenceAllocation.objects.filter(
        merchant_account_version_id=verification.merchant_account_version_id,
        provider_reference=verification.provider_reference,
    ).first()
    if (
        ownership is None
        or ownership.transaction_id != transaction_obj.pk
        or ownership.verification_id != verification.pk
    ):
        raise FundsApplicationBlocked(
            "Provider-reference ownership is missing or conflicting.",
            review_reason=ReviewCaseReason.DUPLICATE_FINANCIAL_ALLOCATION,
        )
    contradiction = Verification.objects.filter(
        transaction__attempt__payment=payment,
        application_state=VerificationApplicationState.REVIEW_REQUIRED,
    ).exclude(pk=verification.pk).exists()
    if contradiction:
        raise FundsApplicationBlocked(
            "Contradictory provider evidence requires review before recognition.",
            review_reason=ReviewCaseReason.FINANCIAL_INVARIANT_VIOLATION,
        )
    if attempt.status == PaymentAttemptStatus.DEFINITIVE_FAILED or transaction_obj.status in (
        PaymentTransactionStatus.DECLINED,
        PaymentTransactionStatus.CANCELED,
        PaymentTransactionStatus.EXPIRED,
    ):
        raise FundsApplicationBlocked("Late success evidence requires controlled review before recognition.")
    if payment.collection_status == PaymentCollectionStatus.CANCELED:
        raise FundsApplicationBlocked("Canceled Payment cannot be recognized automatically.")


def _active_policy(transaction_obj):
    policies = lock_many(
        queryset=ReceiptAccountingPolicyVersion.objects.all(),
        rank=LockRank.ACCOUNTING_POLICY,
        pks=ReceiptAccountingPolicyVersion.objects.filter(
            merchant_account_version_id=transaction_obj.merchant_account_version_id,
            active_for_new_applications=True,
        ).values_list("pk", flat=True),
    )
    if len(policies) != 1:
        raise FundsApplicationBlocked(
            "Exactly one active receipt accounting policy is required.",
            review_reason=ReviewCaseReason.ACCOUNTING_POLICY_MISSING,
        )
    return policies[0]


def _lock_reviews(payment):
    return lock_many(
        queryset=ReviewCase.objects.all(),
        rank=LockRank.REVIEW_CASE,
        pks=ReviewCase.objects.filter(payment=payment).values_list("pk", flat=True),
    )


def _ensure_review_locked(*, order, payment, attempt, transaction_obj, reviews, reason, severity, correlation_id):
    equivalent_reasons = {reason}
    if reason == ReviewCaseReason.PAID_PENDING_FINALIZATION:
        equivalent_reasons.add(ReviewCaseReason.PAID_FINALIZATION_PENDING)
    unresolved = next(
        (
            item
            for item in reviews
            if item.reason in equivalent_reasons
            and item.status in (
                ReviewCaseStatus.OPEN,
                ReviewCaseStatus.INVESTIGATING,
                ReviewCaseStatus.APPROVAL_PENDING,
            )
        ),
        None,
    )
    if unresolved is not None:
        unresolved.version += 1
        unresolved.severity = severity
        unresolved.save(update_fields=("severity", "version", "updated_at"))
        review = unresolved
        event_type = "review_case.escalated"
    else:
        register_lock(LockRank.REVIEW_CASE, f"new:{payment.pk:020d}:{reason}")
        review = ReviewCase.objects.create(
            reason=reason,
            severity=severity,
            order=order,
            payment=payment,
            attempt=attempt,
            transaction=transaction_obj,
            opened_by_type=FinancialActorType.SYSTEM,
            summary="Verified provider funds require controlled financial or commercial follow-up.",
            idempotency_key=_deterministic_uuid(f"review:{payment.public_id}:{reason}"),
        )
        event_type = "review_case.opened"
    register_lock(LockRank.EVENT_OUTBOX, f"000-review-event:{review.pk:020d}:{review.version:020d}")
    append_financial_event(
        aggregate_type=review._meta.label_lower,
        aggregate_id=review.public_id,
        aggregate_version=review.version,
        event_type=event_type,
        actor_type=FinancialActorType.SYSTEM,
        idempotency_key=f"funds-application-review:{review.public_id}:{review.version}",
        correlation_id=correlation_id,
        metadata={"reason_code": reason, "severity": severity},
    )
    return review


@transaction.atomic
def _record_application_failure(*, verification_id, reason, correlation_id):
    with ordered_lock_scope():
        order, payment, attempt, transaction_obj, verification = _lock_graph(verification_id)
        if FinancialAllocation.objects.filter(transaction=transaction_obj).exists():
            return
        reviews = _lock_reviews(payment)
        _ensure_review_locked(
            order=order,
            payment=payment,
            attempt=attempt,
            transaction_obj=transaction_obj,
            reviews=reviews,
            reason=reason,
            severity=ReviewCaseSeverity.CRITICAL,
            correlation_id=correlation_id or verification.correlation_id,
        )


@transaction.atomic
def _apply_verified_funds_atomic(
    *,
    verification_id,
    idempotency_key,
    expected_payment_version,
    correlation_id,
    causation_id,
    actor_type,
    actor_id,
):
    with ordered_lock_scope():
        order, payment, attempt, transaction_obj, verification = _lock_graph(verification_id)
        command_payload = {
            "contract_version": RECOGNITION_CONTRACT_VERSION,
            "verification_public_id": str(verification.public_id),
            "verification_id": verification.pk,
            "payment_public_id": str(payment.public_id),
            "payment_id": payment.pk,
            "expected_payment_version": int(expected_payment_version),
            "actor_type": actor_type,
        }
        application_fingerprint = canonical_request_hash(command_payload)
        if actor_type not in (FinancialActorType.SYSTEM, FinancialActorType.RECONCILIATION):
            raise FundsApplicationBlocked("Only controlled system financial actors may apply verified funds.")
        if actor_type == FinancialActorType.SYSTEM and actor_id is not None:
            raise FundsApplicationBlocked("SYSTEM recognition cannot carry a user actor.")
        if actor_type == FinancialActorType.RECONCILIATION and actor_id is None:
            raise FundsApplicationBlocked("RECONCILIATION recognition requires an accountable actor identity.")
        existing_by_key = FinancialAllocation.objects.filter(
            application_idempotency_key=idempotency_key
        ).first()
        if existing_by_key is not None:
            if existing_by_key.application_fingerprint != application_fingerprint:
                raise IdempotencyConflict("Funds-application idempotency key conflicts with another command.")
            return FundsApplicationResult(existing_by_key, True)
        existing_source = FinancialAllocation.objects.filter(transaction=transaction_obj).first()
        if existing_source is not None:
            if (
                existing_source.payment_id != payment.pk
                or existing_source.provider_reference != verification.provider_reference
                or existing_source.amount != verification.canonical_allocation_amount
            ):
                raise FundsApplicationBlocked(
                    "Provider financial effect is already allocated inconsistently.",
                    review_reason=ReviewCaseReason.DUPLICATE_FINANCIAL_ALLOCATION,
                )
            if existing_source.verification_id != verification.pk:
                _validate_exact_success(
                    payment=payment,
                    attempt=attempt,
                    transaction_obj=transaction_obj,
                    verification=verification,
                )
            return FundsApplicationResult(existing_source, True)
        if payment.version != int(expected_payment_version):
            raise FundsApplicationBlocked("Payment version changed before funds application.")
        if payment.collection_status not in (
            PaymentCollectionStatus.PROCESSING,
            PaymentCollectionStatus.REVIEW,
        ):
            raise FundsApplicationBlocked("Payment is not in an eligible pre-recognition state.")
        _validate_exact_success(
            payment=payment,
            attempt=attempt,
            transaction_obj=transaction_obj,
            verification=verification,
        )
        previous_allocated = FinancialAllocation.objects.filter(payment=payment).aggregate(total=Sum("amount"))["total"] or Decimal("0")
        if payment.confirmed_amount != previous_allocated:
            raise FundsApplicationBlocked(
                "Payment confirmed amount does not reconcile to immutable allocations.",
                review_reason=ReviewCaseReason.FINANCIAL_INVARIANT_VIOLATION,
            )
        allocation_amount = verification.canonical_allocation_amount
        projected_amount = previous_allocated + allocation_amount
        if projected_amount > payment.amount_due:
            raise FundsApplicationBlocked(
                "Verified provider funds exceed the Payment obligation.",
                review_reason=ReviewCaseReason.OVERPAYMENT,
            )
        if projected_amount < payment.amount_due:
            raise FundsApplicationBlocked(
                "Split tender is disabled; partial provider funds require review.",
                review_reason=ReviewCaseReason.VERIFIED_FUNDS_APPLICATION_FAILED,
            )
        if attempt.requested_amount != allocation_amount:
            raise FundsApplicationBlocked("Successful Attempt amount must equal the financial allocation.")

        policy = _active_policy(transaction_obj)
        allocation_public_id = uuid4()
        journal_idempotency_key = _deterministic_uuid(f"receipt-journal:{allocation_public_id}")
        try:
            journal = post_balanced_journal_entry_under_lock(
                source_type=RECEIPT_SOURCE_TYPE,
                source_id=allocation_public_id,
                idempotency_key=journal_idempotency_key,
                correlation_id=correlation_id,
                occurred_at=verification.verified_at,
                description="Provider receipt recognized into unapplied customer funds.",
                postings=(
                    {
                        "account_id": policy.provider_clearing_account_id,
                        "direction": PostingDirection.DEBIT,
                        "amount": allocation_amount,
                        "currency": CANONICAL_CURRENCY,
                        "memo": "Provider clearing receipt",
                    },
                    {
                        "account_id": policy.customer_unapplied_funds_account_id,
                        "direction": PostingDirection.CREDIT,
                        "amount": allocation_amount,
                        "currency": CANONICAL_CURRENCY,
                        "memo": "Customer funds pending commercial finalization",
                    },
                ),
            )
        except (ValidationError, DatabaseError) as exc:
            raise FundsApplicationBlocked(
                "Provider receipt Journal could not be posted.",
                review_reason=ReviewCaseReason.PROVIDER_RECEIPT_JOURNAL_FAILED,
            ) from exc
        allocation = FinancialAllocation.objects.create(
            public_id=allocation_public_id,
            payment=payment,
            attempt=attempt,
            transaction=transaction_obj,
            verification=verification,
            merchant_account_version=transaction_obj.merchant_account_version,
            accounting_policy_version=policy,
            journal_entry=journal,
            provider_reference=verification.provider_reference,
            amount=allocation_amount,
            currency=CANONICAL_CURRENCY,
            application_idempotency_key=idempotency_key,
            application_fingerprint=application_fingerprint,
            correlation_id=correlation_id,
            causation_id=causation_id or verification.public_id,
        )

        assert_payment_transaction_transition(transaction_obj.status, PaymentTransactionStatus.SUCCEEDED)
        assert_payment_attempt_transition(attempt.status, PaymentAttemptStatus.SUCCEEDED)
        assert_payment_transition(payment.collection_status, PaymentCollectionStatus.PAID_PENDING_FINALIZATION)
        now = timezone.now()
        transaction_obj.status = PaymentTransactionStatus.SUCCEEDED
        transaction_obj.completed_at = now
        transaction_obj.version += 1
        transaction_obj.save(update_fields=("status", "completed_at", "version", "updated_at"))
        attempt.status = PaymentAttemptStatus.SUCCEEDED
        attempt.version += 1
        attempt.save(update_fields=("status", "version", "updated_at"))
        payment.confirmed_amount = projected_amount
        payment.collection_status = PaymentCollectionStatus.PAID_PENDING_FINALIZATION
        payment.version += 1
        payment.save(update_fields=("confirmed_amount", "collection_status", "version", "updated_at"))

        reviews = _lock_reviews(payment)
        blocking_reasons = {
            ReviewCaseReason.AMOUNT_MISMATCH,
            ReviewCaseReason.CURRENCY_MISMATCH,
            ReviewCaseReason.DUPLICATE_PROVIDER_REFERENCE,
            ReviewCaseReason.FRAUD_RISK,
            ReviewCaseReason.INVARIANT_VIOLATION,
            ReviewCaseReason.DUPLICATE_FINANCIAL_ALLOCATION,
            ReviewCaseReason.OVERPAYMENT,
            ReviewCaseReason.FINANCIAL_INVARIANT_VIOLATION,
        }
        if any(
            item.status in (ReviewCaseStatus.OPEN, ReviewCaseStatus.INVESTIGATING, ReviewCaseStatus.APPROVAL_PENDING)
            and item.reason in blocking_reasons
            for item in reviews
        ):
            raise FundsApplicationBlocked("An unresolved financial ReviewCase blocks recognition.")
        _ensure_review_locked(
            order=order,
            payment=payment,
            attempt=attempt,
            transaction_obj=transaction_obj,
            reviews=reviews,
            reason=ReviewCaseReason.PAID_PENDING_FINALIZATION,
            severity=ReviewCaseSeverity.HIGH,
            correlation_id=correlation_id,
        )

        register_lock(LockRank.EVENT_OUTBOX, f"100-application:{allocation.pk:020d}")
        try:
            CommercialFinalizationWorkItem.objects.get_or_create(
                payment=payment,
                finalizer_version=FINALIZER_VERSION,
                defaults={
                    "deterministic_identity": f"commercial-finalization:{payment.public_id}:{FINALIZER_VERSION}",
                    "status": FinalizationWorkStatus.PENDING,
                    "correlation_id": correlation_id,
                    "causation_id": allocation.public_id,
                },
            )
        except DatabaseError as exc:
            raise FundsApplicationBlocked(
                "Commercial-finalization work could not be created atomically.",
                review_reason=ReviewCaseReason.VERIFIED_FUNDS_APPLICATION_FAILED,
            ) from exc
        VerificationWorkItem.objects.filter(
            transaction=transaction_obj,
            work_type=VerificationWorkType.APPLY_VERIFIED_FUNDS,
            status__in=(VerificationWorkStatus.PENDING, VerificationWorkStatus.WAITING),
        ).update(
            status=VerificationWorkStatus.COMPLETED,
            completed_at=now,
            claim_token=None,
            claimed_at=None,
            claim_expires_at=None,
        )
        for aggregate, event_type, command_suffix in (
            (transaction_obj, "provider_funds.applied", "transaction"),
            (attempt, "payment_attempt.succeeded", "attempt"),
            (payment, "payment.paid_pending_finalization", "payment"),
            (allocation, "financial_allocation.created", "allocation"),
        ):
            append_financial_event(
                aggregate_type=aggregate._meta.label_lower,
                aggregate_id=aggregate.public_id,
                aggregate_version=getattr(aggregate, "version", 1),
                event_type=event_type,
                actor_type=actor_type,
                actor_id=actor_id,
                idempotency_key=f"funds-application:{idempotency_key}:{command_suffix}",
                correlation_id=correlation_id,
                causation_id=causation_id or verification.public_id,
                metadata={
                    "new_status": getattr(aggregate, "status", getattr(aggregate, "collection_status", "applied")),
                    "amount": allocation_amount,
                    "currency": CANONICAL_CURRENCY,
                    "provider": transaction_obj.provider,
                },
            )
        append_outbox_message(
            topic="commercial.finalization.requested",
            aggregate_type=payment._meta.label_lower,
            aggregate_id=payment.public_id,
            idempotency_key=f"outbox:commercial-finalization:{payment.public_id}:{FINALIZER_VERSION}",
            correlation_id=correlation_id,
            causation_id=allocation.public_id,
            payload={
                "event_type": "commercial_finalization.requested",
                "payment_public_id": str(payment.public_id),
                "new_status": payment.collection_status,
            },
        )
        IdempotencyRecord.objects.create(
            scope="financial_core:apply_verified_funds",
            key=str(idempotency_key),
            request_hash=application_fingerprint,
            status=IdempotencyStatus.COMPLETED,
            result_type=allocation._meta.label_lower,
            result_id=str(allocation.pk),
            safe_response={
                "allocation_public_id": str(allocation.public_id),
                "payment_status": payment.collection_status,
            },
            completed_at=now,
        )
        return FundsApplicationResult(allocation, False)


def recognize_verified_funds(
    *,
    verification_id,
    idempotency_key,
    expected_payment_version,
    correlation_id,
    causation_id=None,
    actor_type=FinancialActorType.SYSTEM,
    actor_id=None,
):
    """Recognize exact authenticated provider funds; no commercial mutation occurs here."""
    try:
        return _apply_verified_funds_atomic(
            verification_id=verification_id,
            idempotency_key=idempotency_key,
            expected_payment_version=expected_payment_version,
            correlation_id=correlation_id,
            causation_id=causation_id,
            actor_type=actor_type,
            actor_id=actor_id,
        )
    except FundsApplicationBlocked as exc:
        _record_application_failure(
            verification_id=verification_id,
            reason=exc.review_reason,
            correlation_id=correlation_id,
        )
        raise
    except DatabaseError:
        _record_application_failure(
            verification_id=verification_id,
            reason=ReviewCaseReason.FINANCIAL_INVARIANT_VIOLATION,
            correlation_id=correlation_id,
        )
        raise


def apply_verified_funds(**kwargs):
    """Backward-compatible internal alias for the frozen Funds Recognition command."""
    return recognize_verified_funds(**kwargs)
