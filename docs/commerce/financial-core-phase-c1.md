# Financial Core Phase C1 — Implementation Compliance Report

Status: **READY FOR CHECKPOINT COMMIT; C1 REMAINS DORMANT**
Architecture authority: `FINANCIAL_CORE_ARCHITECTURE_LOCK.md`
Implementation baseline: Commerce B1 `7b627255efcbf758f8ada43e156ac9d7efc52220`, Digital Batch A `c8fff5c2f8176ca85638922465d0e0da37dd8f4e`, Batch B checkpoint `ca42ce57668dce4620dbcd317b2ef2df656dbea7`, and PostgreSQL scheduling compatibility fix `7fa914a8512aade42c88ab234322a46d95b654d5`
Runtime status: dormant; no feature activation, provider integration, URL, Admin, callback, verification, inventory consumption, fulfillment, or frontend behavior

## 1. Architecture compliance

| Locked requirement | C1 implementation | Result |
|---|---|---|
| Orders own commerce | `Payment.order` is a protected one-to-one obligation owner | compliant |
| Payments own obligations | `Payment` persists immutable amount/currency and separate collection/refund projections | compliant |
| PaymentAttempts own customer tries | Ordered, immutable attempt identity; failed attempts cannot be reopened; live/unknown/review attempts block retry | compliant |
| PaymentTransactions are provider operations | Operation type, parent operation, merchant/provider identity, exact internal/provider money, write-once evidence | compliant |
| Provider truth is authoritative | C1 command services explicitly refuse `SUCCEEDED`, `PAID_PENDING_FINALIZATION`, and `PAID`; those transitions remain reserved for later verified finalization | compliant and dormant |
| Financial journal is append-only | Balanced Entry/Posting service, ORM immutability, PostgreSQL raw-SQL mutation triggers, deferred per-currency balance trigger | compliant |
| ReviewCase is first-class | Aggregate references, reason/severity/queue state, immutable ownership, append-only ReviewAction, maker/checker guard | compliant |
| Financial events are append-only | Versioned aggregate timeline, correlation/causation/idempotency, allowlisted metadata, ORM + PostgreSQL guards | compliant |
| State machines are explicit | Payment, Attempt, Transaction, Review, Reconciliation Run/Finding transition maps plus DB enum constraints | compliant |
| External I/O outside transactions | `assert_external_io_allowed()` fails inside an atomic block; no C1 external I/O exists | compliant |
| Universal lock order | Ranked lock scope and stable-key enforcement; all C1 Payment/Attempt/Transaction/Review/Journal commands use it | compliant |
| Idempotency is scoped and hashed | Canonical request hash, `(scope,key)` uniqueness, conflict/in-progress/completed/failed states, immutable identity | compliant |
| Reconciliation is foundational | Run and deduplicated Finding models/services, state/timestamp constraints, optional ReviewCase link | compliant |
| Inventory/payment/fulfillment independence | No inventory or fulfillment import/mutation in C1 services | compliant |

## 2. Files changed by C1

- `config/django/base.py` — registers the dormant Financial Core app.
- `cheatgame/financial_core/apps.py`
- `cheatgame/financial_core/models.py`
- `cheatgame/financial_core/migrations/0001_initial.py`
- `cheatgame/financial_core/migrations/0002_postgresql_financial_guards.py`
- `cheatgame/financial_core/migrations/0003_enforce_financial_state_constraints.py`
- `cheatgame/financial_core/services/boundaries.py`
- `cheatgame/financial_core/services/events.py`
- `cheatgame/financial_core/services/idempotency.py`
- `cheatgame/financial_core/services/journal.py`
- `cheatgame/financial_core/services/locks.py`
- `cheatgame/financial_core/services/payments.py`
- `cheatgame/financial_core/services/reconciliation.py`
- `cheatgame/financial_core/services/reviews.py`
- `cheatgame/financial_core/services/state_machines.py`
- `cheatgame/financial_core/test_financial_core.py`
- `cheatgame/shop/services/commerce_foundation.py` — restores Cart-before-Checkout order in the dormant legacy manual-review helper.
- `cheatgame/shop/test_commerce_foundation.py` — asserts the helper's emitted PostgreSQL lock order.
- package/migration `__init__.py` files
- this report

Approved Batch B files were used as the baseline and were not redesigned by C1.

## 3. Migration graph

