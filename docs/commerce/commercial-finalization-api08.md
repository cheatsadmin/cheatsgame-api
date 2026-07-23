# API-08 Commercial Finalization Boundary

## Status and activation

API-08 is a dormant internal Financial Core boundary. It has no URL, Admin action,
task, scheduler, signal, startup hook, or outbox consumer. Tests and an explicitly
authorized internal caller may invoke `finalize_commercial_work_item`; deployment or
operational activation is outside this checkpoint.

The command is rooted in one `CommercialFinalizationWorkItem`. The only supported
work contract is `commercial-finalizer-v1-dormant`, mapped explicitly to the existing
`commercial-finalizer-v1` engine. The engine remains the sole commercial mutation
authority, but cannot execute without the exact live claim supplied by the work-root
command. Unsupported work or engine versions fail closed.

## Command authority

Inputs are limited to the work public identity, idempotency UUID, expected work and
Payment versions, correlation and causation identities, and a controlled actor.
Allowed actors are SYSTEM and accountable RECONCILIATION or COMMERCIAL_RECOVERY
actors. Customer, browser, callback, provider, fulfillment, ordinary Admin, and
Manager authority is not accepted. The command accepts no money, currency, line,
inventory, reservation, accounting, fulfillment-result, or target-state input.

## Claim, retry, and idempotency

A claim is committed before finalization, has a finite lease, a deterministic claim
identity, bounded attempts, and optimistic work-version validation. A live foreign
claim blocks a second mutation. A concurrent coherent caller waits briefly for the
authoritative commit and replays it; unresolved work remains a stable in-progress
conflict. Expired claims require the authoritative version and are reclaimed without
overwriting claim evidence. Stale claims cannot complete work.

The application fingerprint freezes the work, Payment, Order, Checkout, obligation
source, placement snapshot, commercial revision, lines, OrderItems, reservation set,
recognized FinancialAllocations and receipt Journals, accounting policy, actor,
expected versions, and finalizer contract. Completed replay is resolved before
mutable terminal-state rejection and uses the accounting policy frozen on the
persisted finalization rather than current policy selection. Replay cannot duplicate an inventory decrement,
reservation transition, commitment, Journal, finalization, obligation, review
resolution, event, work completion, or outbox message.

## Financial and commercial revalidation

Under deterministic locks, API-08 independently checks exact recognized IRR funds,
the complete Attempt/Transaction/Verification/allocation/reference graph, successful
financial states, provider-receipt Journals, frozen receipt policy ownership, and the
absence of unresolved financial review.

Commercial authority comes only from the immutable placement obligation and frozen
Checkout graph. Standard and Digital authorities are homogeneous; mixed, service,
unknown, canceled, expired, or incomplete graphs fail closed. OrderItems and
CheckoutLines must match exactly. Merchandise is the exact sum of frozen line totals;
Standard shipping is the frozen shipping snapshot and Digital shipping is zero.
Unsupported hidden components are rejected. There is no repricing, conversion,
rounding, or tolerance.

## Inventory commitment

Standard finalization locks Products and exact PAYMENT_HOLD StockReservations,
aggregates demand by Product, decrements `Product.quantity` once, consumes the
reservations, and creates one immutable `StandardInventoryCommitment` per Product.

Digital finalization locks InventoryPools and exact PAYMENT_HOLD
DigitalInventoryReservations, validates CheckoutLine/Pool authority, aggregates
shared-pool demand, decrements `InventoryPool.sellable_quantity` once, consumes the
reservations, and creates one immutable `DigitalInventoryCommitment` per pool.

Each commitment freezes its finalization, Order, resource, reservation-set digest,
pre-quantity, committed quantity, post-quantity, and correlation/causation identity.
No warehouse, source account, credential, slot, or secret allocation is created.
Digital CheckoutLine and authority-specific snapshot evidence is insert-validated
and database-immutable, freezing offer, version, Pool, console, capacity, method,
revision, and money identity before finalization.

## Accounting and terminal graph

The frozen `CommercialAccountingPolicyVersion` supplies active IRR accounts. The
commercial reclassification Journal is:

- debit customer unapplied-funds liability for the exact Payment amount;
- credit merchandise revenue for exact frozen line totals;
- credit shipping revenue only for a nonzero frozen Standard shipping component.

The liability must be the same account credited by funds recognition. The Journal is
balanced, immutable, and sourced by the `CommercialFinalization` public identity.

One atomic successful transaction creates commitments, consumes reservations,
decrements inventory, posts the Journal, creates `CommercialFinalization`, creates
exactly one authority-specific fulfillment obligation per OrderItem, transitions
Payment to PAID, Order payment to PAID and fulfillment to PROCESSING, Checkout to
PAID, and Cart to OPEN, resolves only the exact system pending-finalization marker,
completes the work and idempotency evidence, appends audit events, and creates one
`commercial.fulfillment.requested` outbox message.

The outbox contains control identities only. It does not execute fulfillment.
`DigitalFulfillmentItem`, credentials, delivery results, and Entitlement remain
outside API-08.

## Failure and integrity

Every commercial mutation is in one database transaction. Any mismatch or injected
failure rolls back inventory, reservations, Journal, finalization, obligations,
projections, marker resolution, work completion, events, and outbox. Recognized funds
and provider-receipt accounting remain unchanged at PAID_PENDING_FINALIZATION. A
separate controlled retry or review boundary may handle the failure; API-08 does not
refund, reverse, or bypass invariants.

Additive PostgreSQL guards make commitments and finalization outbox evidence
append-only and defer validation of the complete terminal graph until commit. The
guards require coherent Payment, Order, Checkout, Cart, work, commitment,
obligation, inventory, review-marker, and outbox projections. Historical rows are not
rewritten. The migrations create schema only and are reversible to the API-07 leaf.
Deferred guards bind terminal Order/Checkout/Cart transitions and Product/Pool
OLD-to-NEW decrements to the same complete finalization. The commercial Journal
permits only its exact policy postings, and the fulfillment outbox requires its
deterministic identity and exact allowlisted control payload.
Commitment insertion also requires the resource's current quantity to equal the
frozen pre-quantity and the exact PAYMENT_HOLD reservation aggregate. The subsequent
resource decrement must match that commitment's pre/post delta in the same atomic
finalization, so neither fabricated commitment quantities nor an uncommitted
inventory decrement can substitute for observed inventory movement.
