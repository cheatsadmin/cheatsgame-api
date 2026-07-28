# Digital payment retry policy

One `Payment` represents one commercial payment attempt.

An authoritative definitive unpaid result terminates that commercial graph:

- the terminal `PaymentAttempt`, `PaymentTransaction`, and provider evidence
  remain immutable;
- the `Payment` remains the immutable collection graph for that evidence and
  cannot admit another attempt;
- the `Order` becomes failed and its `Checkout` becomes canceled;
- the current authoritative `PAYMENT_HOLD` reservations become `RELEASED`;
- the Cart is unlocked only after the complete definitive-unpaid projection is
  committed.

`can_retry=true` means that the customer may return to the Cart and prepare a
new Checkout. The new Checkout re-resolves current Offer price and availability
and creates a new Order, Payment, PaymentAttempt, and reservation. It never
reuses the failed graph.

Two original-graph continuation paths remain:

1. Verified success after nominal Checkout/reservation expiry when no
   contradictory terminal financial evidence exists.
2. Terminal contradictory late success admitted through maker/checker approval
   and an exact `ExceptionalRecognitionAuthorization`.

The PostgreSQL Cart projection guard permits the definitive-unpaid unlock only
when the Checkout is canceled, the Order is failed, the Payment has no
recognized funds, terminal unpaid Attempt/Transaction evidence exists, and no
current Digital inventory claim remains. All other post-placement Cart unlocks
still require the existing commercial-finalization guard.
