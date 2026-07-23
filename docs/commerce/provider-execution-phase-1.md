# Provider Execution Phase 1 — Verified Funds Recognition

Status: implemented for review, dormant, not deployed.

## Authority and boundary

`apply_verified_funds` is the only Phase 1 command that converts immutable provider success evidence into canonical financial truth. It accepts an internal Verification identity, command identity, expected Payment version, correlation/causation identity, and a controlled system actor. It does not accept an amount, merchant account, Payment target, Journal account, provider payload, or a generic “mark paid” flag.

The command performs no provider I/O. Callback and browser-return hints are not eligible evidence. The Verification must record final `CONFIRMED_SUCCESS`, a paid financial effect, successful server transport, exact provider/account/reference/operation/money ownership, and either `SERVER_TO_SERVER` or `AUTHENTICATED_SETTLEMENT` evidence basis. Existing C2B1 records with no explicit evidence basis remain blocked and require fresh verification.

## Immutable application identity

Verification remains append-only. It is not rewritten to claim financial application. `FinancialAllocation` is the immutable application record and is the audit bridge among Payment, Attempt, Financial Core PaymentTransaction, Verification, merchant-account version, canonical IRR amount, provider reference, receipt policy, JournalEntry, command identity, and correlation/causation identity.

`Verification.projected_application_state` reports `FINANCIALLY_APPLIED` only when the immutable one-to-one allocation exists. This preserves C2B1 evidence while making application state derivable and non-revertible. Unique constraints prevent a Verification, Transaction, provider reference within an account version, JournalEntry, or application command from allocating twice.

## Atomic recognition transaction

The transaction locks in this order:

1. Order
2. Payment
3. PaymentAttempt
4. PaymentTransaction
5. Verification/application source
6. receipt-accounting policy
7. FinancialAccounts in stable primary-key order
8. Journal source/entry
9. ReviewCase rows in stable primary-key order
10. events, work, outbox, and idempotency projection

Within one commit it creates the allocation and balanced receipt Journal, moves Transaction and Attempt to `SUCCEEDED`, sets `Payment.confirmed_amount`, moves a fully funded Payment to `PAID_PENDING_FINALIZATION`, appends events, creates one dormant commercial-finalization work item, records/raises the paid-pending-finalization review, and completes idempotency evidence. Any failure rolls all financial projections back. The pre-existing successful Verification remains append-only and continues to block recollection; a separate failure transaction creates or escalates a reason-specific ReviewCase when possible.

## Confirmed amount and tender policy

Payment and allocation currency are IRR. Application uses the canonical amount persisted on the exact-match Verification; it performs no IRT or provider-unit conversion. Before applying, the existing confirmed amount must equal the sum of immutable allocations. The new sum cannot exceed the obligation.

General split tender is disabled. A provider success smaller than the remaining obligation is not allocated automatically; it remains blocking evidence and enters review. Overpayment is never capped or posted as a normal receipt and enters critical review.

## Receipt accounting

Every allocation owns exactly one `provider_receipt` JournalEntry with exactly two IRR postings under the selected immutable policy version:

- debit the active provider-clearing asset account;
- credit the active customer-unapplied-funds liability account.

Provider receipt does not recognize revenue, tax, shipping, seller funds, fees, settlement, refunds, or payouts. Corrections require future compensating entries. The caller cannot choose accounts.

`ReceiptAccountingPolicyVersion` is scoped to a merchant-account version and records the two account identities and policy version. At most one policy is active for new applications. FinancialAllocation retains the exact historical policy. Migrations create no accounts or policies.

## `PAID_PENDING_FINALIZATION`

This status means provider funds and the receipt liability have been recognized exactly, while commerce is still pending. It forbids recollection and survives later commercial-finalization failure. It does not mean the Order is accepted, reservations are consumed, inventory is decremented, fulfillment exists, or revenue is recognized. Phase 1 cannot transition Payment to `PAID`.

## Work and review

`CommercialFinalizationWorkItem` is a separate durable, deduplicated, dormant work type keyed by Payment and finalizer version. No worker, scheduler, signal, cron, route, or Admin command executes it.

Reason-specific ReviewCases cover application failure, Journal failure, missing policy, paid-pending-finalization, duplicate allocation, overpayment, and invariant failure. There is no generic mark-paid resolution. Future manual financial recognition remains maker/checker work outside this phase.

## PostgreSQL enforcement

Additive guards enforce allocation immutability and ownership, authenticated final success eligibility, one source identity, exact Journal source/money/policy linkage, confirmed amount equal to allocations, exact funding for `PAID_PENDING_FINALIZATION`, successful Attempt/Transaction evidence, no downgrade, no reopen/cancel after allocation, and no transition to `PAID`. Existing C1–C2B1 append-only and balanced-Journal guards remain active.

Migrations do not create allocations, Journal entries, confirmed balances, policies, accounts, finalization work, reviews, providers, or merchant accounts. They do not interpret legacy transactions or consume commercial resources.

## Dormancy and security

The command has no URL, serializer, view, Admin action, callback wiring, signal, scheduled task, or ordinary-traffic import. It uses no raw provider payload and records no credential or full PII in idempotency, event, outbox, work, or error evidence. Only controlled `SYSTEM` or `RECONCILIATION` actors may invoke it. No real accounting or provider configuration is committed.

## Deferred Provider Execution Phase 2

- canonical commercial finalizer;
- Standard StockReservation consumption;
- DigitalInventoryReservation consumption;
- authoritative Product quantity decrement;
- authoritative InventoryPool decrement;
- Order acceptance/finalization;
- fulfillment-obligation creation;
- commercial Journal reclassification for revenue, tax, shipping, and deferred revenue;
- Payment transition from `PAID_PENDING_FINALIZATION` to `PAID`;
- paid-finalization retry execution and reconciliation.
