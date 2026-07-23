import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Callable, Optional

from django.core.management import call_command
from django.db import connections
from django.db.migrations.executor import MigrationExecutor


logger = logging.getLogger(__name__)

# First signed 64 bits of SHA-256("cheatsgame-backend:migrations:v1").
# Keep this stable across releases and replicas. Other projects must use a
# different key. MIGRATION_ADVISORY_LOCK_KEY may override it operationally.
DEFAULT_MIGRATION_LOCK_KEY = -1351441551238418675
DEFAULT_MIGRATION_LOCK_TIMEOUT_SECONDS = 300.0
DEFAULT_MIGRATION_LOCK_POLL_SECONDS = 0.5


class MigrationLockError(RuntimeError):
    pass


class MigrationLockTimeout(MigrationLockError):
    pass


class PendingMigrationsRemain(MigrationLockError):
    pass


@dataclass(frozen=True)
class MigrationRunResult:
    pending_before: int
    lock_wait_seconds: float
    duration_seconds: float
    migrated: bool


def migration_lock_key_from_env() -> int:
    raw_value = os.environ.get("MIGRATION_ADVISORY_LOCK_KEY")
    if raw_value is None:
        return DEFAULT_MIGRATION_LOCK_KEY
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise MigrationLockError("MIGRATION_ADVISORY_LOCK_KEY must be a signed 64-bit integer.") from exc
    if not -(2**63) <= value < 2**63:
        raise MigrationLockError("MIGRATION_ADVISORY_LOCK_KEY is outside the signed 64-bit range.")
    return value


def migration_lock_timeout_from_env() -> float:
    raw_value = os.environ.get("MIGRATION_LOCK_TIMEOUT_SECONDS")
    if raw_value is None:
        return DEFAULT_MIGRATION_LOCK_TIMEOUT_SECONDS
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise MigrationLockError("MIGRATION_LOCK_TIMEOUT_SECONDS must be numeric.") from exc
    if value <= 0:
        raise MigrationLockError("MIGRATION_LOCK_TIMEOUT_SECONDS must be greater than zero.")
    return value


def safe_release_identifier() -> str:
    for name in ("APP_RELEASE_COMMIT", "LIARA_COMMIT", "COMMIT_SHA", "GITHUB_SHA"):
        value = os.environ.get(name)
        if value:
            return re.sub(r"[^A-Za-z0-9._:/@-]", "_", value[:128])
    return "unknown"


class AdvisoryLockedMigrationRunner:
    def __init__(
        self,
        *,
        database: str = "default",
        lock_key: Optional[int] = None,
        timeout_seconds: Optional[float] = None,
        poll_seconds: float = DEFAULT_MIGRATION_LOCK_POLL_SECONDS,
        pending_provider: Optional[Callable[[], int]] = None,
        migrate_runner: Optional[Callable[[], None]] = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        log: logging.Logger = logger,
    ):
        self.database = database
        self.lock_key = migration_lock_key_from_env() if lock_key is None else lock_key
        self.timeout_seconds = migration_lock_timeout_from_env() if timeout_seconds is None else timeout_seconds
        self.poll_seconds = poll_seconds
        self.pending_provider = pending_provider or self._pending_migration_count
        self.migrate_runner = migrate_runner or self._run_django_migrate
        self.sleep = sleep
        self.monotonic = monotonic
        self.log = log

    @property
    def connection(self):
        return connections[self.database]

    def _pending_migration_count(self) -> int:
        executor = MigrationExecutor(self.connection)
        targets = executor.loader.graph.leaf_nodes()
        return len(executor.migration_plan(targets))

    def _run_django_migrate(self) -> None:
        call_command("migrate", database=self.database, interactive=False, verbosity=1)

    def _acquire_lock(self) -> float:
        connection = self.connection
        connection.ensure_connection()
        if connection.vendor != "postgresql":
            raise MigrationLockError("Migration advisory locking requires PostgreSQL.")

        started = self.monotonic()
        waiting_logged = False
        while True:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_try_advisory_lock(%s)", [self.lock_key])
                acquired = bool(cursor.fetchone()[0])
            if acquired:
                return self.monotonic() - started

            elapsed = self.monotonic() - started
            if not waiting_logged:
                self.log.info("Another migration process owns the advisory lock; waiting.")
                waiting_logged = True
            if elapsed >= self.timeout_seconds:
                raise MigrationLockTimeout("Timed out waiting for the database migration advisory lock.")
            self.sleep(min(self.poll_seconds, max(0.0, self.timeout_seconds - elapsed)))

    def _release_lock(self) -> None:
        connection = self.connection
        if connection.connection is None:
            # PostgreSQL releases session locks automatically when a session dies.
            self.log.warning("Migration database session ended before explicit lock release.")
            return
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [self.lock_key])
            released = bool(cursor.fetchone()[0])
        if not released:
            raise MigrationLockError("The migration advisory lock was not owned by this session.")

    def run(self) -> MigrationRunResult:
        started = self.monotonic()
        acquired = False
        wait_seconds = 0.0
        try:
            wait_seconds = self._acquire_lock()
            acquired = True
            pending_before = self.pending_provider()
            self.log.info(
                "Migration lock acquired release=%s wait_seconds=%.3f pending=%d",
                safe_release_identifier(),
                wait_seconds,
                pending_before,
            )

            migrated = pending_before > 0
            if migrated:
                self.log.info("Starting Django migrations.")
                self.migrate_runner()
            else:
                self.log.info("No pending Django migrations; exiting as a safe no-op.")

            pending_after = self.pending_provider()
            if pending_after:
                raise PendingMigrationsRemain("Unapplied Django migrations remain after migration execution.")

            duration = self.monotonic() - started
            self.log.info(
                "Migration check completed migrated=%s duration_seconds=%.3f",
                migrated,
                duration,
            )
            return MigrationRunResult(
                pending_before=pending_before,
                lock_wait_seconds=wait_seconds,
                duration_seconds=duration,
                migrated=migrated,
            )
        except BaseException as exc:
            self.log.error(
                "Migration execution failed error_type=%s duration_seconds=%.3f",
                type(exc).__name__,
                self.monotonic() - started,
            )
            raise
        finally:
            if acquired:
                self._release_lock()
