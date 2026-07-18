# Digital Checkout Preparation API

API-03 prepares an authenticated customer's homogeneous Digital Cart as an immutable, temporary,
reservation-backed Checkout. It stops before Payment, Order placement, Commercial Finalization,
fulfillment, and Entitlement.

## Routes

- `POST /api/digital-products/customer/checkout/prepare/` accepts only `checkout_uuid`.
- `GET /api/digital-products/customer/checkout/active/` reads the active owned Digital Checkout.
- `GET /api/digital-products/customer/checkout/<checkout_id>/` reads one owned Checkout by public UUID.
- `POST /api/digital-products/customer/checkout/<checkout_id>/cancel/` accepts an empty body and
  cancels only a pre-payment Checkout.

All routes require an authenticated, active, phone-verified Customer. Ownership-safe lookup never
accepts a customer or Cart identifier from the client.

## Commercial authority

Preparation locks and revalidates the Cart, CartItems, Product graph, Digital selections,
DigitalOffers, DeliveredVersions, and InventoryPools. A Digital Cart must contain only
`DIGITAL_PRODUCTS` lines. Standard and mixed Carts are rejected and are never split locally.

`DigitalOffer.price` must still equal the frozen CartItem price. No silent repricing occurs.
Product legacy price, Product quantity, and attachments are not Digital truth.

The atomic graph contains one shared Checkout, one CheckoutLine and immutable
DigitalCheckoutLineSnapshot per CartItem, one ACTIVE DigitalInventoryReservation per line, and the
Cart lock. A failure commits none of that graph. InventoryPool sellable quantity is not decremented.

`commercial_revision` is persisted in each immutable CheckoutLine snapshot. It identifies the
commercial snapshot contract and is deliberately separate from the internal mutable Checkout
aggregate `version` used by later concurrency boundaries.

## Lease and reservation policy

Checkout owns a finite expiration lease. The duration and maximum lifetime are supplied by
`COMMERCE_CHECKOUT_TTL_SECONDS` and `COMMERCE_CHECKOUT_MAXIMUM_LIFETIME_SECONDS`. Values must be
positive and the lease cannot exceed the configured maximum lifetime. Reads never renew a lease.

Effective Digital holds include ACTIVE, PAYMENT_HOLD, and HELD_FOR_REVIEW reservations. API-03
creates only ACTIVE reservations. Backend-owned expiration changes eligible reservations to
EXPIRED; pre-payment cancellation changes them to RELEASED. Both release the Cart lock without
changing InventoryPool or Product quantity.

## Idempotency and readiness

`checkout_uuid` is customer-scoped idempotency identity. An identical retry returns the same
coherent Checkout. A different UUID cannot acquire a Cart already owned by an active Checkout.
Concurrent preparation uses deterministic database row locks and Pool-level availability checks.

`is_commercially_ready` and `is_payment_ready` are derived by the backend from the immutable graph,
Checkout status, lease, Cart lock, and ACTIVE reservations. They do not create Payment and do not
establish provider or financial truth.

CommerceEvent rows are append-only audit evidence for reconciliation, investigation, diagnostics,
and history. They are not a state machine or commercial authority; Checkout and reservation state
remain authoritative.

## Dormant boundaries

API-03 creates no Payment, PaymentAttempt, legacy PaymentTransaction, Order, OrderItem,
CommercialFinalization, DigitalFulfillmentObligation, DigitalFulfillmentItem, or Entitlement. It
performs no provider I/O and activates no callback, worker, task, signal, scheduler, Admin, or
fulfillment route. Standard Checkout endpoints and semantics remain unchanged.
