# Zarinpal initiation reliability

The payment-request operation is non-idempotent at the provider boundary. The
application therefore makes one provider call for one claimed transaction and
does not automatically retry when request delivery is uncertain.

## Safe transport evidence

The `cheatgame.financial_core.provider_transport` logger emits one sanitized
JSON event per response or transport failure. Events contain only provider and
operation names, Financial Core correlation/transaction IDs, target host,
durations, HTTP status/response shape when available, and a bounded exception
class/phase classification. Payloads, merchant credentials, customer data,
headers, Authorities, and provider response values are never logged.

Failure events distinguish DNS, connect, TLS, connect-timeout, read-timeout,
connection-reset, proxy, and unknown-after-send boundaries. The same safe
classification is persisted on the append-only provider request result while
the public financial state remains `outcome_unknown`.

## Retry policy

- Never retry a read timeout, connection reset, or unknown-after-send failure.
- Never retry merely because the browser did not reach StartPay.
- A DNS/connect/TLS failure may be considered pre-send evidence, but V1 still
  requires an operator-controlled reconciliation rather than an automatic
  retry.
- Only authoritative provider evidence that no Authority exists may close the
  review and permit a new Checkout/payment action.

## Operational thresholds

Alert and freeze new payment initiation when either condition is true:

- any open `provider_state_unclear` ReviewCase exists; or
- two transport failures occur within 15 minutes.

For a rolling window with at least 20 attempts, also alert when provider-request
success falls below 98% or p95 response duration exceeds 5 seconds. Investigate
the structured transport phase before changing timeouts. The current bounded
client policy is a 3-second connect timeout and 10-second read timeout; it has
no automatic retry and no unlimited wait.

## Safe acceptance

Use repeated non-mutating DNS/TCP/TLS/HEAD probes from both Staging and
Production, adapter fault-injection tests for every taxonomy branch, and the
minimum owner-authorized real initiation. If a real Authority is created, stop
at StartPay and reconcile that unpaid Authority through the existing provider
inquiry/cancellation path.
