from uuid import UUID, uuid5

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from cheatgame.digital_products.models import DigitalInventoryReservationState
from cheatgame.digital_products.services.payment_holds import (
    DigitalPaymentHoldConflict,
    abandonment_candidate_payment_ids,
    escalate_overdue_uncertain_payment,
    expire_abandoned_payment_hold,
)
from cheatgame.financial_core.models import (
    CommercialFinalizationWorkItem,
    FinalizationWorkStatus,
    Payment,
    PaymentCollectionStatus,
)
from cheatgame.financial_core.services.commercial_finalization import (
    finalize_commercial_work_item,
)


COMMAND_NAMESPACE = UUID("fbc027b4-9904-4719-b155-d47af83afed7")


class Command(BaseCommand):
    help = "Inspect or process one bounded batch of Digital payment-hold lifecycle work."

    ACTIONS = (
        "inspect-pending",
        "inspect-review",
        "inspect-abandonment",
        "process-abandonment",
        "escalate-uncertain",
        "inspect-finalization",
        "retry-finalization",
    )

    def add_arguments(self, parser):
        parser.add_argument("action", choices=self.ACTIONS)
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--apply", action="store_true")

    def _ids_for_state(self, state, limit):
        return list(
            Payment.objects.filter(
                order__digital_inventory_reservations__state=state,
            )
            .order_by("order__digital_inventory_reservations__expires_at", "pk")
            .values_list("pk", flat=True)
            .distinct()[:limit]
        )

    def handle(self, *args, **options):
        action = options["action"]
        limit = max(1, min(int(options["limit"]), 1000))
        apply = bool(options["apply"])
        if action == "inspect-pending":
            ids = self._ids_for_state(DigitalInventoryReservationState.PAYMENT_HOLD, limit)
            self.stdout.write(f"pending_count={len(ids)} payment_ids={ids}")
            return
        if action == "inspect-review":
            ids = self._ids_for_state(DigitalInventoryReservationState.HELD_FOR_REVIEW, limit)
            self.stdout.write(f"review_count={len(ids)} payment_ids={ids}")
            return
        if action in ("inspect-abandonment", "process-abandonment"):
            ids = abandonment_candidate_payment_ids(now=timezone.now(), limit=limit)
            if action == "inspect-abandonment" or not apply:
                self.stdout.write(
                    f"dry_run=true abandonment_count={len(ids)} payment_ids={ids}"
                )
                return
            completed = []
            conflicts = []
            for payment_id in ids:
                try:
                    expire_abandoned_payment_hold(payment_id=payment_id)
                    completed.append(payment_id)
                except DigitalPaymentHoldConflict as exc:
                    conflicts.append((payment_id, str(exc)))
            self.stdout.write(
                f"released_count={len(completed)} payment_ids={completed} conflicts={conflicts}"
            )
            return
        if action == "escalate-uncertain":
            ids = list(
                Payment.objects.filter(
                    collection_status__in=(
                        PaymentCollectionStatus.PROCESSING,
                        PaymentCollectionStatus.REVIEW,
                    ),
                    order__digital_inventory_reservations__state=(
                        DigitalInventoryReservationState.PAYMENT_HOLD
                    ),
                )
                .order_by("updated_at", "pk")
                .values_list("pk", flat=True)
                .distinct()[:limit]
            )
            if not apply:
                self.stdout.write(
                    f"dry_run=true uncertain_count={len(ids)} payment_ids={ids}"
                )
                return
            escalated = []
            retained = []
            for payment_id in ids:
                try:
                    escalate_overdue_uncertain_payment(payment_id=payment_id)
                    escalated.append(payment_id)
                except DigitalPaymentHoldConflict:
                    retained.append(payment_id)
            self.stdout.write(
                f"escalated_count={len(escalated)} payment_ids={escalated} "
                f"not_due_or_ineligible={retained}"
            )
            return

        work = (
            CommercialFinalizationWorkItem.objects.filter(
                payment__collection_status=PaymentCollectionStatus.PAID_PENDING_FINALIZATION,
                payment__order__digital_inventory_reservations__isnull=False,
                status__in=(
                    FinalizationWorkStatus.PENDING,
                    FinalizationWorkStatus.CANCELED,
                ),
            )
            .select_related("payment")
            .order_by("next_attempt_at", "pk")
            .distinct()
        )
        if action == "inspect-finalization":
            rows = list(
                work.values_list(
                    "pk",
                    "payment_id",
                    "status",
                    "attempt_count",
                    "next_attempt_at",
                    "last_error_classification",
                )[:limit]
            )
            self.stdout.write(f"finalization_count={len(rows)} rows={rows}")
            return
        due = list(
            work.filter(
                status=FinalizationWorkStatus.PENDING,
                next_attempt_at__lte=timezone.now(),
            )[:limit]
        )
        if not apply:
            self.stdout.write(
                f"dry_run=true due_finalization_count={len(due)} "
                f"work_ids={[item.pk for item in due]}"
            )
            return
        completed = []
        failed = []
        for item in due:
            item.payment.refresh_from_db()
            key = uuid5(
                COMMAND_NAMESPACE,
                f"finalization:{item.public_id}:{item.attempt_count + 1}",
            )
            try:
                finalize_commercial_work_item(
                    work_item_public_id=item.public_id,
                    idempotency_key=key,
                    expected_work_item_version=item.version,
                    expected_payment_version=item.payment.version,
                    correlation_id=key,
                )
                completed.append(item.pk)
            except Exception as exc:
                failed.append((item.pk, type(exc).__name__))
        self.stdout.write(
            f"completed_count={len(completed)} work_ids={completed} failures={failed}"
        )
        if failed:
            raise CommandError("One or more finalization items did not complete.")
