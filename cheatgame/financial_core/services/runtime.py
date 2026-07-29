import logging
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid5

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import DatabaseError, transaction
from django.db.models import Min, Q, Sum
from django.utils import timezone

from cheatgame.financial_core.models import (
    CommercialFinalizationWorkItem,
    FinalizationWorkStatus,
    FinancialActorType,
    Payment,
    ReviewCase,
    ReviewCaseReason,
    ReviewCaseSeverity,
    ReviewCaseStatus,
    Verification,
    VerificationTriggerSource,
    VerificationWorkItem,
    VerificationWorkStatus,
    VerificationWorkType,
)
from cheatgame.financial_core.services.adapters import PRODUCTION_ADAPTER_REGISTRY
from cheatgame.financial_core.services.commercial_finalization import (
    finalize_commercial_work_item,
)
from cheatgame.financial_core.services.events import append_financial_event
from cheatgame.financial_core.services.funds_application import (
    FundsApplicationBlocked,
    recognize_verified_funds,
)
from cheatgame.financial_core.services.idempotency import IdempotencyConflict
from cheatgame.financial_core.services.verification import (
    VerificationBlocked,
    VerificationClaimConflict,
)
from cheatgame.financial_core.services.verification_worker import (
    EXECUTABLE_WORK_TYPES,
    execute_verification_work_item,
)


logger = logging.getLogger("cheatgame.financial_runtime")
RUNTIME_NAMESPACE = UUID("b5f22bc2-25ad-45a5-999d-0fa6b3065644")
RUNTIME_STAGES = ("verification", "recognition", "finalization")
OPEN_REVIEW_STATES = (
    ReviewCaseStatus.OPEN,
    ReviewCaseStatus.INVESTIGATING,
    ReviewCaseStatus.APPROVAL_PENDING,
)


class FinancialRuntimeConflict(ValidationError):
    pass


@dataclass(frozen=True)
class RuntimePolicy:
    verification_lease_seconds: int
    recognition_lease_seconds: int
    retry_base_seconds: int
    retry_max_seconds: int
    max_batch_size: int


@dataclass(frozen=True)
class RuntimeExecutionResult:
    stage: str
    work_id: int
    outcome: str
    classification: str = ""


@dataclass(frozen=True)
class RuntimeBatchResult:
    requested_limit: int
    results: tuple[RuntimeExecutionResult, ...]


def _positive_setting(name, default):
    value = int(getattr(settings, name, default))
    if value <= 0:
        raise ImproperlyConfigured(f"{name} must be a positive integer.")
    return value


def get_runtime_policy():
    policy = RuntimePolicy(
        verification_lease_seconds=_positive_setting(
            "FINANCIAL_RUNTIME_VERIFICATION_LEASE_SECONDS", 60
        ),
        recognition_lease_seconds=_positive_setting(
            "FINANCIAL_RUNTIME_RECOGNITION_LEASE_SECONDS", 60
        ),
        retry_base_seconds=_positive_setting(
            "FINANCIAL_RUNTIME_RETRY_BASE_SECONDS", 30
        ),
        retry_max_seconds=_positive_setting(
            "FINANCIAL_RUNTIME_RETRY_MAX_SECONDS", 900
        ),
        max_batch_size=_positive_setting(
            "FINANCIAL_RUNTIME_MAX_BATCH_SIZE", 100
        ),
    )
    if not 5 <= policy.verification_lease_seconds <= 300:
        raise ImproperlyConfigured(
            "FINANCIAL_RUNTIME_VERIFICATION_LEASE_SECONDS must be between 5 and 300."
        )
    if not 5 <= policy.recognition_lease_seconds <= 300:
        raise ImproperlyConfigured(
            "FINANCIAL_RUNTIME_RECOGNITION_LEASE_SECONDS must be between 5 and 300."
        )
    if policy.retry_max_seconds < policy.retry_base_seconds:
        raise ImproperlyConfigured(
            "FINANCIAL_RUNTIME_RETRY_MAX_SECONDS cannot be shorter than the base delay."
        )
    if policy.max_batch_size > 1000:
        raise ImproperlyConfigured(
            "FINANCIAL_RUNTIME_MAX_BATCH_SIZE cannot exceed 1000."
        )
    return policy


