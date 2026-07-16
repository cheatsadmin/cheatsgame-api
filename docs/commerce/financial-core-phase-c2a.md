# Financial Core Phase C2A — Implementation Review

Status: **READY FOR REVIEW; UNCOMMITTED AND DORMANT**.

Authority: the locked Financial Core and C2 Provider Integration architectures. C2A is provider-neutral and dormant. It does not recognize paid funds, call a provider, consume inventory, finalize an Order, create fulfillment, or expose an API.

## Placement and obligation ownership

`place_order_and_create_payment_obligation` is the sole C2A placement boundary. In one atomic transaction it locks Cart, then Checkout; verifies ownership, version, expiry, readiness, immutable lines, isolated commerce authority, finalized Standard shipping, and valid authority-specific reservations; freezes Order and OrderItem values; normalizes the collectible amount; creates exactly one Order and one Payment; moves reservations to payment holds without consuming them; projects Checkout as `PENDING_PAYMENT`; and appends Commerce/Financial events plus a dormant outbox record.

The current Commerce model has no distinct READY or PLACED enum. C2A therefore uses the already-approved compatibility projection: a fully validated DRAFT Checkout is placement-ready and PENDING_PAYMENT means placed with an outstanding obligation. The one-Order-per-Checkout and one-Payment-per-Order constraints are authoritative. Positive zero-value placement is rejected into a separate, not-yet-implemented acceptance boundary; no successful Payment is synthesized.

Standard and Digital authority remain isolated. C2A does not add mixed carts and does not create reservations during placement. Missing, expired, mismatched, or mixed holds reject the entire transaction.

## Legacy IRT compatibility bridge

`normalize_obligation_money` is the only legacy obligation normalization boundary:

- an explicit source unit is mandatory;
- integer or exact zero-scale Decimal values are accepted; float and fractional values are rejected;
- IRT converts exactly as `canonical_irr = source_irt * 10`;
- IRR passes through unchanged;
- source model, object, field, amount, unit, bridge version, canonical result, snapshot hash, and SHA-256 evidence fingerprint are persisted in append-only `PaymentObligationSource`;
- unique source identity and fingerprint constraints detect replay and conflicting conversion;
- provider-unit representation is a separate function and cannot invoke this bridge implicitly.

`adopt_legacy_order_payment_obligation` is an explicit, single-Order compatibility command. It requires the expected owner, explicit unit, inactive legacy ownership evidence, an unpaid Order, no existing Financial Core Payment, and no legacy paid/live/callback/verifying/unknown/review transaction. Ambiguous Orders are rejected for controlled review; there is no bulk adoption or data migration.

## Provider versions and adapter contract

`ProviderDefinition`, immutable `ProviderCapabilityVersion`, and immutable-identity `MerchantAccountVersion` separate provider identity, adapter contract, supported operations, money unit/conversion policy, idempotency/query capability, callback authentication strength, verification semantics, finality/expiry windows, refund/void declarations, ownership, credential reference, and kill switches. Historical Attempts and Transactions retain their exact capability/account versions. Credential references identify protected secret configuration and never contain repository credentials.

The versioned `ProviderAdapter` protocol may serialize/execute an operation, authenticate and normalize callbacks, verify/query an operation, and read reconciliation records. It may not own database aggregates or business transitions. The production registry is empty and allowlisted; arbitrary import paths are unsupported. A synthetic adapter proves conformance and proves execution is rejected inside `transaction.atomic`.

## Provider money representation

Canonical Payment, Attempt, and Transaction money remains IRR. Each request Transaction also stores provider amount, provider unit, and conversion policy version. IRR providers receive the same integer amount. IRT providers receive exact `canonical_irr / 10`; non-divisible IRR is rejected before request creation. No rounding or float path exists. Future verification must compare provider evidence in the persisted provider unit before canonical accounting recognition.

## Attempt, request Transaction, claim, and result boundaries

Attempt creation locks Order, Payment, all Attempts, commercial holds, and unresolved ReviewCase blockers in the universal order. It requires a collectible positive remainder, an eligible versioned account, valid holds, and no live/success/unknown/review evidence. Every safe customer retry creates a new sequence; terminal attempts are never reset.

Request-operation creation records one immutable PaymentTransaction with deterministic merchant reference, versioned provider/account/adapter identity, canonical and provider money, provider idempotency reference where supported, request fingerprint, and correlation/causation identities. It creates no provider authority and cannot mark success.

The claim command locks Order through Transaction, grants a 5–300 second append-only lease, moves the local projections to REQUESTING/PROCESSING, writes event/outbox evidence, commits, and returns an immutable envelope. Production C2A never invokes the adapter. Claim and result commands persist hashed idempotency identities; same key/same payload replays, while any payload mismatch conflicts.

