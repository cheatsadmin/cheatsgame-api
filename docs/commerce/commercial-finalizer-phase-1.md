# Commercial Finalizer Phase 1

Status: implemented, dormant, uncommitted, and not deployed.

## Authority and boundary

`finalize_paid_commerce` is the only Phase 1 command that converts an exactly funded
`PAID_PENDING_FINALIZATION` Payment into accepted commerce. It accepts an immutable Payment
identity, expected version, idempotency identity, correlation/causation identity, and a controlled
system or reconciliation actor. It accepts no inventory amount, target Order, accounting account,
or fulfillment destination from its caller.

The command performs no provider I/O. It has no URL, API, Admin action, signal, task, scheduler,
cron entry, or active worker.

## One atomic commercial transaction

The transaction locks Checkout, Order, Payment, financial evidence, accounting policy, immutable
commercial lines, commercial resources, reservations, fulfillment identity, journal accounts and
records, ReviewCases, and events/work in the universal order.

Within that transaction it:

1. proves the Payment is exactly funded in IRR and reconciles to immutable FinancialAllocations;
2. proves Order, Checkout, frozen lines, authority, and reservations are coherent;
3. consumes either Standard StockReservations and Product quantity or Digital reservations and
   InventoryPool sellable quantity;
4. creates concrete immutable Standard or Digital fulfillment obligations;
5. posts one balanced commercial reclassification Journal;
6. records one immutable CommercialFinalization;
7. moves Order to paid/processing, Checkout to paid, and Payment to PAID;
8. completes the existing dormant finalization work and appends financial and commercial events.

Any exception, deferred-trigger failure, journal failure, event failure, or work completion failure
rolls the entire transaction back. The prior provider receipt, FinancialAllocation, confirmed amount,
and `PAID_PENDING_FINALIZATION` state predate this transaction and remain authoritative. Collection
therefore remains blocked and retry is deterministic.

## Inventory and fulfillment

Standard and Digital commerce remain isolated. Mixed authority is rejected.

- Standard: Product rows are locked by primary key, aggregate reservation quantities are checked,
  Product quantity is decremented, reservations become CONSUMED, and one immutable
  StandardFulfillmentObligation is created per OrderItem.
- Digital: InventoryPool rows are locked by primary key, reservation/snapshot ownership is checked,
  pool availability is decremented, reservations become CONSUMED, and one immutable
  DigitalFulfillmentObligation is created per reservation/OrderItem.

No delivery execution or credential allocation occurs. Fulfillment obligations are durable work owed
by the platform, not proof that delivery has happened.

## Commercial accounting

CommercialAccountingPolicyVersion is immutable and versioned by commerce authority. It selects only:

- customer unapplied/deferred-funds liability;
- merchandise revenue;
- shipping revenue.

No policy rows or FinancialAccounts are migration-created. The policy liability must be exactly the
liability credited by every receipt allocation for the Payment. The Journal is IRR-only and performs:

- Debit customer unapplied/deferred funds for the full obligation;
- Credit merchandise revenue for the frozen merchandise component;
- Credit shipping revenue for the frozen shipping component, when non-zero.

No provider conversion occurs. Frozen legacy IRT components use the already-recorded obligation bridge
unit and the exact `IRT × 10` rule solely to express the commercial split in canonical IRR. Revenue,
fees, tax, settlement, payout, refund, and marketplace accounting beyond these explicit frozen
components are not inferred.

## Idempotency and database enforcement

One Payment and one Order can have only one CommercialFinalization. The command identity and complete
fingerprint replay the original result; conflicts fail deterministically. Fulfillment sources and the
Journal source are unique.

PostgreSQL guards make finalizations and fulfillment obligations append-only, protect policy identity,
validate ownership and exact money at commit, validate the Journal against policy, require all
reservations consumed and all fulfillment obligations present, and forbid a raw Payment transition to
PAID without the immutable finalization. Existing C1–Provider Execution Phase 1 guards remain active.

## Dormancy and deferred work

The finalizer is callable only as an internal service in this phase. There is no automatic executor.
The following remain deferred:

- delivery/fulfillment execution;
- paid-finalization retry and reconciliation workers;
- active provider and callback integration;
- refund, chargeback, settlement, fee, tax, seller, and marketplace accounting;
- customer or Admin APIs and frontend behavior;
- operational closure of the existing paid-pending-finalization ReviewCase under the controlled
  ReviewAction/maker-checker workflow;
- deployment and feature activation.

## Validation

Validated on isolated PostgreSQL 18 with migration from zero, deferred constraints/triggers, real
independent-connection concurrency, Standard and Digital success paths, rollback injection, raw-SQL
forgery attempts, idempotency replay/conflict, combined Financial Core regression, and full backend
regression.
