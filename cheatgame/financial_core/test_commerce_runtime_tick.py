import io
import json
import threading
from types import SimpleNamespace
from unittest import mock

from django.core.management import call_command
from django.db import connection, connections
from django.test import TransactionTestCase, skipUnlessDBFeature

from cheatgame.financial_core.management.commands.commerce_runtime_tick import (
    RUNTIME_ADVISORY_LOCK_ID,
)


class CommerceRuntimeTickTests(TransactionTestCase):
    @skipUnlessDBFeature("has_select_for_update")
    def test_overlapping_tick_exits_cleanly_without_processing(self):
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL advisory locks are required.")
        locked = threading.Event()
        release = threading.Event()

        def hold_lock():
            thread_connection = connections["default"]
            try:
                with thread_connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_lock(%s)", [RUNTIME_ADVISORY_LOCK_ID])
                locked.set()
                release.wait(timeout=10)
                with thread_connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_unlock(%s)", [RUNTIME_ADVISORY_LOCK_ID])
            finally:
                thread_connection.close()

        thread = threading.Thread(target=hold_lock)
        thread.start()
        self.assertTrue(locked.wait(timeout=10))
        output = io.StringIO()
        try:
            with mock.patch(
                "cheatgame.financial_core.management.commands.commerce_runtime_tick.run_runtime_batch"
            ) as runtime:
                call_command("commerce_runtime_tick", apply=True, stdout=output)
                runtime.assert_not_called()
        finally:
            release.set()
            thread.join(timeout=10)
        self.assertEqual(json.loads(output.getvalue())["status"], "skipped_overlap")

    def test_financial_runtime_precedes_fulfillment_and_replay_is_safe(self):
        output = io.StringIO()
        events = []
        financial_result = SimpleNamespace(results=[])
        with (
            mock.patch(
                "cheatgame.financial_core.management.commands.commerce_runtime_tick.run_runtime_batch",
                side_effect=lambda **kwargs: events.append("financial") or financial_result,
            ),
            mock.patch(
                "cheatgame.financial_core.management.commands.commerce_runtime_tick.pending_digital_fulfillment_obligation_ids",
                side_effect=lambda **kwargs: events.append("fulfillment-selector") or [],
            ),
        ):
            call_command("commerce_runtime_tick", apply=True, stdout=output)
            call_command("commerce_runtime_tick", apply=True, stdout=output)
        self.assertEqual(
            events,
            ["financial", "fulfillment-selector", "financial", "fulfillment-selector"],
        )
        payloads = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([item["status"] for item in payloads], ["completed", "completed"])