```text
shop.0017_checkoutshippingsnapshot_is_pricing_finalized
└── shop.0018_cartitem_commerce_authority
    └── shop.0019_checkoutline_commerce_authority

product.0019_category_name_not_globally_unique
└── product.0020_deliveredversion_product_commerce_authority
    └── digital_products.0001_initial
        └── digital_products.0002_digitalcartselection
            └── digital_products.0003_digitalcheckoutlinesnapshot
                └── digital_products.0004_digitalinventoryreservation

shop.0019 + digital_products.0004 + AUTH_USER_MODEL
└── financial_core.0001_initial
    └── financial_core.0002_postgresql_financial_guards
        └── financial_core.0003_enforce_financial_state_constraints
```

All C1 migrations are additive. They create no Payment, Attempt, Transaction, journal, ReviewCase, event, idempotency, or reconciliation rows. They do not alter legacy `shop.PaymentTransaction` or existing Orders.

## 4. Database model review

### Aggregate models

- `Payment`: exactly one per current Order; immutable Order/amount/currency/public identity; separate confirmed/refunded amounts and collection/refund state axes.
- `PaymentAttempt`: immutable payment, sequence, amount, currency, tender/provider account, command identity, and request hash; mutable lease/state projection only.
- `PaymentTransaction`: immutable attempt/operation/provider/account/merchant reference/money identity; provider evidence fields are write-once; terminal timestamps are constrained.

### Journal

- `FinancialAccount` stores immutable account identity/type/currency and mutable operational status.
- `JournalEntry` and `JournalPosting` are append-only.
- Source and idempotency uniqueness prevent duplicate posting.
- Service validation and deferred PostgreSQL triggers require at least two postings and debit=credit at commit; C1 accepts canonical IRR only, so mixed-currency journals cannot be created.
- No stored mutable balance exists; balances derive from postings.

### Safety/operations

- `ReviewCase` and append-only `ReviewAction` provide controlled uncertainty ownership.
- `FinancialEvent` provides append-only versioned state history with safe metadata.
- `IdempotencyRecord` owns command replay/conflict identity.
- `ReconciliationRun` and `ReconciliationFinding` provide durable, deduplicated findings.

Twelve new PostgreSQL tables, all expected indexes, uniqueness constraints, check constraints, and eleven distinct database triggers were observed after migration.

## 5. Transaction boundary review

- Local commands are short `transaction.atomic` boundaries.
- Payment creation locks Order before creating Payment.
- Attempt/Transaction creation and transitions resolve identifiers without locks, then restart at the earliest applicable canonical lock rank.
- Provider/network behavior is absent. The explicit I/O guard rejects calls made inside an atomic block.
- Journal entry and all postings commit atomically; deferred balance validation executes at commit.
- Events are written last in the same transaction as aggregate state change.
- Review creation locks referenced commercial/financial aggregates before creating the case/event.
- Idempotency claiming/completion uses separate short coordination transactions.

Paid finalization, refunds, inventory consumption, and fulfillment boundaries are intentionally not implemented.

## 6. Lock-order review

C1 ranks are:

1. Cart
2. Checkout
3. Payable (Order/Invoice)
4. Payment
5. PaymentAttempt
6. PaymentTransaction
7. commercial lines
8. commercial resources
9. reservations
10. fulfillment
11. journal accounts
12. journal records
13. ReviewCase
14. events/outbox

`ordered_lock_scope`, `register_lock`, `lock_one`, and `lock_many` reject descending rank and descending same-rank stable keys. Multi-row locks sort primary keys. C1 services never acquire Cart/Checkout because C1 does not place Orders; they begin at Order and preserve the remaining rank order.

## 7. Commit-boundary restoration

Starting branch/HEAD was `integration/commerce-b1-digital-products` at Batch A `c8fff5c2f8176ca85638922465d0e0da37dd8f4e`, with Batch B and C1 combined in one dirty worktree.

The exact combined state was preserved on local-only safety branch `safety/financial-core-c1-combined-20260716`, commit `3539ef07f1e68aadca8f9ca32c12915f8489a2e4`. Batch B paths were restored onto Batch A and compared against the safety tree through the Git index before committing. Resulting local-only history:

1. Batch A: `c8fff5c2f8176ca85638922465d0e0da37dd8f4e`
2. Batch B: `ca42ce57668dce4620dbcd317b2ef2df656dbea7`
3. PostgreSQL scheduling compatibility: `7fa914a8512aade42c88ab234322a46d95b654d5`
4. C1: uncommitted

