# Digital commerce runtime operations

This runbook supervises the existing bounded commands. It does not authorize
domain mutation outside their services.

## Normal operation

Initial supervised cadence:

```shell
python manage.py financial_runtime run-batch --limit 50 --apply
python manage.py activate_digital_fulfillment run-batch --limit 50 --apply
```

Each command is bounded and exits. Normal empty queues emit an empty result and
a zero-count summary with exit code 0. Successful work emits identifiers and a
summary. The scheduler records output, duration, and exit code.

Read-only inspection:

```shell
python manage.py financial_runtime stats
python manage.py financial_runtime inspect --stage all --limit 50
python manage.py activate_digital_fulfillment stats
python manage.py activate_digital_fulfillment inspect --limit 50
```

Mutation is explicit:

```shell
python manage.py financial_runtime run-one --stage verification --work-id <id> --apply
python manage.py financial_runtime run-one --stage recognition --work-id <id> --apply
python manage.py financial_runtime run-one --stage finalization --work-id <id> --apply
python manage.py financial_runtime retry --stage <stage> --work-id <id> --apply
python manage.py financial_runtime run-batch --limit 50 --apply
python manage.py activate_digital_fulfillment run-one --obligation-id <uuid> --apply
python manage.py activate_digital_fulfillment run-batch --limit 50 --apply
```

`financial_runtime retry` only makes eligible, unclaimed, nonterminal work due.
It cannot reopen canceled/exhausted work. `--apply` is required for every
mutation; without it the commands inspect only.

## Incident procedures

### Payment stuck before verification

Inspect Financial Runtime stats and the verification work identifier. Confirm
provider configuration, callback correlation, next-attempt time, and lease.
Allow active leases to finish. Reclaim happens only after expiry. Run a
bounded batch; use explicit retry only for eligible work. Do not query Zarinpal
manually and record the answer as payment truth.

### Payment verified but not finalized

Inspect recognition and finalization counts, last safe classification, review
ownership, and reservation state. Run one bounded aggregate batch. If the work
is exhausted or review-required, resolve it through the existing ReviewCase
boundary rather than reopening work.

### Finalization complete but fulfillment absent

Run fulfillment `stats` and `inspect`. Confirm the obligation has exactly one
due `commercial.fulfillment.requested` outbox event. Run `run-one --apply`.
Never call provisioning from a shell or create an Entitlement directly.

### Fulfillment activation failure

Use the obligation ID and safe error code from command output. Inspect
obligation/outbox coherence and whether an execution already exists. Correct
only the authoritative upstream defect, then replay the same activation
command.

### Stale lease or process interruption

Do not clear claim fields. Wait for lease expiry and invoke the bounded command
again. A transaction interrupted before commit rolls back; mutation committed
before output is recovered by idempotent replay.

### Provider timeout or unknown outcome

Do not mark failed or release inventory. The work retains PAYMENT_HOLD and the
runtime's bounded backoff. Prolonged uncertainty follows existing review
policy.

### Callback replay

Repeated callbacks correlate with the same durable receipt/work. Inspect
verification progress; do not execute recognition in the request path and do
not treat callback status as paid authority.

### Contradictory late success

Use the existing LATE_PAYMENT ReviewCase maker/checker adjudication. Never
reuse ordinary retry, bypass terminal state, or create exceptional recognition
authority manually.

### Manual-review case

Use the authorized Admin/review workflow. Distinct maker/checker rules and
immutable evidence remain mandatory. A scheduled command must not endlessly
retry terminal review-owned work.

## Exit and partial failure

- `0`: empty queue, successful mutation, coherent replay, or skipped active
  contention.
- non-zero: invalid configuration/target, processing failure that scheduled a
  retry, review escalation during invocation, or one or more fulfillment
  activation failures.

Financial output includes per-work results followed by `summary={...}`.
Fulfillment output includes activated rows, isolated failures, and summary.
Healthy fulfillment rows may commit even when another row fails; the non-zero
exit makes that partial result visible to supervision.

