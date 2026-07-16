from django.db import transaction
from django.utils import timezone

from cheatgame.financial_core.models import (
    ReconciliationFinding,
    ReconciliationFindingStatus,
    ReconciliationRun,
    ReconciliationRunStatus,
)
from cheatgame.financial_core.services.state_machines import (
    assert_reconciliation_finding_transition,
    assert_reconciliation_run_transition,
)


@transaction.atomic
def create_reconciliation_run(*, run_type, period_start, period_end, idempotency_key):
    existing = ReconciliationRun.objects.select_for_update().filter(idempotency_key=idempotency_key).first()
    if existing is not None:
        if (
            existing.run_type != run_type
            or existing.period_start != period_start
            or existing.period_end != period_end
        ):
            raise ValueError("Reconciliation idempotency key conflicts with another run.")
        return existing
    return ReconciliationRun.objects.create(
        run_type=run_type,
        period_start=period_start,
        period_end=period_end,
        idempotency_key=idempotency_key,
    )


@transaction.atomic
def transition_reconciliation_run(*, run_id, target_status, records_scanned=None):
    run = ReconciliationRun.objects.select_for_update().get(pk=run_id)
    assert_reconciliation_run_transition(run.status, target_status)
    if run.status == target_status:
        return run
    run.status = target_status
    update_fields = ["status", "updated_at"]
    if target_status == ReconciliationRunStatus.RUNNING:
        run.started_at = run.started_at or timezone.now()
        update_fields.append("started_at")
    if target_status in (ReconciliationRunStatus.COMPLETED, ReconciliationRunStatus.FAILED):
        run.started_at = run.started_at or timezone.now()
        run.completed_at = timezone.now()
        if "started_at" not in update_fields:
            update_fields.append("started_at")
        update_fields.append("completed_at")
    if records_scanned is not None:
        run.records_scanned = records_scanned
        update_fields.append("records_scanned")
    run.save(update_fields=update_fields)
    return run


@transaction.atomic
def record_reconciliation_finding(
    *,
    run_id,
    finding_key,
    finding_type,
    severity,
    expected=None,
    actual=None,
    payment_id=None,
    transaction_id=None,
):
    run = ReconciliationRun.objects.select_for_update().get(pk=run_id)
    existing = ReconciliationFinding.objects.select_for_update().filter(
        run=run, finding_key=finding_key
    ).first()
    if existing is not None:
        return existing
    finding = ReconciliationFinding.objects.create(
        run=run,
        finding_key=finding_key,
        finding_type=finding_type,
        severity=severity,
        expected=expected or {},
        actual=actual or {},
        payment_id=payment_id,
        transaction_id=transaction_id,
    )
    run.findings_count += 1
    run.save(update_fields=("findings_count", "updated_at"))
    return finding


@transaction.atomic
def transition_reconciliation_finding(*, finding_id, target_status, review_case_id=None):
    finding = ReconciliationFinding.objects.select_for_update().get(pk=finding_id)
    assert_reconciliation_finding_transition(finding.status, target_status)
    if finding.status == target_status:
        return finding
    finding.status = target_status
    update_fields = ["status", "updated_at"]
    if review_case_id is not None:
        finding.review_case_id = review_case_id
        update_fields.append("review_case")
    if target_status in (
        ReconciliationFindingStatus.RESOLVED,
        ReconciliationFindingStatus.ACCEPTED,
    ):
        finding.resolved_at = timezone.now()
        update_fields.append("resolved_at")
    finding.save(update_fields=update_fields)
    return finding
