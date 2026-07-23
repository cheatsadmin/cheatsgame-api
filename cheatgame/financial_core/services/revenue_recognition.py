"""Dormant Revenue Recognition v1 command boundary.

No route, task, signal, scheduler, or outbox consumer invokes this module.
Recognition is point-in-time and consumes only the frozen obligation,
allocation, launch satisfaction evidence, and recognition policy graph.
"""

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid5

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from cheatgame.financial_core.models import (
    CANONICAL_CURRENCY,
    ConsiderationAllocation,
    FinancialAccountStatus,
    FinancialAccountType,
    FinancialActorType,
    IdempotencyRecord,
    IdempotencyStatus,
    PerformanceObligation,
    PerformanceObligationType,
    PostingDirection,
    RecognitionProgressMethod,
    REVENUE_RECOGNITION_ENGINE_CONTRACT,
    RecognitionSatisfactionPattern,
    RevenueRecognition,
    RevenueRecognitionEffect,
    RevenueRecognitionWorkItem,
    RevenueRecognitionWorkPurpose,
    RevenueRecognitionWorkStatus,
    SatisfactionEvidence,
    SatisfactionEvidenceClassification,
)
from cheatgame.financial_core.services.idempotency import canonical_request_hash
from cheatgame.financial_core.services.journal import post_balanced_journal_entry_under_lock
from cheatgame.financial_core.services.locks import (
    LockRank,
    lock_one,
    ordered_lock_scope,
    register_lock,
)
from cheatgame.financial_core.services.satisfaction_evidence import (
    DIGITAL_FULFILLMENT_COMPLETED,
    STANDARD_DELIVERY_COMPLETED,
)
from cheatgame.shop.models import Order


REVENUE_RECOGNITION_CONTRACT = REVENUE_RECOGNITION_ENGINE_CONTRACT
REVENUE_RECOGNITION_PERIOD = "point-in-time"
REVENUE_RECOGNITION_NAMESPACE = UUID("cc34c27a-467c-4520-8503-074020e69f0f")
CLAIM_LEASE = timedelta(minutes=5)
CREATE_SCOPE = "financial_core:revenue_recognition_work"
CLAIM_SCOPE = "financial_core:revenue_recognition_claim"


class RevenueRecognitionError(Exception):
    pass


class RevenueRecognitionConflict(RevenueRecognitionError):
    pass


@dataclass(frozen=True)
class RecognitionWorkResult:
    work_item: RevenueRecognitionWorkItem
    replayed: bool


@dataclass(frozen=True)
class RecognitionClaimResult:
    work_item: RevenueRecognitionWorkItem
    claim_token: UUID = None
    recognition: RevenueRecognition = None
    replayed: bool = False


@dataclass(frozen=True)
class RevenueRecognitionResult:
    recognition: RevenueRecognition
    replayed: bool


def _uuid(value, label):
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise RevenueRecognitionError(f"{label} must be a UUID.") from exc


def _derived_uuid(label, identity):
    return uuid5(REVENUE_RECOGNITION_NAMESPACE, f"{label}:{identity}")


def _expected_evidence_contract(obligation):
    if (
        obligation.commerce_authority == "standard_commerce"
        and obligation.obligation_type == PerformanceObligationType.PHYSICAL_GOOD
    ):
        return STANDARD_DELIVERY_COMPLETED
    if (
        obligation.commerce_authority == "digital_products"
        and obligation.obligation_type == PerformanceObligationType.DIGITAL_ACCESS_INSTALLATION
    ):
        return DIGITAL_FULFILLMENT_COMPLETED
    raise RevenueRecognitionError("The obligation authority is not launch-recognizable.")


def _load_launch_graph(evidence_public_id):
    evidence = SatisfactionEvidence.objects.select_related(
        "obligation__finalization__payment",
        "obligation__recognition_policy_version__contract_liability_account",
        "obligation__recognition_policy_version__revenue_account",
    ).filter(public_id=_uuid(evidence_public_id, "evidence_public_id")).first()
    if evidence is None:
        raise RevenueRecognitionError("Satisfaction evidence was not found.")
    obligation = evidence.obligation
    try:
        allocation = obligation.consideration_allocation
    except ConsiderationAllocation.DoesNotExist as exc:
        raise RevenueRecognitionError("The satisfied obligation has no consideration allocation.") from exc
    return evidence, obligation, allocation, obligation.recognition_policy_version


