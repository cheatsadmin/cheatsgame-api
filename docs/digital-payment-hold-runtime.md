# Digital PAYMENT_HOLD runtime policy

This policy completes the non-contradictory Digital payment-hold lifecycle. It
does not change terminal Financial Core evidence, terminal late-payment
adjudication, provider integration, or fulfillment.

## State authority

```text
Checkout draft
  ACTIVE
    |
    | Order and Payment placement
    v
  PAYMENT_HOLD
    |-- provider/callback/verification pending --> renew PAYMENT_HOLD
    |-- prolonged uncertainty -----------------> HELD_FOR_REVIEW
    |-- non-contradictory verified success ----> renew PAYMENT_HOLD
    |-- definitive unpaid ----------------------> RELEASED + canceled Checkout + open Cart
    |-- paid finalization success --------------> CONSUMED
    `-- paid finalization terminal failure -----> HELD_FOR_REVIEW
```

`ACTIVE`, `PAYMENT_HOLD`, and `HELD_FOR_REVIEW` remain the only current
authoritative reservation states. `RELEASED`, `EXPIRED`, and `CONSUMED` are
immutable history. One CheckoutLine still has at most one current reservation.

Pending and uncertain graphs keep the Cart locked. `HELD_FOR_REVIEW` is entered
for prolonged or classified uncertainty and for terminal commercial
finalization failure after funds recognition. It exits only through
authoritative financial evidence followed by the ordinary recognition path, a
controlled ReviewCase resolution, or the already-approved terminal
late-payment adjudication boundary.

## Abandonment

Browser closure, inactivity, and a missing callback are not abandonment.
Abandonment requires a terminal definitive-unpaid Attempt/Transaction, no
recognized funds, no pending verification work, no unresolved ReviewCase, and
the configured deadline. The bounded cleanup calls the same definitive-unpaid
domain service; release and Cart unlock remain atomic and idempotent.

## Nominal expiry and finalization

A nonterminal verified success renews the original current hold before funds
recognition. It does not require maker/checker authority. Terminal
contradictory success continues exclusively through
`ExceptionalRecognitionAuthorization`.

Retryable finalization failures retain `PAYMENT_HOLD` and schedule exponential
backoff on the existing work item. A terminal finalization failure cancels that
work item, opens or reuses `COMMERCIAL_FINALIZATION_FAILED`, retains the
strongest valid inventory claim as `HELD_FOR_REVIEW`, keeps the Cart locked,
and preserves recognized-funds liability.

## Configurable durations

| Setting | Default | Effect |
| --- | ---: | --- |
| `DIGITAL_PAYMENT_PROVIDER_PENDING_HOLD_SECONDS` | 1800 | Renew provider/callback pending holds |
| `DIGITAL_PAYMENT_VERIFICATION_PENDING_HOLD_SECONDS` | 1800 | Renew verification-pending holds |
| `DIGITAL_PAYMENT_REVIEW_HOLD_SECONDS` | 86400 | Extend review-owned claims; never releases them |
| `DIGITAL_PAYMENT_ABANDONMENT_SECONDS` | 86400 | Earliest terminal-unpaid abandonment cleanup |
| `DIGITAL_PAYMENT_NOMINAL_EXPIRY_RENEWAL_SECONDS` | 1800 | Renew non-contradictory paid/finalization claims |
| `DIGITAL_PAYMENT_FINALIZATION_RETRY_SECONDS` | 300 | Initial retryable-finalization backoff |
| `DIGITAL_PAYMENT_FINALIZATION_RETRY_MAX_SECONDS` | 3600 | Maximum finalization backoff |

Production values require release-operations approval. No duration treats an
unknown outcome as unpaid.

## Bounded operations

`manage_payment_holds` is an explicit operator command, not a scheduler or
worker. It supports inspection by default and requires `--apply` for mutation:

```text
inspect-pending
inspect-review
inspect-abandonment
process-abandonment --apply
escalate-uncertain --apply
inspect-finalization
retry-finalization --apply
```

Every mutation delegates to domain services, uses bounded `--limit` batches,
and does not expose force-paid, force-release, or direct status mutation.