The restored C1 files matched their pre-separation SHA-256 inventory byte-for-byte. Subsequent uncommitted changes are only the release-gate hardening documented here. Nothing was pushed.

## 8. PostgreSQL 18 migration validation

Runtime: `PostgreSQL 18.4 (Homebrew) on x86_64-apple-darwin23.6.0`, 64-bit.

### Migration from zero

A brand-new disposable `c1_zero_final` database applied the complete repository graph, including Financial Core `0001`, `0002`, and `0003`. `manage.py check` passed and `makemigrations --check --dry-run` reported no changes.

### Exact upgrade from Batch B

A separate disposable database was migrated only through Shop `0019` and Digital Products `0004`. It was seeded with two users and Orders, Standard Product/Cart/CheckoutLine/shipping snapshot/StockReservation, Digital Product/DeliveredVersion/Offer/InventoryPool/Cart/CheckoutLine/snapshot/DigitalInventoryReservation, and one legacy `shop.PaymentTransaction` carrying explicit IRT request evidence.

The canonical snapshot hash before C1 was:

`fa3eecdfa8910bf7f42d66b118bc18d3426d902fcdb79e714746e4654a5495b8`

After sequentially applying Financial Core `0001`, `0002`, and `0003`, the complete snapshot hash was identical. All eleven Financial Core aggregate/infrastructure row counts were zero; the legacy PaymentTransaction count remained one and its amount/unit evidence remained unchanged.

The graph has one linear Financial Core leaf, used no fake migration, and introduced no data migration. Forward SQL creates only new Financial Core tables, constraints, functions, and triggers. It does not update/delete existing rows or alter Shop, Digital, Product, or User tables. Lock risk is limited to ordinary brief DDL/FK validation locks while creating new tables referencing Order/User; there is no existing-table rewrite or long-running data scan.

## 9. IRR and legacy IRT compliance

1. `Payment.currency` can contain only `IRR`; service validation and PostgreSQL checks both enforce it.
2. `JournalPosting.currency` can contain only `IRR`; FinancialAccount currency is also IRR-only in C1.
3. C1 cannot write IRT into the canonical journal.
4. IRT-to-IRR conversion is intentionally not implemented in C1.
5. No multiplication occurs, so there is no float, rounding, or double-conversion path. The future bridge must implement exact integer/Decimal `IRT × 10 = IRR`.
6. Existing legacy values and their original unit evidence are untouched; the upgrade rehearsal preserved `amount_unit=IRT` byte-for-byte.
7. Mixed-currency entries are impossible because accounts and postings are constrained to IRR, in addition to commit-time balancing.
8. C1 does not derive a Payment from an old Order total. Order-to-Payment creation is deferred, preventing silent reinterpretation or double multiplication.
9. Every new money record has an explicit constrained currency/unit; blank or ambiguous canonical amounts are impossible.
10. Provider comparison/conversion is deferred with provider adapters. C1 accepts provider unit IRR only, so it cannot ingest unconverted IRT evidence.

The explicit IRT compatibility bridge is a hard blocker before any payment request/provider integration.

## 10. Model, constraint, and trigger findings

- Payment is one protected one-to-one obligation per Order; public ID, owner, amount, and currency are immutable in ORM and PostgreSQL.
- Attempt sequence is unique per Payment and positive. Terminal succeeded/definitive-failed attempts cannot be reopened or deleted, including through raw SQL.
- Live, successful, unknown, and review attempts block a new collection attempt. Retry after definitive failure creates a new sequence.
- PaymentTransaction provider operation identity and money terms are database-protected; evidence is write-once and rows cannot be deleted.
- C1 services reject successful Transaction/Attempt, paid Payment states, and every confirmed-amount write.
- A PostgreSQL evidence trigger prevents nonzero confirmed funds or paid projections without successful sale/capture and attempt evidence.
- JournalEntry requires at least two postings and balances at deferred commit time. JournalEntry, JournalPosting, FinancialEvent, and ReviewAction reject raw SQL update/delete.
- ReviewCase resolution uses a named command, creates an append-only ReviewAction first, validates maker/checker, and is blocked by PostgreSQL if no resolution action exists.
- Command idempotency hashes reject payload mismatch. PaymentTransaction replay also compares its complete immutable request identity.
- Reconciliation run idempotency is unique; finding identity is unique within a run and safely repeatable across runs.
- Ownership uses explicit protected foreign keys, not generic/polymorphic owner fields. Review service normalizes Order/Payment/Attempt/Transaction ancestry before locking and creation.