def _runtime_key(value):
    return uuid5(RUNTIME_NAMESPACE, str(value))


def _retry_delay(*, attempt_count, policy):
    return min(
        policy.retry_base_seconds * (2 ** max(int(attempt_count) - 1, 0)),
        policy.retry_max_seconds,
    )


def _digital_payment_ids():
    return Payment.objects.filter(
        order__digital_inventory_reservations__isnull=False
    ).values_list("pk", flat=True)


def _verification_queryset():
    return VerificationWorkItem.objects.filter(
        transaction__attempt__payment_id__in=_digital_payment_ids(),
        work_type__in=EXECUTABLE_WORK_TYPES,
    )


def _recognition_queryset():
    return VerificationWorkItem.objects.filter(
        transaction__attempt__payment_id__in=_digital_payment_ids(),
        work_type=VerificationWorkType.APPLY_VERIFIED_FUNDS,
    )


def _finalization_queryset():
    return CommercialFinalizationWorkItem.objects.filter(
        payment_id__in=_digital_payment_ids()
    )


def _due_filter(queryset, *, now):
    return queryset.filter(
        status__in=(VerificationWorkStatus.PENDING, VerificationWorkStatus.WAITING),
        next_attempt_at__lte=now,
    ) | queryset.filter(
        status=VerificationWorkStatus.CLAIMED,
        claim_expires_at__lte=now,
    )


def _due_finalization_filter(queryset, *, now):
    return queryset.filter(
        status=FinalizationWorkStatus.PENDING,
        next_attempt_at__lte=now,
    ) | queryset.filter(
        status=FinalizationWorkStatus.CLAIMED,
        claim_expires_at__lte=now,
    )


def due_runtime_work_ids(*, stage, limit, now=None):
    now = now or timezone.now()
    bounded_limit = min(max(1, int(limit)), get_runtime_policy().max_batch_size)
    if stage == "verification":
        queryset = _due_filter(_verification_queryset(), now=now)
    elif stage == "recognition":
        queryset = _due_filter(_recognition_queryset(), now=now)
    elif stage == "finalization":
        queryset = _due_finalization_filter(_finalization_queryset(), now=now)
    else:
        raise FinancialRuntimeConflict("Unsupported runtime stage.")
    return list(
        queryset.order_by("next_attempt_at", "pk")
        .values_list("pk", flat=True)
        .distinct()[:bounded_limit]
    )


@transaction.atomic
def make_runtime_work_due(*, stage, work_id, now=None):
    now = now or timezone.now()
    if stage in ("verification", "recognition"):
        work = (
            VerificationWorkItem.objects.select_for_update()
            .select_related("transaction__attempt__payment__order")
            .get(pk=work_id)
        )
        if not work.transaction.attempt.payment.order.digital_inventory_reservations.exists():
            raise FinancialRuntimeConflict(
                "Runtime activation is limited to Digital payment work."
            )
        expected_type = (
            work.work_type in EXECUTABLE_WORK_TYPES
            if stage == "verification"
            else work.work_type == VerificationWorkType.APPLY_VERIFIED_FUNDS
        )
        if not expected_type:
            raise FinancialRuntimeConflict("Work item belongs to another runtime stage.")
        if work.status not in (
            VerificationWorkStatus.PENDING,
            VerificationWorkStatus.WAITING,
        ):
            raise FinancialRuntimeConflict("Only nonterminal unclaimed work may be retried.")
        if work.attempt_count >= work.max_attempts:
            raise FinancialRuntimeConflict("Exhausted work requires ReviewCase resolution.")
    elif stage == "finalization":
        work = (
            CommercialFinalizationWorkItem.objects.select_for_update()
            .select_related("payment__order")
            .get(pk=work_id)
        )
        if not work.payment.order.digital_inventory_reservations.exists():
            raise FinancialRuntimeConflict(
                "Runtime activation is limited to Digital payment work."
            )
        if work.status != FinalizationWorkStatus.PENDING:
            raise FinancialRuntimeConflict("Only pending finalization work may be retried.")
        if work.attempt_count >= work.max_attempts:
            raise FinancialRuntimeConflict("Exhausted finalization requires ReviewCase resolution.")
    else:
        raise FinancialRuntimeConflict("Unsupported runtime stage.")
    if work.next_attempt_at > now:
        work.next_attempt_at = now
        work.version += 1
        work.save(update_fields=("next_attempt_at", "version", "updated_at"))
    return work


