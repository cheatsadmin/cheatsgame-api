# Terminal late-payment adjudication

This contract handles authoritative success evidence received after a PaymentAttempt or
PaymentTransaction already reached a contradictory terminal state.

It does not handle ordinary success received after only a Checkout or reservation timestamp
expired. That nominal-expiry case remains part of the PAYMENT_HOLD lifecycle.

## Immutable evidence

The original terminal PaymentAttempt, PaymentTransaction, Verification, provider evidence,
amount, currency, merchant account, and provider reference remain unchanged.

Verification creates or reuses one `LATE_PAYMENT` ReviewCase and one
`LatePaymentAdjudication` for the exact success Verification. Funds recognition remains
blocked until a distinct maker and checker accept that exact evidence.

## Roles and decisions

Only active Admin or Manager users may act.

1. A maker proposes `accept` or `reject` and records a rationale.
2. A different checker approves or rejects the proposal.
3. Accepted adjudication creates one immutable `ExceptionalRecognitionAuthorization`.
4. Rejected adjudication creates no recognition or inventory authority.

Every action has a UUID idempotency identity and an append-only ReviewAction/FinancialEvent.
Direct model administration is not an operational interface.

## Exact authorization

Authorization is bound to:

- adjudication and Verification;
- original Order, Payment, PaymentAttempt, and PaymentTransaction;
- merchant-account version;
- provider reference;
- canonical amount and IRR currency;
- evidence hash;
- the Payment version admitted by the checker.

It cannot authorize another Payment or different evidence. PostgreSQL financial guards accept
terminal-row allocations only when this exact authorization is applied.

## Recognition and inventory order

The existing accounting model recognizes authenticated receipt funds first:

- provider clearing is debited;
- customer unapplied funds liability is credited;
- Payment becomes `PAID_PENDING_FINALIZATION`;
- the original Order and Payment are reused.

This preserves accepted-funds liability even if inventory cannot be recovered.

Before recognition, an `INVENTORY_CONFLICT` ReviewCase is opened as a finalization barrier.
After recognition:

1. An existing `PAYMENT_HOLD` is renewed without decrementing stock.
2. A released original reservation may receive one exact-authorized replacement reservation
   against the same Checkout line, Order, Payment, and InventoryPool.
3. If equivalent stock is unavailable, no reservation or commercial completion is fabricated.
   The inventory ReviewCase remains open and blocks finalization.

The recovery hold duration uses `FINANCIAL_LATE_PAYMENT_RECOVERY_HOLD_SECONDS`, defaulting to
1800 seconds. This duration is only the adjudicated recovery claim; general PAYMENT_HOLD
timing remains outside this contract.

Inventory is decremented only by the existing commercial finalizer. Its idempotency and unique
obligation/allocation/journal/work constraints remain authoritative.

## Operational services

- `list_open_terminal_late_payment_reviews`
- `inspect_terminal_late_payment_review`
- `propose_terminal_late_payment_decision`
- `check_terminal_late_payment_decision`
- `cancel_terminal_late_payment_adjudication`
- `apply_approved_terminal_late_payment`
- `recover_terminal_late_payment_inventory`

There is no `force_paid`, terminal-row rewrite, provider adapter, worker activation, refund,
outbox dispatch, or fulfillment execution in this contract.