## Configuration inventory

No values belong in this runbook or source control.

| Setting | Classification | Operational purpose |
| --- | --- | --- |
| `FINANCIAL_RUNTIME_VERIFICATION_LEASE_SECONDS` | configurable | verification claim lease |
| `FINANCIAL_RUNTIME_RECOGNITION_LEASE_SECONDS` | configurable | recognition claim lease |
| commercial finalization five-minute lease | fixed safely in code | bounded finalization claim recovery |
| `FINANCIAL_RUNTIME_RETRY_BASE_SECONDS` | configurable | initial backoff |
| `FINANCIAL_RUNTIME_RETRY_MAX_SECONDS` | configurable | maximum runtime backoff |
| `FINANCIAL_RUNTIME_MAX_BATCH_SIZE` | configurable | command hard ceiling |
| `DIGITAL_PAYMENT_PROVIDER_PENDING_HOLD_SECONDS` | configurable | provider-pending hold |
| `DIGITAL_PAYMENT_VERIFICATION_PENDING_HOLD_SECONDS` | configurable | verification-pending hold |
| `DIGITAL_PAYMENT_REVIEW_HOLD_SECONDS` | configurable | review hold |
| `DIGITAL_PAYMENT_ABANDONMENT_SECONDS` | configurable | authoritative abandonment eligibility |
| `DIGITAL_PAYMENT_NOMINAL_EXPIRY_RENEWAL_SECONDS` | configurable | nonterminal verified-success renewal |
| `DIGITAL_PAYMENT_FINALIZATION_RETRY_SECONDS` | configurable | initial finalization retry |
| `DIGITAL_PAYMENT_FINALIZATION_RETRY_MAX_SECONDS` | configurable | finalization retry ceiling |
| scheduler cadence/timeout/overlap | missing; operationally required | external platform configuration |
| process stdout/stderr collection | platform-required | structured supervision/log retention |
| `FINANCIAL_ZARINPAL_ENABLED` | configurable | adapter enablement |
| `ZARINPAL_MERCHANT_ID` | secret external configuration | merchant credential |
| `ZARINPAL_SANDBOX` | configurable | explicit sandbox/production selection |
| `ZARINPAL_REQUEST_URL`, `ZARINPAL_VERIFY_URL`, `ZARINPAL_STARTPAY_URL` | configurable with safe defaults | provider endpoints |
| `ZARINPAL_CONNECT_TIMEOUT_SECONDS`, `ZARINPAL_READ_TIMEOUT_SECONDS` | configurable | bounded provider I/O |
| `FINANCIAL_PROVIDER_CALLBACK_BASE_URL` | configurable; launch-required | fixed Backend callback base |
| `DIGITAL_PAYMENT_CUSTOMER_RETURN_BASE_URL` | configurable; launch-required | fixed safe customer return base |

## Forbidden recovery

Never:

- mutate payment, order, reservation, work, outbox, fulfillment, or entitlement
  rows directly;
- force paid or change immutable provider evidence;
- consume a reservation directly;
- activate an Entitlement directly;
- delete work or outbox rows;
- fabricate provider success;
- call provisioning outside the approved command/service;
- use callback claims as verified funds;
- expose or print merchant credentials.

## Future rollout order

Do not execute these steps from this document:

1. Commit and release reviewed Slice F/G/I/J changes.
2. Apply existing migrations.
3. Configure Zarinpal non-secret endpoints and secret merchant ID.
4. Run Django system checks.
5. Run `configure_zarinpal` inspection.
6. Run `configure_zarinpal --apply`.
7. Verify callback and customer-return URLs.
8. Run Financial Runtime and fulfillment commands in inspect/stats mode.
9. Execute controlled sandbox certification.
10. Enable the supervised Financial Runtime schedule.
11. Enable the supervised fulfillment activation schedule.
12. Run one smoke transaction.
13. Confirm operator queue and customer visibility.
14. Activate production traffic only after release-gate approval.