def _validate_launch_graph(*, evidence, obligation, allocation, policy):
    expected_contract = _expected_evidence_contract(obligation)
    if (
        evidence.obligation_id != obligation.pk
        or evidence.evidence_classification != SatisfactionEvidenceClassification.POINT_IN_TIME_SATISFIED
        or evidence.evidence_contract_version != expected_contract
        or evidence.satisfied_quantity != Decimal(obligation.quantity_basis)
        or evidence.progress_numerator is not None
        or evidence.progress_denominator is not None
        or evidence.corrects_id is not None
        or evidence.contradicts_id is not None
        or SatisfactionEvidence.objects.filter(obligation=obligation).exclude(pk=evidence.pk).exists()
    ):
        raise RevenueRecognitionError("Satisfaction evidence is incomplete or contradictory.")
    if (
        allocation.obligation_id != obligation.pk
        or allocation.finalization_id != obligation.finalization_id
        or allocation.payment_id != obligation.finalization.payment_id
        or allocation.recognition_policy_version_id != policy.pk
        or allocation.contract_liability_account_id != policy.contract_liability_account_id
        or allocation.currency != CANONICAL_CURRENCY
        or allocation.allocated_amount <= 0
    ):
        raise RevenueRecognitionError("Consideration allocation lineage is incoherent.")
    if (
        obligation.recognition_policy_version_id != policy.pk
        or obligation.satisfaction_pattern != RecognitionSatisfactionPattern.POINT_IN_TIME
        or policy.satisfaction_pattern != RecognitionSatisfactionPattern.POINT_IN_TIME
        or policy.progress_measurement_method != RecognitionProgressMethod.NONE
        or policy.evidence_contract_version != "fulfillment-satisfaction-v1"
        or policy.currency != CANONICAL_CURRENCY
        or policy.maximum_recognition_basis != "allocated_consideration"
        or policy.contract_liability_account_id == policy.revenue_account_id
        or policy.contract_liability_account.account_type != FinancialAccountType.LIABILITY
        or policy.revenue_account.account_type != FinancialAccountType.REVENUE
        or policy.contract_liability_account.currency != CANONICAL_CURRENCY
        or policy.revenue_account.currency != CANONICAL_CURRENCY
        or policy.contract_liability_account.status != FinancialAccountStatus.ACTIVE
        or policy.revenue_account.status != FinancialAccountStatus.ACTIVE
    ):
        raise RevenueRecognitionError("Frozen recognition policy is incompatible with launch recognition.")


@transaction.atomic
def create_or_replay_recognition_work(*, evidence_public_id, idempotency_key,
                                      correlation_id, causation_id=None):
    key = _uuid(idempotency_key, "idempotency_key")
    correlation = _uuid(correlation_id, "correlation_id")
    causation = None if causation_id is None else _uuid(causation_id, "causation_id")
    evidence, obligation, allocation, policy = _load_launch_graph(evidence_public_id)
    _validate_launch_graph(
        evidence=evidence, obligation=obligation, allocation=allocation, policy=policy
    )
    identity = {
        "contract": REVENUE_RECOGNITION_CONTRACT,
        "obligation": str(obligation.public_id),
        "allocation": str(allocation.public_id),
        "evidence": str(evidence.public_id),
        "evidence_hash": evidence.source_evidence_hash,
        "policy": str(policy.public_id),
        "target": str(allocation.allocated_amount),
        "currency": allocation.currency,
    }
    deterministic_identity = canonical_request_hash(identity)
    request_hash = canonical_request_hash({"operation": "create-recognition-work", **identity})
    existing_command = IdempotencyRecord.objects.select_for_update().filter(
        scope=CREATE_SCOPE, key=str(key)
    ).first()
    if existing_command and existing_command.request_hash != request_hash:
        raise RevenueRecognitionConflict("Work idempotency key was reused for another graph.")
    work = RevenueRecognitionWorkItem.objects.filter(
        deterministic_identity=deterministic_identity
    ).first()
    created = False
    if work is None:
        try:
            with transaction.atomic():
                work = RevenueRecognitionWorkItem.objects.create(
                    public_id=_derived_uuid("work", deterministic_identity),
                    obligation=obligation,
                    purpose=RevenueRecognitionWorkPurpose.RECOGNIZE_SATISFACTION,
                    evidence_set_digest=evidence.source_evidence_hash,
                    recognition_policy_version=policy,
                    recognition_contract_version=REVENUE_RECOGNITION_CONTRACT,
                    recognition_period_key=REVENUE_RECOGNITION_PERIOD,
                    cumulative_target_amount=allocation.allocated_amount,
                    deterministic_identity=deterministic_identity,
                    correlation_id=correlation,
                    causation_id=causation,
                )
                created = True
        except (IntegrityError, ValidationError):
            work = RevenueRecognitionWorkItem.objects.filter(
                deterministic_identity=deterministic_identity
            ).first()
            if work is None:
                raise
    if (
        work.obligation_id != obligation.pk
        or work.recognition_policy_version_id != policy.pk
        or work.evidence_set_digest != evidence.source_evidence_hash
        or work.cumulative_target_amount != allocation.allocated_amount
        or work.recognition_contract_version != REVENUE_RECOGNITION_CONTRACT
    ):
        raise RevenueRecognitionConflict("Existing recognition work is contradictory.")
    if existing_command is None:
        IdempotencyRecord.objects.create(
            scope=CREATE_SCOPE,
            key=str(key),
            request_hash=request_hash,
            status=IdempotencyStatus.COMPLETED,
            result_type=work._meta.label_lower,
            result_id=str(work.pk),
            safe_response={"work_public_id": str(work.public_id)},
            completed_at=timezone.now(),
        )
    elif existing_command.result_id != str(work.pk):
        raise RevenueRecognitionConflict("Work replay result is contradictory.")
    return RecognitionWorkResult(work, not created)