@transaction.atomic
def _claim_recognition_work(*, work_id, now=None):
    now = now or timezone.now()
    policy = get_runtime_policy()
    work = (
        VerificationWorkItem.objects.select_for_update()
        .select_related("transaction__attempt__payment")
        .get(pk=work_id)
    )
    if work.work_type != VerificationWorkType.APPLY_VERIFIED_FUNDS:
        raise FinancialRuntimeConflict("Work item is not funds-recognition work.")
    if work.status == VerificationWorkStatus.COMPLETED:
        return work, None, True
    if work.status == VerificationWorkStatus.CANCELED:
        raise FinancialRuntimeConflict("Canceled recognition work requires review resolution.")
    if work.status == VerificationWorkStatus.CLAIMED:
        if work.claim_expires_at and work.claim_expires_at > now:
            raise FinancialRuntimeConflict("Recognition work already has an active lease.")
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
    if work.status not in (
        VerificationWorkStatus.PENDING,
        VerificationWorkStatus.WAITING,
    ):
        raise FinancialRuntimeConflict("Recognition work is not claimable.")
    if work.next_attempt_at > now:
        raise FinancialRuntimeConflict("Recognition work is not due.")
    if work.attempt_count >= work.max_attempts:
        raise FinancialRuntimeConflict("Recognition work exhausted its retry budget.")
    token = _runtime_key(
        f"recognition-claim:{work.public_id}:{work.attempt_count + 1}"
    )
    work.status = VerificationWorkStatus.CLAIMED
    work.attempt_count += 1
    work.claim_token = token
    work.claimed_at = now
    work.claim_expires_at = now + timedelta(
        seconds=policy.recognition_lease_seconds
    )
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
    return work, token, False


@transaction.atomic
def _record_verification_runtime_failure(
    *,
    work_id,
    classification,
    terminal=False,
    now=None,
):
    now = now or timezone.now()
    policy = get_runtime_policy()
    work = VerificationWorkItem.objects.select_for_update().get(pk=work_id)
    if (
        work.status == VerificationWorkStatus.CLAIMED
        and work.claim_expires_at
        and work.claim_expires_at > now
        and classification == "VerificationClaimConflict"
    ):
        return work, False
    if work.status not in (
        VerificationWorkStatus.PENDING,
        VerificationWorkStatus.CLAIMED,
        VerificationWorkStatus.WAITING,
    ):
        return work, False
    exhausted = terminal or work.attempt_count >= work.max_attempts
    work.status = (
        VerificationWorkStatus.CANCELED
        if exhausted
        else VerificationWorkStatus.WAITING
    )
    work.claim_token = None
    work.claimed_at = None
    work.claim_expires_at = None
    work.completed_at = now if exhausted else None
    work.next_attempt_at = now + timedelta(
        seconds=_retry_delay(attempt_count=work.attempt_count, policy=policy)
    )
    work.last_error_classification = str(classification)[:64]
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
    return work, exhausted


@transaction.atomic
def _record_recognition_runtime_failure(
    *,
    work_id,
    claim_token,
    classification,
    terminal,
    now=None,
):
    now = now or timezone.now()
    policy = get_runtime_policy()
    work = VerificationWorkItem.objects.select_for_update().get(pk=work_id)
    if (
        work.status != VerificationWorkStatus.CLAIMED
        or work.claim_token != claim_token
    ):
        return work, work.status == VerificationWorkStatus.CANCELED
    exhausted = terminal or work.attempt_count >= work.max_attempts
    work.status = (
        VerificationWorkStatus.CANCELED
        if exhausted
        else VerificationWorkStatus.WAITING
    )
    work.claim_token = None
    work.claimed_at = None
    work.claim_expires_at = None
    work.completed_at = now if exhausted else None
    work.next_attempt_at = now + timedelta(
        seconds=_retry_delay(attempt_count=work.attempt_count, policy=policy)
    )
    work.last_error_classification = str(classification)[:64]
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
    return work, exhausted


