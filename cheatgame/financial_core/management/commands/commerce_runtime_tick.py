import json
from time import monotonic

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from cheatgame.digital_products.services.fulfillment import (
    DigitalFulfillmentError,
    activate_digital_fulfillment_obligation,
    pending_digital_fulfillment_obligation_ids,
)
from cheatgame.financial_core.services.runtime import run_runtime_batch


RUNTIME_ADVISORY_LOCK_ID = 4851062113444592071


class Command(BaseCommand):
    help = "Run one non-overlapping, bounded Financial Core then fulfillment activation tick."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--limit", type=int, default=25)
        parser.add_argument("--timeout-seconds", type=int, default=45)

    def handle(self, *args, **options):
        del args
        if not options["apply"]:
            self.stdout.write("dry_run=true; pass --apply to execute one runtime tick")
            return
        if connection.vendor != "postgresql":
            raise CommandError("Commerce runtime supervision requires PostgreSQL advisory locks.")
        limit = min(max(1, int(options["limit"])), 100)
        timeout_seconds = min(max(5, int(options["timeout_seconds"])), 50)
        started = monotonic()
        if not self._try_lock():
            self.stdout.write(json.dumps({"status": "skipped_overlap"}, sort_keys=True))
            return
        try:
            financial = run_runtime_batch(limit=limit)
            unresolved = [
                item for item in financial.results
                if item.outcome in {"retry_scheduled", "review_required"}
            ]
            if unresolved:
                raise CommandError(
                    "Financial Runtime left unresolved processing failures; fulfillment activation was not run."
                )
            self._require_time(started, timeout_seconds)

            activated = []
            failures = []
            for public_id in pending_digital_fulfillment_obligation_ids(limit=limit):
                self._require_time(started, timeout_seconds)
                try:
                    item = activate_digital_fulfillment_obligation(
                        obligation_public_id=public_id
                    )
                    activated.append(str(item.public_id))
                except DigitalFulfillmentError as exc:
                    failures.append({"code": exc.code})
            payload = {
                "status": "completed" if not failures else "failed",
                "financial_processed": len(financial.results),
                "financial_completed": sum(
                    item.outcome in {"completed", "replayed"}
                    for item in financial.results
                ),
                "fulfillment_activated": len(activated),
                "fulfillment_failures": failures,
                "elapsed_seconds": round(monotonic() - started, 3),
            }
            self.stdout.write(json.dumps(payload, sort_keys=True))
            if failures:
                raise CommandError("One or more fulfillment obligations failed activation.")
        finally:
            self._unlock()

    @staticmethod
    def _require_time(started, timeout_seconds):
        if monotonic() - started >= timeout_seconds:
            raise CommandError("Commerce runtime tick exceeded its bounded execution window.")

    @staticmethod
    def _try_lock():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", [RUNTIME_ADVISORY_LOCK_ID])
            return bool(cursor.fetchone()[0])

    @staticmethod
    def _unlock():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [RUNTIME_ADVISORY_LOCK_ID])
