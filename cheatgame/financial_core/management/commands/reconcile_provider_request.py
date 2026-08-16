from datetime import datetime
from uuid import UUID, uuid5

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from cheatgame.financial_core.models import PaymentTransaction
from cheatgame.financial_core.services.provider_request_reconciliation import (
    RECONCILIATION_NAMESPACE,
    ProviderRequestReconciliationError,
    reconcile_no_authority_created,
)
from cheatgame.users.models import BaseUser, UserTypes


class Command(BaseCommand):
    help = "Inspect or close one provider-request ambiguity using authoritative external evidence."

    def add_arguments(self, parser):
        parser.add_argument("transaction_public_id")
        parser.add_argument("--classification", choices=("NO_AUTHORITY_CREATED",), required=True)
        parser.add_argument("--evidence-sha256", required=True)
        parser.add_argument("--observed-at", required=True)
        parser.add_argument("--actor-id", type=int, required=True)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        try:
            transaction_public_id = UUID(options["transaction_public_id"])
            observed_at = datetime.fromisoformat(options["observed_at"].replace("Z", "+00:00"))
            if timezone.is_naive(observed_at):
                raise ValueError
        except (TypeError, ValueError):
            raise CommandError("Transaction and observation values must be valid UUID/ISO-8601 values.")
        try:
            transaction_obj = PaymentTransaction.objects.select_related("attempt__payment").get(
                public_id=transaction_public_id
            )
            actor = BaseUser.objects.get(pk=options["actor_id"])
        except (PaymentTransaction.DoesNotExist, BaseUser.DoesNotExist) as exc:
            raise CommandError("Transaction or finance reviewer does not exist.") from exc
        if not actor.is_active or actor.user_type not in (UserTypes.ADMIN, UserTypes.MANAGER):
            raise CommandError("Actor must be an active Admin or Manager.")
        summary = (
            f"transaction={str(transaction_obj.public_id)[:8]} status={transaction_obj.status} "
            f"attempt={transaction_obj.attempt.status} payment={transaction_obj.attempt.payment.collection_status} "
            f"classification={options['classification']}"
        )
        if not options["apply"]:
            self.stdout.write(f"dry_run=true {summary}")
            return
        try:
            result = reconcile_no_authority_created(
                transaction_public_id=transaction_public_id,
                actor=actor,
                evidence_sha256=options["evidence_sha256"],
                observed_at=observed_at,
                idempotency_key=uuid5(
                    RECONCILIATION_NAMESPACE,
                    f"{transaction_public_id}:{options['evidence_sha256'].lower().strip()}",
                ),
            )
        except (ProviderRequestReconciliationError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            f"applied=true {summary} released_reservations={len(result.released_reservation_ids)} "
            f"replayed={str(result.replayed).lower()}"
        )
