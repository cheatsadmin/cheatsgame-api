# Database Migration Deployment

## Purpose

Liara documents `liara_pre_start.sh` as a pre-start hook suitable for migrations, but does not guarantee whether it runs once per release or once per applet. Cheats Game therefore treats hook cardinality as ambiguous and makes every invocation safe through a PostgreSQL session advisory lock.

Plain migration execution in every web worker is prohibited. Gunicorn never runs migrations.

## Deployment Gate

The repository-root `liara_pre_start.sh` runs:

```bash
python manage.py migrate_with_advisory_lock
python manage.py migrate --check
```

The management command keeps one database session open while it:

1. waits for the project migration lock;
2. counts pending migrations;
3. runs Django migrations only when needed;
4. verifies no migration remains pending;
5. releases the lock in a `finally` block.

Waiting replicas acquire the lock after the first process exits, observe a current schema, and complete as no-ops. PostgreSQL releases the session lock automatically if the process or connection dies.

## Configuration

- `MIGRATION_ADVISORY_LOCK_KEY`: optional signed 64-bit integer override. The default is project-specific and must not be reused by another project.
- `MIGRATION_LOCK_TIMEOUT_SECONDS`: optional positive number. Default: 300 seconds.
- `APP_RELEASE_COMMIT`: optional safe release identifier for logs. The command also checks common commit environment names and does not depend on any of them.

Database credentials remain in Liara environment configuration. They must not be committed or copied to GitHub variables.

## Failure And Retry

Lock timeout, database failure, migration failure, or remaining unapplied migrations produce a non-zero command exit. `set -Eeuo pipefail` stops the pre-start hook. No migration is faked, reversed, or manually inserted into `django_migrations`.

A later restart or deployment may retry safely. Operators must inspect the original failure before retrying. Destructive rollback is not automatic; restore service with forward-compatible code and a corrective forward migration.

## Readiness

- `/health/live/` checks only that Django can answer HTTP.
- `/health/ready/` checks database connectivity and verifies that the deployed migration graph is fully applied.

Readiness returns HTTP 503 with a small generic response when the database or schema is not ready. It never runs migrations or returns migration names or SQL. Liara health checks use the readiness endpoint, preventing traffic activation when the deployed code requires unapplied migrations.

The localhost Liara probe sends `X-Forwarded-Proto: https` so Django's production HTTPS redirect does not turn the readiness request into a misleading HTTP 301.

## Staging Checklist

1. Review `migrate --plan` and `sqlmigrate` for the new migration.
2. Create a Liara database backup.
3. Confirm migrations follow expand/migrate/contract compatibility.
4. Deploy from the staging GitHub branch.
5. Confirm pre-start logs show lock acquisition and migration success/no-op.
6. Confirm `/health/ready/` returns 200 and the release becomes READY.
7. Verify `showmigrations`, legacy APIs, admin, and aggregate record counts.

## Production Rehearsal

Production remains blocked until the exact release is rehearsed against a disposable PostgreSQL database and staging. Record migration duration and lock impact. Create and verify a production backup immediately before deployment.

## Schema Policy

Use expand/migrate/contract releases:

1. Expand with additive, backward-compatible schema.
2. Migrate data and activate compatible code.
3. Remove old fields only in a later independent release after verification.

Application rollback must remain compatible with the expanded schema. Never depend on reversing a migration as the normal deployment rollback mechanism.
