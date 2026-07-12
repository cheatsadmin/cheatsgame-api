import logging

from django.core.management.base import BaseCommand, CommandError

from cheatgame.common.migration_lock import AdvisoryLockedMigrationRunner


logger = logging.getLogger("cheatgame.migrations")


class Command(BaseCommand):
    help = "Apply Django migrations under a project-specific PostgreSQL session advisory lock."

    def add_arguments(self, parser):
        parser.add_argument("--database", default="default", help="Database alias to migrate (default: default).")

    def handle(self, *args, **options):
        try:
            runner = AdvisoryLockedMigrationRunner(database=options["database"], log=logger)
            result = runner.run()
        except Exception as exc:
            logger.error("Migration execution failed safely error_type=%s", type(exc).__name__)
            raise CommandError("Database migration failed; release startup must stop.") from exc

        self.stdout.write(
            self.style.SUCCESS(
                "Migration gate passed "
                f"(pending_before={result.pending_before}, "
                f"migrated={str(result.migrated).lower()}, "
                f"wait_seconds={result.lock_wait_seconds:.3f}, "
                f"duration_seconds={result.duration_seconds:.3f})."
            )
        )
