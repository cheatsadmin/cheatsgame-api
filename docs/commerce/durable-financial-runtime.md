# Durable Financial Runtime

This slice activates the existing Financial Core work graph through explicit,
bounded operator commands. It does not install or start a scheduler, daemon,
Celery worker, signal, startup hook, or background thread.

## Execution order

```text
VerificationWorkItem (provider truth work)
  -> immutable Verification
  -> VerificationWorkItem (APPLY_VERIFIED_FUNDS)
  -> FinancialAllocation and receipt journal
  -> CommercialFinalizationWorkItem
  -> CommercialFinalization
```

`run-batch` always visits Verification, funds recognition, and commercial
finalization in that order. Work created by an earlier stage may therefore be
processed by a later stage in the same bounded invocation.

Only Digital Payments are selected. Existing provider, Financial Core,
PAYMENT_HOLD, adjudication, cardinality, finalization, and fulfillment
contracts remain authoritative.

## Invocation

```text
python manage.py financial_runtime stats
python manage.py financial_runtime inspect --stage all --limit 25
python manage.py financial_runtime run-one --stage verification --work-id 41
python manage.py financial_runtime run-batch --limit 25
python manage.py financial_runtime retry --stage recognition --work-id 52
```

Mutating actions are dry-run unless `--apply` is present. Every command handles
one bounded set and exits. `retry` only makes an existing nonterminal,
unclaimed item due; it cannot reopen canceled, exhausted, paid, or finalized
work.

## Leases and failure recovery

- Verification uses its existing immutable claim and configurable 5–300 second
  lease.
- Funds-recognition work uses the existing `VerificationWorkItem` lease fields.
- Commercial finalization uses its existing five-minute claim.
- An expired lease can be reclaimed; an active lease cannot be stolen.
- `KeyboardInterrupt`, process termination, or process death leaves the claim
  durable for lease-expiry recovery.
- Operational failures return work to `WAITING`/`PENDING` with bounded
  exponential backoff.
- Exhausted or classified terminal work becomes `CANCELED` and is represented
  by an existing `ReviewCase`.
- Domain mutations commit before their work item is considered complete.

No work row is deleted.

## Configuration

| Setting | Default | Purpose |
| --- | ---: | --- |
| `FINANCIAL_RUNTIME_VERIFICATION_LEASE_SECONDS` | 60 | Verification claim lease |
| `FINANCIAL_RUNTIME_RECOGNITION_LEASE_SECONDS` | 60 | Recognition claim lease |
| `FINANCIAL_RUNTIME_RETRY_BASE_SECONDS` | 30 | Initial runtime backoff |
| `FINANCIAL_RUNTIME_RETRY_MAX_SECONDS` | 900 | Maximum runtime backoff |
| `FINANCIAL_RUNTIME_MAX_BATCH_SIZE` | 100 | Hard operator batch ceiling |

The command emits structured application logs with work identifiers,
classification, outcome, and replay state. It never logs provider payloads,
credentials, secrets, or customer data. `stats` exposes pending counts, retry
counts, oldest pending age, canceled work, and open review totals.

## Deployment boundary

No deployment process invokes this command yet. A future release may schedule
separate bounded invocations after provider adapter activation and operational
approval. Scheduling, process supervision, and continuous worker operation are
outside this slice.
