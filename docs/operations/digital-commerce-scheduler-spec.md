# Digital commerce scheduler specification

Status: platform-neutral release specification. It does not activate a job,
daemon, process supervisor, startup hook, or schedule.

Repository evidence contains a Liara web application manifest, a manually
triggered staging deployment workflow, and a migration-only pre-start hook. It
contains no verified production cron or scheduled-job format. Therefore:

`PRODUCTION SCHEDULER CONFIGURATION PENDING PLATFORM CONFIRMATION`

## Financial Runtime job

| Property | Initial production contract |
| --- | --- |
| Command | `python manage.py financial_runtime run-batch --limit 50 --apply` |
| Cadence | every 60 seconds |
| Timeout | 4 minutes; terminate gracefully before a later invocation |
| Overlap | supervisor should forbid overlap; database leases still make accidental overlap safe |
| Concurrency | one scheduled invocation; emergency second invocation is safe but not a throughput default |
| Exit | zero for empty/success/replay/contention; non-zero for processing failure, review escalation, unsafe configuration, or command failure |
| Replay | safe; durable identities, leases, allocations, journals, and finalization guards converge |
| Environment | Django production settings, database, runtime policy settings, provider configuration, callback/return hosts, structured log collection |
| Rollout | inspect/stats, sandbox certification, supervised enablement, then production release gate |

One aggregate invocation is preferred because the command enforces
Verification → Recognition → Finalization and may process newly created
downstream work within the same bounded batch. Separate stage schedules are
not required and would increase latency without adding authority.

## Fulfillment activation job

| Property | Initial production contract |
| --- | --- |
| Command | `python manage.py activate_digital_fulfillment run-batch --limit 50 --apply` |
| Cadence | every 60 seconds, after the Financial Runtime job where the scheduler supports dependency ordering |
| Timeout | 2 minutes |
| Overlap | supervisor should forbid overlap; obligation locks, deterministic intake identity, and database uniqueness make accidental overlap converge |
| Concurrency | one scheduled invocation |
| Exit | zero for empty or successful batch; non-zero if any obligation failed after all selected rows were isolated and attempted |
| Replay | safe; one obligation produces one execution, one pending Entitlement, and one provisioning activity |
| Environment | Django production settings, database, structured log collection |
| Rollout | stats/inspect, explicit bounded activation, supervised enablement |

Fulfillment may run concurrently with financial finalization: it selects only
committed obligations without an execution and validates the committed
`commercial.fulfillment.requested` outbox authority. It never calls Financial
Runtime and Financial Runtime never calls provisioning.

## Initial alerts

These are operational starting defaults, not domain rules.

| Signal | Warning | Critical | Operator action |
| --- | --- | --- | --- |
| Oldest pending verification | 5 minutes | 15 minutes | inspect stats/work, provider health, callbacks and leases; run one bounded batch |
| Oldest pending finalization | 10 minutes | 30 minutes | inspect recognition/finalization classification and reservation/review ownership; safely rerun |
| Manual-review backlog | 1 case older than 15 minutes | 5 cases or oldest over 60 minutes | finance/Admin review; never force paid |
| Failed Financial Runtime invocations | 2 consecutive | 5 consecutive or 15 minutes without success | stop overlapping launch actions; inspect sanitized logs, settings and leases |
| Oldest unactivated fulfillment | 5 minutes | 15 minutes | inspect obligation/outbox coherence; run bounded activation |
| Activation failures | 1 invocation | 3 consecutive or any obligation over 15 minutes | inspect obligation/event IDs and operator queue; do not provision directly |
| Callback without verification progress | 5 minutes | 15 minutes | inspect correlated PaymentAttempt, callback receipt and verification work; never infer payment from callback |
| Runtime backlog | 100 open items | 500 open items | increase bounded invocation frequency only after DB/runtime inspection |
| Fulfillment intake backlog | 50 obligations | 200 obligations | inspect activation failures and scheduler health |

## Required supervisor behavior

- provide the complete production environment without printing secrets;
- collect stdout, stderr, exit status, start/end timestamps, and duration;
- prevent routine overlap and enforce the timeout;
- alert on non-zero exit, missed cadence, backlog, and oldest-age signals;
- use graceful termination, then rely on lease expiry or transaction rollback;
- never retry in a tight loop; the durable runtime owns backoff;
- never use the migration pre-start hook as a recurring scheduler.
