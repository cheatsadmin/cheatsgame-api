import json

from django.core.management.base import BaseCommand, CommandError

from cheatgame.digital_products.services.fulfillment import (
    DigitalFulfillmentError,
    activate_digital_fulfillment_obligation,
    digital_fulfillment_activation_stats,
    pending_digital_fulfillment_obligation_ids,
)


class Command(BaseCommand):
    help = (
        "Inspect or explicitly activate one bounded batch of finalized "
        "Digital fulfillment obligations."
    )

    ACTIONS = ("inspect", "run-one", "run-batch", "stats")

    def add_arguments(self, parser):
        parser.add_argument("action", choices=self.ACTIONS)
        parser.add_argument("--obligation-id")
        parser.add_argument("--limit", type=int, default=25)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        action = options["action"]
        obligation_id = options["obligation_id"]
        apply = bool(options["apply"])
        limit = max(1, min(int(options["limit"]), 1000))

        if action == "stats":
            self.stdout.write(
                json.dumps(digital_fulfillment_activation_stats(), sort_keys=True)
            )
            return

        if action == "run-one":
            if not obligation_id:
                raise CommandError(
                    "run-one requires --obligation-id."
                )
            obligation_ids = [obligation_id]
        else:
            obligation_ids = pending_digital_fulfillment_obligation_ids(
                limit=limit
            )

        if action == "inspect" or not apply:
            self.stdout.write(
                f"dry_run=true pending_count={len(obligation_ids)} "
                f"obligation_ids={[str(value) for value in obligation_ids]}"
            )
            return

        completed = []
        failures = []
        for public_id in obligation_ids:
            try:
                item = activate_digital_fulfillment_obligation(
                    obligation_public_id=public_id
                )
                completed.append(
                    {
                        "obligation_id": str(public_id),
                        "fulfillment_id": str(item.public_id),
                    }
                )
            except DigitalFulfillmentError as exc:
                failures.append(
                    {
                        "obligation_id": str(public_id),
                        "code": exc.code,
                        "detail": str(exc),
                    }
                )
        payload = {
            "activated": completed,
            "failures": failures,
            "summary": {
                "attempted": len(obligation_ids),
                "activated": len(completed),
                "failed": len(failures),
                "resulting_queue": digital_fulfillment_activation_stats()[
                    "queued"
                ],
            },
        }
        self.stdout.write(json.dumps(payload, sort_keys=True))
        if failures:
            raise CommandError(
                "One or more Digital fulfillment obligations were not activated."
            )
