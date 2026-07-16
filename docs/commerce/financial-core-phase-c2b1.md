# Financial Core Phase C2B1 — Implementation Review

Status: **READY FOR REVIEW; UNCOMMITTED AND DORMANT**.

Authority: the locked Financial Core, C2 Provider Integration Architecture, C1, and C2A. C2B1 records and verifies provider evidence but cannot recognize funds. `Payment.confirmed_amount`, paid states, provider-receipt journals, reservations, inventory, Orders, and fulfillment remain outside this phase.

## Evidence boundaries

`CallbackReceipt` represents every HTTP delivery. It stores bounded metadata, exact-envelope SHA-256, keyed hashes of allowlisted headers/account/network hints, authentication and replay classifications, duplicate linkage, quarantine classification, correlation identity, and a 90-day retention marker. It deliberately stores no raw body, unrestricted headers, credentials, or customer PII. Operational access therefore exposes hashes and sanitized classifications only. Infrastructure encryption at rest is not asserted by this implementation.

`ProviderEvent` is normalized evidence, not payment truth. A receipt links to at most one event through append-only `ProviderEventReceipt`. Authenticated trustworthy event IDs deduplicate inside the exact merchant-account version; unsigned evidence falls back to account-scoped envelope hash. Same trusted event ID with changed bytes is quarantined as contradictory. Equality of unsigned payloads cannot cross account boundaries. Unknown references remain unassociated and never create an Order, Payment, Attempt, or Transaction.

Callback transport policy is POST-only, JSON/form only, body limit 64 KiB, at most 32 headers, 8 KiB total header evidence, and 1 KiB per header. Authentication receives exact raw bytes outside every database transaction. HMAC/asymmetric/mTLS declarations are provider-neutral; only a synthetic test adapter exists. TLS and IP evidence are supplemental. Invalid signatures/replay windows produce security evidence and a generic acknowledgement, with no aggregate transition. The dormant callback view has a configured throttle boundary but is intentionally absent from URL configuration.

## Verification boundary

Every provider verification execution produces a new append-only `Verification`. It preserves transaction/provider/account/capability versions, sequence, trigger, merchant/provider references, requested and observed provider-unit money, canonical IRR allocation, normalized outcome/effect/finality/transport, sanitized evidence references/hashes, correlation/causation, retryability, and application classification.

`VerificationWorkItem` is dormant durable work with deterministic identity, due time, bounded attempts, and a short lease. `VerificationClaim` is append-only. Claiming locks Order → Payment → all Attempts → all Transactions, records the lease/event, commits, and returns an immutable envelope. Adapter verification/query executes only after commit and is rejected inside `atomic`. Application reacquires the same locks, validates the current token and lease, stores immutable evidence, applies only C2B1-safe projections, appends review/event/outbox evidence, and commits. Lease expiry after possible I/O is never interpreted as unpaid; the immutable old claim becomes stale and a new bounded read-only verification/query claim may recover the work.

Accepted success evidence requires exact provider, adapter contract, account/version, merchant reference, operation, authority/reference ownership, provider amount/unit, canonical IRR allocation, and final paid semantics. “Already verified” is accepted only when a fresh query supplies the same exact evidence. Provider-unit comparison occurs before canonical association, without tolerance, rounding, float, or callback conversion. A globally unique `ProviderReferenceAllocation` prevents one account/version reference from owning multiple obligations.

## Result projections

- Confirmed paid verification is `APPLIED_BLOCKING_SUCCESS`: Payment/Attempt/Transaction enter review-safe projections, holds remain, a critical ReviewCase and deduplicated future `APPLY_VERIFIED_FUNDS` work are created, and all recollection is blocked. Payment is not paid, confirmed amount is unchanged, and no journal or commercial mutation occurs.
- Final authenticated decline, canceled-unpaid, expired-unpaid, or capability-guaranteed final not-found becomes `APPLIED_UNPAID` only when aggregate evidence has no other blocker. Only then may the attempt become definitively failed and Payment reopen. Reservations are not released by C2B1.
- Pending/no-effect evidence retains processing and schedules another check.
- Timeout, unknown, mismatch, contradiction, invalid ownership, duplicate reference, or security evidence retains holds, blocks recollection, and opens/escalates one deterministic ReviewCase through append-only events.
- Weaker or later evidence cannot downgrade an existing successful Verification. Late success after terminal unpaid evidence is preserved and enters late-payment review without rewriting the terminal provider projection.

## Database and lock guards

Migrations `0008` and `0009` are additive. They create evidence/work tables and constraints, add the explicit capability declaration for final not-found, install append-only PostgreSQL triggers, protect work identity/terminal history, block new attempts and Payment reopening when verification/review evidence exists, block definitive failure over successful evidence, and prohibit C2B1 provider-receipt journal sources. Existing C1/C2A guards remain in place.

The universal order remains Cart → Checkout → Order → Payment → PaymentAttempt → PaymentTransaction → commercial resources → reservations → fulfillment → journal → ReviewCase → events/outbox. Callback receipt ingestion is isolated and takes no aggregate locks. Verification starts again at Order. Collections use stable primary-key ordering. No helper performs provider I/O while atomic.

## Privacy and operations

No raw callback payload is retained, so forensic recovery is intentionally limited to exact hashes and allowlisted normalized fields. If future providers require restricted encrypted raw evidence, C2B2 must introduce a separately authorized encrypted store, access audit, and deletion/retention process; database encryption is not claimed here. References shown to operators must be masked by future UI/API policy. Application logs and external responses contain no raw body, credential reference, signature, customer ownership, or internal matching result.

Rate limiting is configured as a boundary but the route remains unwired. Production rollout must additionally define edge/WAF limits, proxy-normalized client-network handling, retention deletion under legal hold, least-privilege operational roles, key rotation/overlap windows, incident alerting, and applicable privacy/GDPR lawful-basis and deletion rules.

## Migration and dormancy

Graph:

```text
financial_core.0007
  -> financial_core.0008 (models and constraints)
  -> financial_core.0009 (PostgreSQL guards)
```

There is no data migration: no historical callback/success inference, provider/account activation, Verification synthesis, state rewrite, ReviewCase creation, or reservation release. Migrations leave every new table empty. The production adapter registry is empty; no callback URL, worker, signal, cron, customer/Admin API, frontend, real provider credential/algorithm, or ordinary-traffic import exists.

Validation used isolated PostgreSQL 18.4. Migration from zero passed through Financial Core `0009`. The exact C2A upgrade started at `financial_core.0007`, seeded inactive provider/capability/account versions, and applied `0008`–`0009`; the preservation hash remained `a8151032064b18d3c55ab96b2691e35f`, all new evidence/work tables remained empty, and 32 non-internal Financial Core triggers were installed. Focused C2B1 tests passed 16/16, combined Financial Core tests passed 76/76, and the complete backend passed 350/350 with no reported skips. Django system checks, migration-drift checks, Python compilation, and `git diff --check` passed.

## C2B2 hard prerequisites

C2B2 requires separate architecture approval and must implement authenticated verified-success application, the balanced provider-receipt Journal entry, confirmed Payment amount update, `PAID_PENDING_FINALIZATION`, idempotent commercial finalization, Standard/Digital reservation and inventory consumption, fulfillment creation, paid-but-finalization-failed recovery/manual review, and active reconciliation/retry workers. Browser state must never own payment truth.

No commit, push, deployment, staging/production access, provider activation, or C2B2 behavior is part of C2B1.

**FINANCIAL CORE C2B1 READY FOR REVIEW**