The result boundary accepts only customer-action, pending, definitive decline/cancel/expiry, no-effect retryable, unknown, security, configuration, and protocol outcomes. `CONFIRMED_SUCCESS` is rejected by service and database constraint. Stronger or terminal evidence cannot be downgraded. Unknown or contradictory outcomes put Payment/Attempt/Transaction into blocking review-safe states, preserve holds, append evidence, and open a ReviewCase. Time alone never converts unknown into failure.

## Universal lock order

C2A preserves:

Cart → Checkout → Order → Payment → PaymentAttempt → PaymentTransaction → commercial lines/resources → reservations → fulfillment → journal → ReviewCase → events/outbox.

Collections are locked by ascending primary key. Provider execution is forbidden inside atomic transactions. Active legacy payment creation is rejected for Financial-Core-owned Orders; Checkout cancellation and expiry skip placed Financial Core obligations.

## Events, outbox, and idempotency

C2A appends provider-neutral obligation, adoption, attempt, transaction, claim, result, unknown, and collection-blocked evidence in the same transaction as each state change. Event and outbox payloads are allowlisted; credentials, tokens, authorization material, arbitrary provider payloads, and full PII are excluded. The outbox is append-only and has no consumer, worker, signal, scheduler, or cron.

The independent identities are placement, legacy adoption, Attempt creation, request Transaction creation, request claim, and request-result application. A rolled-back command rolls back both its state and idempotency completion.

## Migration strategy

The C2A graph is additive:

```text
shop.0019 -> shop.0020
digital_products.0004 -> digital_products.0005
financial_core.0003 + shop.0020 + digital_products.0005
  -> financial_core.0004
  -> financial_core.0005
  -> financial_core.0006
  -> financial_core.0007
```

No migration adopts an Order, converts historical amounts, creates a provider/account, or creates any Financial Core row. The migrations add nullable compatibility links, new tables/fields/constraints, and PostgreSQL guards. They do not reinterpret legacy `shop.PaymentTransaction` rows.

## PostgreSQL and regression evidence

Runtime: `PostgreSQL 18.4 (Homebrew) on x86_64-apple-darwin23.6.0`, 64-bit.

- Migration from zero applied the complete graph through Shop `0020`, Digital Products `0005`, and Financial Core `0007`. The final full test database independently rebuilt the same graph from zero.
- The exact C1 upgrade began at Shop `0019`, Digital Products `0004`, and Financial Core `0003`. Synthetic Standard and Digital carts/checkouts/snapshots/reservations, three Orders, one legacy Shop transaction, and one C1 Payment/Attempt/Transaction graph were seeded.
- The final before and after preservation hash was identical: `d9a1f5fc90848288041075dcc4b534805ac51eb6cab42cf7410060c2b3f36c61`.
- C2A migration-created row counts were all zero: providers, capabilities, merchant accounts, obligation sources, request claims, request results, and outbox messages. The legacy transaction and C1 Payment/Attempt/Transaction remained one each.
- Focused C2A, including all required real-connection races: 33/33.
- Financial Core C1 plus C2A: 60/60.
- Complete backend: 334/334, zero failures and no reported skips.
- `manage.py check`, `makemigrations --check --dry-run`, Python compilation, and `git diff --check` passed.

The C2A schema migration performs no data migration and no row update/delete. New nullable compatibility columns avoid table rewrites; new constraints and indexes require ordinary PostgreSQL DDL/validation locks. Before any future deployment, operational preflight must verify no historical Checkout already owns multiple Orders, then schedule the brief DDL lock window. That is a deployment gate, not feature activation.

## Security and dormancy

There are no provider credentials, real adapters, arbitrary adapter imports, customer-provided callback/redirect targets, callback URLs, APIs, Admin actions, provider calls, verification, paid-state authority, journal receipt posting, refunds, reservation consumption, Order finalization, fulfillment, workers, frontend integration, or feature activation. Ordinary routes do not call the placement/request services. Migrations create no domain rows.

## Deferred C2B work — hard prerequisites

C2B requires separate approval and must add:

- callback receipt/event models;
- callback authentication, normalization, replay handling, and quarantine;
- immutable Verification records;
- provider query/verification orchestration outside database transactions;
- authenticated backend-only success application;
- provider receipt journal entry;
- Payment `PAID_PENDING_FINALIZATION` transition based only on verified provider evidence;
- ReviewCase escalation for provider-paid/local-finalization failure;
- idempotent commercial finalization;
- reconciliation and bounded retry workers.

No browser or redirect result may own payment truth.

## Final disposition

No commit, push, deployment, staging/production access, provider operation, or feature activation occurred.

**FINANCIAL CORE C2A READY FOR REVIEW**