@transaction.atomic
def _open_runtime_review(*, work, reason, summary):
    payment = work.transaction.attempt.payment
    key = _runtime_key(f"runtime-review:{work.public_id}:{reason}")
    review, created = ReviewCase.objects.get_or_create(
        idempotency_key=key,
        defaults={
            "reason": reason,
            "severity": ReviewCaseSeverity.HIGH,
            "order": payment.order,
            "payment": payment,
            "attempt": work.transaction.attempt,
            "transaction": work.transaction,
            "opened_by_type": FinancialActorType.SYSTEM,
            "summary": str(summary)[:1000],
        },
    )
    if created:
        append_financial_event(
            aggregate_type=review._meta.label_lower,
            aggregate_id=review.public_id,
            aggregate_version=review.version,
            event_type="financial_runtime.review_required",
            actor_type=FinancialActorType.SYSTEM,
            idempotency_key=f"financial-runtime-review:{key}",
            correlation_id=payment.public_id,
            causation_id=work.public_id,
            metadata={
                "reason_code": reason,
                "work_type": work.work_type,
            },
        )
    return review


def _execute_verification(*, work_id, adapter_registry):
    work = VerificationWorkItem.objects.get(pk=work_id)
    if work.status == VerificationWorkStatus.COMPLETED:
        return RuntimeExecutionResult("verification", work.pk, "replayed")
    if work.status == VerificationWorkStatus.CANCELED:
        return RuntimeExecutionResult(
            "verification", work.pk, "review_required", work.last_error_classification
        )
    root_key = _runtime_key(
        f"verification:{work.public_id}:{work.attempt_count + 1}"
    )
    try:
        result = execute_verification_work_item(
            work_item_id=work.pk,
            trigger_source=VerificationTriggerSource.RECONCILIATION,
            execution_idempotency_key=root_key,
            adapter_registry=adapter_registry,
            lease_seconds=get_runtime_policy().verification_lease_seconds,
            retry_after_seconds=_retry_delay(
                attempt_count=work.attempt_count + 1,
                policy=get_runtime_policy(),
            ),
            apply_projection=True,
        )
    except VerificationClaimConflict as exc:
        refreshed = VerificationWorkItem.objects.get(pk=work.pk)
        if (
            refreshed.status == VerificationWorkStatus.CLAIMED
            and refreshed.claim_expires_at
            and refreshed.claim_expires_at > timezone.now()
        ):
            return RuntimeExecutionResult(
                "verification", work.pk, "skipped", type(exc).__name__
            )
        refreshed, exhausted = _record_verification_runtime_failure(
            work_id=work.pk,
            classification=type(exc).__name__,
        )
        if exhausted:
            _open_runtime_review(
                work=refreshed,
                reason=ReviewCaseReason.PROVIDER_STATE_UNCLEAR,
                summary="Verification runtime exhausted without authoritative resolution.",
            )
            outcome = "review_required"
        elif refreshed.status == VerificationWorkStatus.WAITING:
            outcome = "retry_scheduled"
        else:
            outcome = "skipped"
        return RuntimeExecutionResult(
            "verification", work.pk, outcome, type(exc).__name__
        )
    except VerificationBlocked as exc:
        refreshed, _ = _record_verification_runtime_failure(
            work_id=work.pk,
            classification=type(exc).__name__,
            terminal=True,
        )
        _open_runtime_review(
            work=refreshed,
            reason=ReviewCaseReason.PROVIDER_STATE_UNCLEAR,
            summary="Verification work is blocked by an authoritative runtime guard.",
        )
        return RuntimeExecutionResult(
            "verification", work.pk, "review_required", type(exc).__name__
        )
    except Exception as exc:
        refreshed, exhausted = _record_verification_runtime_failure(
            work_id=work.pk,
            classification=type(exc).__name__,
        )
        if exhausted:
            _open_runtime_review(
                work=refreshed,
                reason=ReviewCaseReason.PROVIDER_STATE_UNCLEAR,
                summary="Verification runtime exhausted after repeated operational failures.",
            )
        logger.warning(
            "financial_runtime_verification_failed",
            extra={
                "event": "financial_runtime.verification_failed",
                "work_id": work.pk,
                "classification": type(exc).__name__,
                "exhausted": exhausted,
            },
        )
        return RuntimeExecutionResult(
            "verification",
            work.pk,
            "review_required" if exhausted else "retry_scheduled",
            type(exc).__name__,
        )
    logger.info(
        "financial_runtime_verification_completed",
        extra={
            "event": "financial_runtime.verification_completed",
            "work_id": work.pk,
            "verification_id": result.verification.pk,
            "replayed": result.replayed,
        },
    )
    return RuntimeExecutionResult(
        "verification",
        work.pk,
        "replayed" if result.replayed else "completed",
    )


