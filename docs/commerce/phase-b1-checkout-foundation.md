# Commerce Phase B1: Recoverable Checkout Foundation

## Status

Phase B1 is implemented behind `COMMERCE_CHECKOUT_V2_ENABLED`, which defaults to `False`.
Legacy cart, order, and payment endpoints remain available and unchanged while the flag is disabled.
The frontend is not switched to these endpoints in B1.

## API

- `POST /api/shop/checkouts/` creates or reuses a draft from the authenticated user's server-owned cart.
- `GET /api/shop/checkout/active/` returns the recoverable active checkout.
- `GET /api/shop/checkouts/{public_id}/` returns an owned checkout.
- `PATCH /api/shop/checkouts/{public_id}/address/` snapshots an owned address.
- `PATCH /api/shop/checkouts/{public_id}/shipping/` snapshots a shop-side delivery method.
- `PATCH /api/shop/checkouts/{public_id}/schedule/` snapshots an available order schedule without consuming capacity.
- `POST /api/shop/checkouts/{public_id}/cancel/` idempotently cancels a safe unpaid checkout and unlocks its cart.

Stable errors include `CHECKOUT_V2_DISABLED`, `CART_EMPTY`, `CART_INVALID`,
`CART_LOCKED`, `IDEMPOTENCY_CONFLICT`, `CHECKOUT_NOT_FOUND`,
`CHECKOUT_NOT_EDITABLE`, and `CHECKOUT_NOT_CANCELABLE`.

## Idempotency And Recovery

The client supplies an opaque UUID. The server creates the public checkout UUID and a SHA-256
fingerprint from canonical, server-owned product, quantity, option, and price data. Reusing the
same client UUID with the same fingerprint returns the same checkout. Reusing it with different
content fails. A different UUID against a locked cart returns safe resume data.

The cart remains intact and is locked with `CHECKOUT_IN_PROGRESS`. Draft lines and selected
attachments are immutable snapshots. Address, shipping method, and schedule selections are
persisted in the shipping snapshot and returned by the active-checkout API.

## Expiration And Cancellation

`expire_checkouts` remains report-only by default and requires `--apply`. Eligible unpaid drafts
are expired under row locks, reservations are released, cart items are preserved, and the cart is
unlocked. Customer cancellation follows the same preservation rule and refuses paid, uncertain,
or manual-review states.

## Shipping Limitation

`CheckoutShippingSnapshot.is_pricing_finalized` is `False` in B1. Delivery cost remains zero as a
placeholder and must not be presented as free shipping. Payment creation must remain blocked until
the server-owned shipping-price phase is complete. Checkout responses therefore expose
`payment_eligible=false` and `payment_ineligible_reason=SHIPPING_PRICING_NOT_FINALIZED`.

CheckoutLine and CheckoutLineAttachment reject normal model `save()` updates and all snapshot
models are read-only in Django Admin. Address/shipping/schedule selection intentionally updates the
shipping snapshot only while its Checkout is editable. These are application-level guarantees;
unrestricted ORM `QuerySet.update()` or direct database access can still bypass them because B1
does not add database immutability triggers.

## Explicit Non-Goals

B1 does not create payment transactions, verify callbacks, reserve stock or delivery capacity,
create Orders, activate a gateway, alter PaymentSuccess authority, or enable Checkout V2 for normal
customers. The frontend has not switched to V2, staging activation requires separate approval, and
real gateway integration and production rollout remain NO-GO.

## Deferred Prerequisites

- Active B1 paths acquire Cart before Checkout where both rows are locked. The dormant Phase A
  manual-review helper still acquires Checkout before Cart and must be normalized before any Phase
  B2 callback or payment flow activates it.
- Replace concrete local database examples with unmistakable placeholders in a separate
  repository-hygiene change. This is intentionally outside the B1 commit's runtime behavior.

## Test Commands

Run the focused suite on PostgreSQL:

```bash
python manage.py test cheatgame.shop.test_checkout_b1
python manage.py test cheatgame.shop
python manage.py check
python manage.py makemigrations --check --dry-run
```
