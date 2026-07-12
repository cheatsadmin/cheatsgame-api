# ADR-001: Commerce Architecture

## Status

Accepted for staged implementation. Phase A is additive foundation only.

## Context

The legacy checkout creates an Order and removes CartItems before shipping and payment complete. Payment verification is currently initiated by the frontend result page, checkout submission has no durable client idempotency contract, commercial data relies on mutable catalog records, and paid-but-unfulfillable transactions do not have a distinct manual-review state.

These conditions are acceptable only for controlled sandbox testing. They are not acceptable for a real payment gateway.

## Decision

Commerce will use three independent lifecycles:

- Checkout: draft, pending payment, paid, manual review, canceled, expired.
- PaymentTransaction: one record per provider attempt, with created, pending, callback received, verifying, paid, failed, and manual-review states. Multiple attempts may belong to one Checkout.
- Order fulfillment: not started, processing, sending, delivered, canceled.

Checkout is the aggregate coordinating a locked Cart, immutable commercial snapshots, child Orders, provider attempts, stock reservations, and an application-level append-only diagnostic timeline. Existing Order and PaymentTransaction fields and endpoints remain compatible during rollout.

## Checkout Identity and Idempotency

The server generates immutable `Checkout.public_id`. The client supplies only an opaque UUID identifying the checkout action. The backend also calculates a SHA-256 fingerprint from canonical, server-owned cart IDs, quantities, options, and captured prices.

- Same user/client UUID and same fingerprint reuses the Checkout.
- Reusing a UUID with different content is a conflict.
- One Cart may have many historical Checkouts but only one active Checkout.

`Checkout.cart` is intentionally a nullable foreign key rather than a one-to-one relation. A one-to-one relation would prevent terminal historical Checkouts from remaining associated with the same Cart. A PostgreSQL partial unique constraint enforces at most one active Checkout per Cart instead.

## Cart Locking

An active Checkout locks its Cart with a reason: checkout in progress, payment in progress, manual review, or admin intervention. CartItems remain available for recovery until verified payment consumes them atomically. Canceling or expiring a draft releases reservations and unlocks the Cart. Legacy cart endpoints do not enforce this lock in Phase A.

## Expiration

The initial draft TTL is 30 minutes with a maximum lifetime of two hours. Valid checkout updates may extend the normal TTL but not the maximum. A cleanup command expires eligible drafts, releases dormant reservations, unlocks the Cart, and appends an event. A late proven payment for an expired Checkout requires manual review.

## Immutable Commercial Snapshots

New Checkouts capture product identity, names, unit prices, quantities, line totals, selected options, address, delivery method, delivery cost, and schedule data. Snapshot models reject normal updates after creation. Shipping-cost computation is not activated in Phase A.

## Backend-Owned Callback Direction

The target design verifies payment inside the backend callback, independently of browser state or authentication. Verification and finalization are idempotent and use row locks. Provider-paid transactions that cannot finalize locally enter manual review while preserving payment evidence.

Phase A does not implement or enable this callback path. Existing verification behavior remains unchanged until Phase B.

## Manual Review

Checkout and PaymentTransaction support structured reason codes for amount, stock, delivery, discount, provider-state, finalization, late-payment, and unknown conflicts. Manual-review records retain provider evidence and remain visible to customers and administrators. Resolution workflows and refunds are not implemented in Phase A.

## Commerce Events

CommerceEvent is a lightweight application-level append-only diagnostic timeline. Application services append events, and Django Admin cannot edit or delete them. It is not the authoritative current state and is not a full event store. Direct database access or unrestricted ORM code can still mutate records because Phase A intentionally adds no database trigger. Secrets, authorization data, card data, credentials, OTPs, and raw provider payloads are forbidden.

## Rollout

1. Phase A: additive models, statuses, events, commands, helpers, admin inspection, and tests.
2. Phase B: backend-owned callback, idempotent draft/payment creation, locking enforcement, and sandbox failure tests.
3. Phase C: active checkout recovery frontend and customer/admin projections.
4. Phase D: server-owned shipping prices and complete invoice-grade snapshots.
5. Phase E: real-gateway readiness review.

Feature flags are planned for activation of later phases; Phase A does not introduce or enable a commerce V2 feature flag.

## Consequences and Trade-offs

- Additional tables and state reconciliation increase operational complexity.
- Retaining CartItems while locked requires all future mutation paths to enforce ownership.
- Stock reservations reduce overselling but require reliable expiry.
- Provider verification outside long database transactions requires a verification claim and reconciliation process.
- Additive nullable compatibility fields simplify rollout but require temporary dual-model auditing.

## Real Gateway No-Go Criteria

A real gateway remains prohibited until backend callback verification, idempotency, checkout recovery, manual review, immutable totals, stock finalization, server-owned shipping cost, reconciliation, monitoring, and concurrency tests are complete.