def _execute_recognition(*, work_id):
    try:
        work, claim_token, replayed = _claim_recognition_work(work_id=work_id)
    except FinancialRuntimeConflict as exc:
        work = VerificationWorkItem.objects.get(pk=work_id)
        if work.status == VerificationWorkStatus.COMPLETED:
            return RuntimeExecutionResult("recognition", work.pk, "replayed")
        if (
            work.status == VerificationWorkStatus.CLAIMED
            and work.claim_expires_at
            and work.claim_expires_at > timezone.now()
        ):
            return RuntimeExecutionResult(
                "recognition", work.pk, "skipped", type(exc).__name__
            )
        raise
    if replayed:
        return RuntimeExecutionResult("recognition", work.pk, "replayed")
    try:
        verification = Verification.objects.get(
            public_id=work.causation_id,
            transaction=work.transaction,
        )
    except Verification.DoesNotExist:
        _record_recognition_runtime_failure(
            work_id=work.pk,
            claim_token=claim_token,
            classification="RecognitionVerificationMissing",
            terminal=True,
        )
        _open_runtime_review(
            work=work,
            reason=ReviewCaseReason.VERIFIED_FUNDS_APPLICATION_FAILED,
            summary="Recognition work is not bound to its authoritative Verification.",
        )
        return RuntimeExecutionResult(
            "recognition",
            work.pk,
            "review_required",
            "RecognitionVerificationMissing",
        )
    payment = work.transaction.attempt.payment
    key = _runtime_key(f"recognition:{work.public_id}")
    try:
        result = recognize_verified_funds(
            verification_id=verification.pk,
            idempotency_key=key,
            expected_payment_version=payment.version,
            correlation_id=work.correlation_id,
            causation_id=verification.public_id,
        )
    except FundsApplicationBlocked as exc:
        _record_recognition_runtime_failure(
            work_id=work.pk,
            claim_token=claim_token,
            classification=type(exc).__name__,
            terminal=True,
        )
        return RuntimeExecutionResult(
            "recognition", work.pk, "review_required", type(exc).__name__
        )
    except (DatabaseError, IdempotencyConflict) as exc:
        refreshed, exhausted = _record_recognition_runtime_failure(
            work_id=work.pk,
            claim_token=claim_token,
            classification=type(exc).__name__,
            terminal=False,
        )
        if exhausted:
            _open_runtime_review(
                work=refreshed,
                reason=ReviewCaseReason.VERIFIED_FUNDS_APPLICATION_FAILED,
                summary="Funds-recognition runtime exhausted its retry budget.",
            )
        logger.warning(
            "financial_runtime_recognition_failed",
            extra={
                "event": "financial_runtime.recognition_failed",
                "work_id": work.pk,
                "classification": type(exc).__name__,
                "exhausted": exhausted,
            },
        )
        return RuntimeExecutionResult(
            "recognition",
            work.pk,
            "review_required" if exhausted else "retry_scheduled",
            type(exc).__name__,
        )
    logger.info(
        "financial_runtime_recognition_completed",
        extra={
            "event": "financial_runtime.recognition_completed",
            "work_id": work.pk,
            "allocation_id": result.allocation.pk,
            "replayed": result.replayed,
        },
    )
    return RuntimeExecutionResult(
        "recognition", work.pk, "replayed" if result.replayed else "completed"
    )