Eleven distinct Financial Core triggers were observed: four append-only guards, two deferred journal guards, Payment obligation/evidence guards, Attempt/Transaction guards, and the ReviewCase resolution-action guard.

## 11. Universal lock-order integration

The implemented ranks are Cart → Checkout → payable Order → Payment → Attempts → Transactions → commercial lines/resources → reservations → fulfillment → journal accounts/records → ReviewCase → events/outbox.

- C1 collection commands start at Order and proceed forward.
- Collections are deduplicated and locked in ascending primary-key order; both descending rank and descending same-rank requests are rejected.
- The dormant Phase A helper now resolves Checkout identity without a lock, then locks Cart → Checkout → legacy PaymentTransaction; a PostgreSQL query-capture test proves the order.
- Existing Cart/Checkout preparation paths may enter Financial Core only through a fresh Order-placement boundary after their transaction commits. No current active route calls C1.
- No lock helper performs provider I/O. The explicit boundary guard rejects external I/O inside `atomic`.
- Concurrency tests cover two attempts, attempt creation versus ReviewCase creation, journal posting versus reconciliation work, stable collection ordering, and lock-order violations.

## 12. Scheduling compatibility correction

The previous three errors came from `reserve_delivery_data`: an unqualified `FOR UPDATE` targeted a query containing nullable outer joins to DeliverySchedule and Address. Repository audit found one unsafe call site; the analogous Order path was already correct.

Commit `7fa914a8512aade42c88ab234322a46d95b654d5` changes only that query to `select_for_update(of=("self",))`, while retaining the earlier authoritative DeliverySchedule lock. Two focused PostgreSQL tests prove the concrete lock target and capacity-one concurrent serialization. This correction is separate from Batch B and C1.

## 13. PostgreSQL 18 regression matrix

| Suite | Result |
|---|---:|
| Financial Core C1 | 27/27 |
| Digital Products Batch A | 16/16 |
| Digital Products Batch B | 18/18 |
| Commerce B1 | 33/33 |
| Commerce foundation | 22/22 |
| Product | 27/27 |
| Legacy Shop | 64/64 |
| Repair/Issue plus scheduling lock tests | 35/35 |
| Authentication plus Users | 12/12 |
| Migration pipeline | 18/18 |
| Full backend | 301/301 |

There were zero failures and zero reported skips. `manage.py check`, migration drift check, whole-backend Python compilation, and `git diff --check` passed.

## 14. Dormancy proof

- No Financial Core URL, API, serializer, Admin registration, signal, task, scheduler, callback, provider adapter, gateway verification, refund execution, inventory consumption, Order finalization, fulfillment execution, frontend integration, or feature activation exists.
- Active application code outside the Financial Core has no Financial Core import. The only runtime integration is dormant app registration.
- No migration creates Financial Core records.
- All twelve Financial Core tables were empty after the full 301-test run; focused C1 tests create only isolated synthetic test rows that are flushed.
- Legacy provider/payment routes remain unchanged and do not call C1.
- Staging and production were not touched.

## 15. C2 prerequisites and remaining risks

C2 may not start until separately approved. Its prerequisites are:

- explicit Order-to-Payment obligation placement at the atomic placement boundary;
- a legacy IRT-to-canonical-IRR compatibility bridge with exact `IRT × 10 = IRR`, original-value/unit evidence, and double-conversion protection;
- provider unit declaration and comparison before canonical accounting conversion;
- a provider adapter contract and merchant-account versioning;
- payment request idempotency;
- immutable PaymentAttempt creation for every collection try;
- backend-owned callback processing;
- backend server-to-server provider verification with append-only verification evidence;
- unknown provider outcome recovery;
- provider-confirmed journal posting under canonical locks;
- ReviewCase entry when the provider is paid but local finalization fails;
- idempotent commercial finalization;
- reconciliation and retry workers with bounded, safe retry policy;
- no browser-owned payment truth;
- reservation consumption and fulfillment boundaries approved separately.

Remaining risks are intentionally dormant capabilities, not C1 defects: no provider evidence model/adapter, no legacy currency bridge, no Order placement integration, and no paid finalizer exist yet. These are hard activation blockers.

## 16. Review disposition

Batch B and the unrelated PostgreSQL compatibility correction are committed locally and unpushed. Financial Core C1 is approved for its separate local checkpoint commit. No push, deployment, or feature activation occurred.

**FINANCIAL CORE C1 READY TO COMMIT**
