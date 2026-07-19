# Funds Recognition boundary

API-07 is an internal, dormant Financial Core command boundary. It converts one
eligible immutable Verification into recognized financial reality. It exposes no
HTTP endpoint and performs no provider I/O.

## Authority and eligibility

`recognize_verified_funds` accepts only a Verification identity, an idempotency
UUID, the expected Payment version, correlation and causation identities, and a
controlled actor. The only allowed actors are `SYSTEM` and explicitly identified
`RECONCILIATION`. Amount, currency, provider identity, accounting accounts,
Payment status, Order identity, and customer identity are never command inputs.

The command locks and revalidates the complete immutable Payment graph. Funds are
eligible only when the current Verification interpretation is
`ELIGIBLE_FINAL_PAID` and the selected observation proves exact final paid effect
using `SERVER_TO_SERVER` or `AUTHENTICATED_SETTLEMENT` evidence. Provider,
account, capability, adapter contract, operation, references, amounts, units, and
canonical IRR currency must exactly match the PaymentTransaction. The external
provider reference must be allocated to that exact Verification and transaction.
Contradiction, mismatch, review, non-final, browser, callback-hint, or missing
evidence fails closed.

## Recognition graph and accounting

Recognition is one database transaction:

1. Resolve one immutable active ReceiptAccountingPolicyVersion belonging to the
   transaction's MerchantAccountVersion.
2. Post one balanced provider-receipt Journal in canonical IRR: debit the active
   provider-clearing asset and credit the active customer-unapplied-funds
   liability for the exact verified amount.
3. Create one immutable FinancialAllocation linking Payment, Attempt,
   PaymentTransaction, Verification, merchant-account version, policy version,
   Journal, provider reference, amount, currency, and command identities.
4. Transition the PaymentTransaction and PaymentAttempt to `SUCCEEDED`, reconcile
   `Payment.confirmed_amount` from immutable allocations, and transition the
   exactly funded Payment to `PAID_PENDING_FINALIZATION`.
5. Append financial events and create exactly one dormant
   CommercialFinalizationWorkItem plus its transactional outbox message.

The receipt entry recognizes an asset held at the provider and a liability to the
customer's unapplied funds. It does not post revenue, inventory, cost, tax, or
commercial acceptance. Partial funding, overpayment, currency conversion,
rounding, and IRT inputs are outside this checkpoint.

## Idempotency, locking, and recovery

The fingerprint freezes the command contract, Verification and Payment public
and internal identities, the original expected Payment version, and controlled
actor type. Completed coherent replay returns the authoritative allocation before
mutable-version rejection. A conflicting key fails closed; another key for an
already coherent allocation returns that allocation without duplicating any
financial or delivery evidence.

The transaction follows the global order: Order, Payment, all sibling Attempts,
all sibling Transactions, Verification evidence, accounting policy, Journal
accounts and Journal, ReviewCases, then idempotency/event/finalizer/outbox
identities. There is no external I/O. Any failure, including a missing finalizer
work item or outbox message, rolls back the Journal, allocation, projections,
events, and handoff together. PostgreSQL deferred guards validate the complete
recognized-payment handoff at commit.

Recognized allocations and receipt Journals are append-only. Later provider
disputes require a future refund, chargeback, reversal, or accounting-adjustment
boundary; API-07 never rewrites recognized history.

## Boundary and dormancy

Verification remains the source of verified evidence; Funds Recognition alone
creates FinancialAllocation, receipt accounting, and the recognized Payment
projection. Commercial Finalization consumes the dormant handoff later and must
revalidate the full graph. API-07 does not execute it.

No public/customer route, callback trigger, provider query, worker, task,
scheduler, signal, startup hook, or management command invokes recognition.
Commercial Finalizer execution, Order acceptance, reservation or inventory
consumption/release, fulfillment, Entitlement, refunds, chargebacks, reversals,
and provider registration remain dormant.