def _execute_finalization(*, work_id):
    work = CommercialFinalizationWorkItem.objects.select_related("payment").get(
        pk=work_id
    )
    if work.status == FinalizationWorkStatus.COMPLETED:
        return RuntimeExecutionResult("finalization", work.pk, "replayed")
    if work.status == FinalizationWorkStatus.CANCELED:
        return RuntimeExecutionResult(
            "finalization", work.pk, "review_required", work.last_error_classification
        )
    key = _runtime_key(
        f"finalization:{work.public_id}:{work.attempt_count + 1}"
    )
    try:
        result = finalize_commercial_work_item(
            work_item_public_id=work.public_id,
            idempotency_key=key,
            expected_work_item_version=work.version,
            expected_payment_version=work.payment.version,
            correlation_id=work.correlation_id,
            causation_id=work.causation_id,
        )
    except Exception as exc:
        work.refresh_from_db()
        exhausted = (
            work.status == FinalizationWorkStatus.PENDING
            and work.attempt_count >= work.max_attempts
        )
        if exhausted:
            from cheatgame.digital_products.services.payment_holds import (
                escalate_terminal_finalization_failure,
            )

            with transaction.atomic():
                locked = CommercialFinalizationWorkItem.objects.select_for_update().get(
                    pk=work.pk
                )
                if (
                    locked.status == FinalizationWorkStatus.PENDING
                    and locked.attempt_count >= locked.max_attempts
                ):
                    locked.status = FinalizationWorkStatus.CANCELED
                    locked.completed_at = timezone.now()
                    locked.last_error_classification = (
                        "finalization_attempts_exhausted"
                    )
                    locked.version += 1
                    locked.save(
                        update_fields=(
                            "status",
                            "completed_at",
                            "last_error_classification",
                            "version",
                            "updated_at",
                        )
                    )
            escalate_terminal_finalization_failure(
                payment_id=work.payment_id,
                classification="finalization_attempts_exhausted",
                identity=f"runtime-exhausted:{work.public_id}",
            )
        logger.warning(
            "financial_runtime_finalization_failed",
            extra={
                "event": "financial_runtime.finalization_failed",
                "work_id": work.pk,
                "classification": type(exc).__name__,
                "exhausted": exhausted,
            },
        )
        return RuntimeExecutionResult(
            "finalization",
            work.pk,
            (
                "review_required"
                if exhausted or work.status == FinalizationWorkStatus.CANCELED
                else "retry_scheduled"
            ),
            type(exc).__name__,
        )
    logger.info(
        "financial_runtime_finalization_completed",
        extra={
            "event": "financial_runtime.finalization_completed",
            "work_id": work.pk,
            "finalization_id": result.finalization.pk,
            "replayed": result.replayed,
        },
    )
    return RuntimeExecutionResult(
        "finalization", work.pk, "replayed" if result.replayed else "completed"
    )


def execute_runtime_work(
    *,
    stage,
    work_id,
    adapter_registry=PRODUCTION_ADAPTER_REGISTRY,
):
    if stage in ("verification", "recognition"):
        work = VerificationWorkItem.objects.select_related(
            "transaction__attempt__payment__order"
        ).get(pk=work_id)
        if not work.transaction.attempt.payment.order.digital_inventory_reservations.exists():
            raise FinancialRuntimeConflict(
                "Runtime activation is limited to Digital payment work."
            )
    elif stage == "finalization":
        work = CommercialFinalizationWorkItem.objects.select_related(
            "payment__order"
        ).get(pk=work_id)
        if not work.payment.order.digital_inventory_reservations.exists():
            raise FinancialRuntimeConflict(
                "Runtime activation is limited to Digital payment work."
            )
    if stage == "verification":
        return _execute_verification(
            work_id=work_id,
            adapter_registry=adapter_registry,
        )
    if stage == "recognition":
        return _execute_recognition(work_id=work_id)
    if stage == "finalization":
        return _execute_finalization(work_id=work_id)
    raise FinancialRuntimeConflict("Unsupported runtime stage.")