@transaction.atomic
def claim_revenue_recognition_work(*, work_item_public_id, claim_idempotency_key,
                                   expected_work_version, claim_owner="system"):
    key = _uuid(claim_idempotency_key, "claim_idempotency_key")
    owner = str(claim_owner).strip()
    if not owner or len(owner) > 128:
        raise RevenueRecognitionError("A bounded internal claim owner is required.")
    work = RevenueRecognitionWorkItem.objects.select_for_update().filter(
        public_id=_uuid(work_item_public_id, "work_item_public_id")
    ).first()
    if work is None:
        raise RevenueRecognitionError("Revenue-recognition work was not found.")
    recognition = RevenueRecognition.objects.filter(work_item=work).first()
    if work.status == RevenueRecognitionWorkStatus.COMPLETED:
        if recognition is None:
            raise RevenueRecognitionConflict("Completed work has no recognition result.")
        return RecognitionClaimResult(work, recognition=recognition, replayed=True)
    if work.status in (RevenueRecognitionWorkStatus.CANCELED, RevenueRecognitionWorkStatus.REVIEW_REQUIRED):
        raise RevenueRecognitionError("Terminal recognition work cannot be claimed.")
    if work.recognition_contract_version != REVENUE_RECOGNITION_CONTRACT:
        raise RevenueRecognitionError("Revenue-recognition work contract is unsupported.")
    now = timezone.now()
    token = _derived_uuid("claim", f"{work.public_id}:{key}")
    request_hash = canonical_request_hash({
        "work": str(work.public_id), "owner": owner,
        "expected_work_version": int(expected_work_version),
    })
    prior = IdempotencyRecord.objects.select_for_update().filter(
        scope=CLAIM_SCOPE, key=str(key)
    ).first()
    if prior and prior.request_hash != request_hash:
        raise RevenueRecognitionConflict("Claim idempotency key was reused.")
    if work.status == RevenueRecognitionWorkStatus.CLAIMED:
        if work.claim_token == token and work.claim_owner == owner and work.claim_expires_at > now:
            return RecognitionClaimResult(work, claim_token=token, replayed=True)
        if work.claim_expires_at and work.claim_expires_at > now:
            raise RevenueRecognitionConflict("Recognition work already has an active claim.")
        if work.version != int(expected_work_version):
            raise RevenueRecognitionError("Recognition work version changed before reclaim.")
        work.status = RevenueRecognitionWorkStatus.WAITING
        work.claim_owner = ""
        work.claim_token = None
        work.claimed_at = None
        work.claim_expires_at = None
        work.version += 1
        work.save(update_fields=(
            "status", "claim_owner", "claim_token", "claimed_at", "claim_expires_at",
            "version", "updated_at",
        ))
    elif work.version != int(expected_work_version):
        raise RevenueRecognitionError("Recognition work version changed.")
    if work.attempt_count >= work.max_attempts:
        work.status = RevenueRecognitionWorkStatus.REVIEW_REQUIRED
        work.completed_at = now
        work.failure_classification = "attempts_exhausted"
        work.version += 1
        work.save(update_fields=(
            "status", "completed_at", "failure_classification", "version", "updated_at",
        ))
        raise RevenueRecognitionError("Recognition work attempts are exhausted.")
    if work.next_attempt_at and work.next_attempt_at > now:
        raise RevenueRecognitionError("Recognition work is not due.")
    work.status = RevenueRecognitionWorkStatus.CLAIMED
    work.attempt_count += 1
    work.claim_owner = owner
    work.claim_token = token
    work.claimed_at = now
    work.claim_expires_at = now + CLAIM_LEASE
    work.next_attempt_at = None
    work.version += 1
    work.save(update_fields=(
        "status", "attempt_count", "claim_owner", "claim_token", "claimed_at",
        "claim_expires_at", "next_attempt_at", "version", "updated_at",
    ))
    if prior is None:
        IdempotencyRecord.objects.create(
            scope=CLAIM_SCOPE, key=str(key), request_hash=request_hash,
            status=IdempotencyStatus.COMPLETED,
            result_type=work._meta.label_lower, result_id=str(work.pk),
            safe_response={"work_public_id": str(work.public_id), "claim_token": str(token)},
            completed_at=now,
        )
    elif prior.result_id != str(work.pk):
        raise RevenueRecognitionConflict("Claim replay result is contradictory.")
    return RecognitionClaimResult(work, claim_token=token, replayed=prior is not None)


