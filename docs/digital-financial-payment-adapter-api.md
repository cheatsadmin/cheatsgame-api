# Digital Financial Payment Adapter API (API-04)

API-04 is the only customer boundary that converts a prepared, homogeneous
Digital Checkout into the frozen Financial Core placement and provider-request
graph. It stops after a normalized provider request result and never treats a
browser response as payment truth.

## Routes

- `POST /api/digital-products/customer/checkout/{checkout_id}/payment/request/`
- `GET /api/digital-products/customer/checkout/{checkout_id}/payment/`

Both routes require an authenticated, active, phone-verified Customer who owns
the Checkout. The request route also requires an `Idempotency-Key` UUID and the
strict body `{ "provider": "..." }`.
Amount, currency, customer, Order, Product, Offer, and inventory identity are
never accepted from the client.

## Placement and money authority

The adapter invokes `place_order_and_create_payment_obligation` with the frozen
Checkout identity, owner, row version, and configured source money unit. That
Financial Core service alone creates the Order, OrderItems, Financial Payment,
and obligation source, advances reservations to `PAYMENT_HOLD`, and changes the
Cart lock to payment-in-progress. The immutable Checkout snapshot and its
`commercial_revision` remain the commercial input. Financial Core performs the
canonical IRT-to-IRR bridge; API-04 performs no conversion and sends no amount
from the browser.

## Attempts and provider requests

One Payment may have sequential attempts, but the Financial Core partial unique
constraint permits at most one blocking attempt. API-04 creates a SALE request
through the existing attempt, transaction, claim, and result services. A
`NO_EFFECT_RETRYABLE` result may re-use that same transaction with a new root
request key; a definitive failure permits a new attempt. Unknown, review, live,
successful, and customer-action states block a competing attempt.

The provider receives only an `ImmutableProviderRequestEnvelope`: frozen
Financial Core amount/currency, provider representation, immutable merchant and
capability versions, credential *reference*, correlation identities, request
fingerprint, and one claim token. No Product, Pool, reservation, customer, card,
or raw credential data leaves this boundary. The adapter call is made outside
all database transactions. Provider definitions, merchant accounts, capability
versions, adapter registry membership, and redirect hosts must all be explicitly
enabled/allowlisted.

## Idempotency and handoff

The customer key owns a canonical request fingerprint containing the immutable
Checkout/customer identity, original placement Checkout version, commercial
revision, provider, merchant-account version and capability version.
Deterministic stage keys are derived from that complete commercial root as well
as the client UUID, protecting placement, attempt, transaction, claim, and result
without cross-Checkout collisions. An
identical completed replay returns the current authoritative state without
calling the provider again. A conflicting key payload fails closed. A concurrent
request that observes a claimed but incomplete provider operation returns an
in-progress response and never duplicates I/O.

The root command is persisted before placement. If the process stops after
placement, attempt creation, or transaction creation, the same request resumes
the next deterministic stage. Once a provider-request claim exists, retries do
not issue provider I/O again without stronger evidence; an unresolved claim is
reported as an in-progress, do-not-pay-again state for later controlled recovery.

Customer-action URLs are accepted only for the normalized
`CUSTOMER_ACTION_REQUIRED` outcome, must be HTTPS, and must match the configured
provider-specific host allowlist. The sanitized URL is persisted atomically with
the provider result in root idempotency evidence; it is returned only while the
current transaction remains pending customer action. It is not a payment result.

## Status and privacy

The GET route is read-only and uses Financial Core `Payment`, `PaymentAttempt`,
and `PaymentTransaction` state. It never uses legacy `shop.PaymentTransaction`
as authority. Responses expose public aggregate identifiers, safe state, amount
due/currency, retry/review guidance, and an eligible customer-action URL. They do
not expose merchant credentials, claim tokens, request fingerprints, provider
evidence, Journals, allocations, reservations, Pool data, or review diagnostics.

## Dormant boundaries

API-04 does not ingest callbacks, verify provider outcomes, recognize funds,
create allocations or Journals, invoke Commercial Finalization, consume
inventory, provision fulfillment, or activate Entitlements. The production
adapter registry remains empty in this checkpoint, so a provider cannot execute
until a later, explicit provider-activation phase supplies both a registered
adapter and enabled versioned configuration.