def run_runtime_batch(
    *,
    limit,
    adapter_registry=PRODUCTION_ADAPTER_REGISTRY,
):
    bounded_limit = min(max(1, int(limit)), get_runtime_policy().max_batch_size)
    results = []
    for stage in RUNTIME_STAGES:
        remaining = bounded_limit - len(results)
        if remaining <= 0:
            break
        for work_id in due_runtime_work_ids(stage=stage, limit=remaining):
            try:
                results.append(
                    execute_runtime_work(
                        stage=stage,
                        work_id=work_id,
                        adapter_registry=adapter_registry,
                    )
                )
            except FinancialRuntimeConflict as exc:
                results.append(
                    RuntimeExecutionResult(
                        stage,
                        work_id,
                        "skipped",
                        type(exc).__name__,
                    )
                )
    return RuntimeBatchResult(bounded_limit, tuple(results))


def runtime_stats(*, now=None):
    now = now or timezone.now()
    verification = _verification_queryset()
    recognition = _recognition_queryset()
    finalization = _finalization_queryset()

    def pending_stats(queryset, statuses, *, claimed_status, waiting_status=None):
        rows = queryset.filter(status__in=statuses)
        aggregate = rows.aggregate(
            oldest=Min("created_at"),
            retries=Sum("attempt_count"),
        )
        due_filter = Q(
            status__in=tuple(
                status
                for status in statuses
                if status != claimed_status
            ),
            next_attempt_at__lte=now,
        ) | Q(
            status=claimed_status,
            claim_expires_at__lte=now,
        )
        return {
            "count": rows.count(),
            "oldest_age_seconds": (
                max(0, int((now - aggregate["oldest"]).total_seconds()))
                if aggregate["oldest"]
                else 0
            ),
            "retry_count": int(aggregate["retries"] or 0),
            "due_count": rows.filter(due_filter).count(),
            "claimed_count": rows.filter(status=claimed_status).count(),
            "retryable_failure_count": rows.exclude(
                last_error_classification=""
            ).count(),
            "completed_count": queryset.filter(
                status=(
                    VerificationWorkStatus.COMPLETED
                    if waiting_status is not None
                    else FinalizationWorkStatus.COMPLETED
                )
            ).count(),
            "canceled_count": queryset.filter(
                status=(
                    VerificationWorkStatus.CANCELED
                    if waiting_status is not None
                    else FinalizationWorkStatus.CANCELED
                )
            ).count(),
            **(
                {
                    "pending_count": rows.filter(
                        status=VerificationWorkStatus.PENDING
                    ).count(),
                    "waiting_count": rows.filter(
                        status=waiting_status
                    ).count(),
                }
                if waiting_status is not None
                else {
                    "pending_count": rows.filter(
                        status=FinalizationWorkStatus.PENDING
                    ).count(),
                    "waiting_count": 0,
                }
            ),
        }

    result = {
        "verification": pending_stats(
            verification,
            (
                VerificationWorkStatus.PENDING,
                VerificationWorkStatus.WAITING,
                VerificationWorkStatus.CLAIMED,
            ),
            claimed_status=VerificationWorkStatus.CLAIMED,
            waiting_status=VerificationWorkStatus.WAITING,
        ),
        "recognition": pending_stats(
            recognition,
            (
                VerificationWorkStatus.PENDING,
                VerificationWorkStatus.WAITING,
                VerificationWorkStatus.CLAIMED,
            ),
            claimed_status=VerificationWorkStatus.CLAIMED,
            waiting_status=VerificationWorkStatus.WAITING,
        ),
        "finalization": pending_stats(
            finalization,
            (
                FinalizationWorkStatus.PENDING,
                FinalizationWorkStatus.CLAIMED,
            ),
            claimed_status=FinalizationWorkStatus.CLAIMED,
        ),
        "failed_work": (
            verification.filter(status=VerificationWorkStatus.CANCELED).count()
            + recognition.filter(status=VerificationWorkStatus.CANCELED).count()
            + finalization.filter(status=FinalizationWorkStatus.CANCELED).count()
        ),
        "review_required": ReviewCase.objects.filter(
            payment_id__in=_digital_payment_ids(),
            status__in=OPEN_REVIEW_STATES,
        ).count(),
    }
    result["pending_total"] = sum(
        result[stage]["count"] for stage in RUNTIME_STAGES
    )
    result["claimed_total"] = sum(
        result[stage]["claimed_count"] for stage in RUNTIME_STAGES
    )
    result["retryable_failure_total"] = sum(
        result[stage]["retryable_failure_count"] for stage in RUNTIME_STAGES
    )
    return result