@transaction.atomic
def recognize_revenue(*, work_item_public_id, claim_token, idempotency_key,
                      correlation_id, causation_id=None,
                      actor_type=FinancialActorType.SYSTEM, actor_id=None):
    work_public_id = _uuid(work_item_public_id, "work_item_public_id")
    token = _uuid(claim_token, "claim_token")
    key = _uuid(idempotency_key, "idempotency_key")
    correlation = _uuid(correlation_id, "correlation_id")
    causation = None if causation_id is None else _uuid(causation_id, "causation_id")
    if not (
        (actor_type == FinancialActorType.SYSTEM and actor_id is None)
        or (actor_type == FinancialActorType.RECONCILIATION and actor_id is not None)
    ):
        raise RevenueRecognitionError("Recognition actor authority is invalid.")
    identity = RevenueRecognitionWorkItem.objects.filter(public_id=work_public_id).values(
        "pk", "obligation_id", "obligation__order_id",
        "obligation__finalization__payment_id",
    ).first()
    if identity is None:
        raise RevenueRecognitionError("Revenue-recognition work was not found.")
    with ordered_lock_scope():
        lock_one(queryset=Order.objects.all(), rank=LockRank.PAYABLE, pk=identity["obligation__order_id"])
        from cheatgame.financial_core.models import Payment
        lock_one(
            queryset=Payment.objects.all(), rank=LockRank.PAYMENT,
            pk=identity["obligation__finalization__payment_id"],
        )
        register_lock(LockRank.FINANCIAL_EVIDENCE, f"010-obligation:{identity['obligation_id']:020d}")
        obligation = PerformanceObligation.objects.select_for_update().select_related(
            "finalization__payment"
        ).get(pk=identity["obligation_id"])
        register_lock(LockRank.FINANCIAL_EVIDENCE, f"020-allocation:{obligation.pk:020d}")
        allocation = ConsiderationAllocation.objects.select_for_update().get(obligation=obligation)
        register_lock(LockRank.FINANCIAL_EVIDENCE, f"030-evidence:{obligation.pk:020d}")
        evidence_rows = list(SatisfactionEvidence.objects.select_for_update().filter(
            obligation=obligation
        ).order_by("pk")[:2])
        if len(evidence_rows) != 1:
            raise RevenueRecognitionError("Exactly one launch satisfaction evidence row is required.")
        evidence = evidence_rows[0]
        register_lock(LockRank.FINANCIAL_EVIDENCE, f"040-work:{identity['pk']:020d}")
        work = RevenueRecognitionWorkItem.objects.select_for_update().get(pk=identity["pk"])
        register_lock(LockRank.ACCOUNTING_POLICY, f"{obligation.recognition_policy_version_id:020d}")
        policy = type(obligation.recognition_policy_version).objects.select_for_update().select_related(
            "contract_liability_account", "revenue_account"
        ).get(pk=obligation.recognition_policy_version_id)
        _validate_launch_graph(
            evidence=evidence, obligation=obligation, allocation=allocation, policy=policy
        )
        fingerprint = canonical_request_hash({
            "contract": REVENUE_RECOGNITION_CONTRACT,
            "work": str(work.public_id),
            "obligation": str(obligation.public_id),
            "allocation": str(allocation.public_id),
            "evidence": str(evidence.public_id),
            "evidence_hash": evidence.source_evidence_hash,
            "policy": str(policy.public_id),
            "amount": str(allocation.allocated_amount),
            "currency": allocation.currency,
            "actor_type": actor_type,
            "actor_id": actor_id,
        })
        key_result = RevenueRecognition.objects.filter(idempotency_key=key).first()
        existing = RevenueRecognition.objects.filter(work_item=work).first()
        if existing:
            if existing.application_fingerprint != fingerprint:
                raise RevenueRecognitionConflict("Completed recognition graph is contradictory.")
            if key_result and key_result.pk != existing.pk:
                raise RevenueRecognitionConflict("Recognition idempotency key belongs to another graph.")
            return RevenueRecognitionResult(existing, True)
        if key_result:
            raise RevenueRecognitionConflict("Recognition idempotency key was reused.")
        now = timezone.now()
        if (
            work.status != RevenueRecognitionWorkStatus.CLAIMED
            or work.claim_token != token
            or work.claim_expires_at is None
            or work.claim_expires_at <= now
            or work.recognition_contract_version != REVENUE_RECOGNITION_CONTRACT
            or work.purpose != RevenueRecognitionWorkPurpose.RECOGNIZE_SATISFACTION
            or work.evidence_set_digest != evidence.source_evidence_hash
            or work.recognition_policy_version_id != policy.pk
            or work.cumulative_target_amount != allocation.allocated_amount
            or work.recognition_period_key != REVENUE_RECOGNITION_PERIOD
        ):
            raise RevenueRecognitionError("Recognition claim or frozen work identity is stale.")
        # Launch v1 permits one full point-in-time recognition only.
        if RevenueRecognition.objects.filter(consideration_allocation=allocation).exists():
            raise RevenueRecognitionConflict("Consideration was already recognized.")
        public_id = _derived_uuid("recognition", work.deterministic_identity)
        journal = post_balanced_journal_entry_under_lock(
            source_type="revenue_recognition",
            source_id=public_id,
            idempotency_key=_derived_uuid("journal", work.deterministic_identity),
            correlation_id=correlation,
            occurred_at=now,
            description="Recognize satisfied performance obligation",
            postings=(
                {
                    "account_id": policy.contract_liability_account_id,
                    "direction": PostingDirection.DEBIT,
                    "amount": allocation.allocated_amount,
                    "currency": CANONICAL_CURRENCY,
                },
                {
                    "account_id": policy.revenue_account_id,
                    "direction": PostingDirection.CREDIT,
                    "amount": allocation.allocated_amount,
                    "currency": CANONICAL_CURRENCY,
                },
            ),
        )
        recognition = RevenueRecognition.objects.create(
            public_id=public_id,
            obligation=obligation,
            consideration_allocation=allocation,
            work_item=work,
            recognition_policy_version=policy,
            journal_entry=journal,
            effect=RevenueRecognitionEffect.EARN,
            amount=allocation.allocated_amount,
            currency=CANONICAL_CURRENCY,
            cumulative_net_recognized_amount=allocation.allocated_amount,
            evidence_set_digest=evidence.source_evidence_hash,
            recognition_period_key=REVENUE_RECOGNITION_PERIOD,
            command_contract_version=REVENUE_RECOGNITION_CONTRACT,
            idempotency_key=key,
            application_fingerprint=fingerprint,
            actor_type=actor_type,
            actor_id=actor_id,
            correlation_id=correlation,
            causation_id=causation,
            recognized_at=now,
        )
        work.status = RevenueRecognitionWorkStatus.COMPLETED
        work.claim_owner = ""
        work.claim_token = None
        work.claimed_at = None
        work.claim_expires_at = None
        work.completed_at = now
        work.failure_classification = ""
        work.safe_result = {
            "recognition_public_id": str(recognition.public_id),
            "journal_public_id": str(journal.public_id),
        }
        work.version += 1
        work.save(update_fields=(
            "status", "claim_owner", "claim_token", "claimed_at", "claim_expires_at",
            "completed_at", "failure_classification", "safe_result", "version", "updated_at",
        ))
        return RevenueRecognitionResult(recognition, False)
