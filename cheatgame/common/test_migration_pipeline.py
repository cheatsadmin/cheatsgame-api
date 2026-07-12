import io
import os
import threading
import time
from unittest import mock, skipUnless

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import close_old_connections, connection
from django.test import Client, SimpleTestCase, TransactionTestCase

from cheatgame.common.migration_lock import (
    AdvisoryLockedMigrationRunner,
    MigrationLockTimeout,
    PendingMigrationsRemain,
)


IS_POSTGRESQL = connection.vendor == "postgresql"


class MigrationReadinessTests(SimpleTestCase):
    databases = {"default"}

    def test_liveness_is_independent_of_database(self):
        response = Client().get("/health/live/", secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "alive"})

    @mock.patch("cheatgame.core.health.MigrationExecutor")
    def test_readiness_returns_503_when_migrations_are_pending(self, executor_class):
        executor = executor_class.return_value
        executor.loader.graph.leaf_nodes.return_value = [("shop", "9999_test")]
        executor.migration_plan.return_value = [object()]
        response = Client().get("/health/ready/", secure=True)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "not_ready", "reason": "schema_not_ready"})

    @mock.patch("cheatgame.core.health.MigrationExecutor")
    def test_readiness_returns_200_when_schema_is_current(self, executor_class):
        executor = executor_class.return_value
        executor.loader.graph.leaf_nodes.return_value = []
        executor.migration_plan.return_value = []
        response = Client().get("/health/ready/", secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ready"})

    def test_readiness_returns_200_for_applied_test_schema(self):
        response = Client().get("/health/ready/", secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ready"})


@skipUnless(IS_POSTGRESQL, "PostgreSQL advisory-lock tests require PostgreSQL.")
class AdvisoryLockedMigrationRunnerTests(TransactionTestCase):
    reset_sequences = False

    def setUp(self):
        self.lock_key = -1351441551238418675 + (id(self) % 1000000)

    def runner(self, **overrides):
        defaults = {"lock_key": self.lock_key, "timeout_seconds": 2, "poll_seconds": 0.02}
        defaults.update(overrides)
        return AdvisoryLockedMigrationRunner(**defaults)

    def test_no_pending_migration_is_a_successful_noop(self):
        migrate = mock.Mock()
        result = self.runner(pending_provider=lambda: 0, migrate_runner=migrate).run()
        self.assertFalse(result.migrated)
        migrate.assert_not_called()

    def test_pending_migration_runs_and_verifies(self):
        state = {"pending": 1}

        def migrate():
            state["pending"] = 0

        result = self.runner(pending_provider=lambda: state["pending"], migrate_runner=migrate).run()
        self.assertTrue(result.migrated)
        self.assertEqual(result.pending_before, 1)

    def test_two_concurrent_runners_only_one_performs_work(self):
        state_lock = threading.Lock()
        state = {"pending": 1, "migrations": 0}
        first_started = threading.Event()
        allow_first_to_finish = threading.Event()
        results = []
        errors = []

        def pending():
            with state_lock:
                return state["pending"]

        def migrate():
            with state_lock:
                state["migrations"] += 1
            first_started.set()
            allow_first_to_finish.wait(timeout=2)
            with state_lock:
                state["pending"] = 0

        def invoke():
            close_old_connections()
            try:
                results.append(self.runner(pending_provider=pending, migrate_runner=migrate).run())
            except Exception as exc:
                errors.append(exc)
            finally:
                close_old_connections()

        first = threading.Thread(target=invoke)
        second = threading.Thread(target=invoke)
        first.start()
        self.assertTrue(first_started.wait(timeout=2))
        second.start()
        time.sleep(0.1)
        allow_first_to_finish.set()
        first.join(timeout=3)
        second.join(timeout=3)

        self.assertFalse(errors)
        self.assertEqual(state["migrations"], 1)
        self.assertEqual(len(results), 2)
        self.assertEqual(sorted(result.migrated for result in results), [False, True])

    def test_lock_timeout_exits_with_error(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(%s)", [self.lock_key])
        captured = []

        def invoke():
            close_old_connections()
            try:
                self.runner(timeout_seconds=0.1, pending_provider=lambda: 0).run()
            except Exception as exc:
                captured.append(exc)
            finally:
                close_old_connections()

        thread = threading.Thread(target=invoke)
        thread.start()
        thread.join(timeout=2)
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [self.lock_key])
        self.assertEqual(len(captured), 1)
        self.assertIsInstance(captured[0], MigrationLockTimeout)

    def test_failure_releases_lock_and_subsequent_invocation_succeeds(self):
        def fail():
            raise RuntimeError("controlled failure")

        with self.assertRaises(RuntimeError):
            self.runner(pending_provider=lambda: 1, migrate_runner=fail).run()
        result = self.runner(pending_provider=lambda: 0).run()
        self.assertFalse(result.migrated)

    def test_interruption_releases_lock_and_subsequent_invocation_succeeds(self):
        def interrupt():
            raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            self.runner(pending_provider=lambda: 1, migrate_runner=interrupt).run()
        result = self.runner(pending_provider=lambda: 0).run()
        self.assertFalse(result.migrated)

    def test_pending_after_execution_is_failure(self):
        with self.assertRaises(PendingMigrationsRemain):
            self.runner(pending_provider=lambda: 1, migrate_runner=lambda: None).run()

    def test_connection_termination_releases_session_lock(self):
        acquired = threading.Event()

        def acquire_and_disconnect():
            close_old_connections()
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_lock(%s)", [self.lock_key])
            acquired.set()
            connection.close()

        thread = threading.Thread(target=acquire_and_disconnect)
        thread.start()
        self.assertTrue(acquired.wait(timeout=2))
        thread.join(timeout=2)
        result = self.runner(pending_provider=lambda: 0).run()
        self.assertFalse(result.migrated)

    @mock.patch("cheatgame.common.management.commands.migrate_with_advisory_lock.AdvisoryLockedMigrationRunner")
    def test_command_failure_output_does_not_expose_exception_message(self, runner_class):
        secret = "postgres://user:private-password@host/database"
        runner_class.return_value.run.side_effect = RuntimeError(secret)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with self.assertRaises(CommandError):
            call_command("migrate_with_advisory_lock", stdout=stdout, stderr=stderr)
        self.assertNotIn(secret, stdout.getvalue())
        self.assertNotIn(secret, stderr.getvalue())

    def test_release_identifier_is_optional_and_sanitized(self):
        with mock.patch.dict(os.environ, {"APP_RELEASE_COMMIT": "abc123\nunsafe"}, clear=False):
            with self.assertLogs("cheatgame.common.migration_lock", level="INFO") as logs:
                self.runner(pending_provider=lambda: 0).run()
        self.assertIn("release=abc123_unsafe", " ".join(logs.output))
