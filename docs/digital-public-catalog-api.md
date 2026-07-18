# Public Digital Catalog API

The public Digital catalog is a read-only adapter over the Digital Products domain. It does not replace or alter the Standard Product catalog.

## Authority and eligibility

A public game must be a published `GAME` Product with `DIGITAL_PRODUCTS` commerce authority and at least one coherent customer-visible Offer. A customer-visible Offer is `ACTIVE`, belongs to an active DeliveredVersion, uses an enabled InventoryPool, and has a supported console/version relationship. Draft, paused, hidden, archived, inactive-version, paused-Pool, and archived-Pool configurations are excluded.

DigitalOffer is the only public Digital price authority. `Product.price`, `Product.off_price`, attachment prices, and Product quantity are never used as Digital price or availability fallbacks. Public prices use the existing Storefront source-money contract, `IRT`; conversion into Financial Core's canonical `IRR` occurs only at the frozen financial placement boundary.

InventoryPool is the availability authority. Public availability is calculated as sellable quantity less reservations in `ACTIVE`, `PAYMENT_HOLD`, and `HELD_FOR_REVIEW`. The API returns only `AVAILABLE` or `SOLD_OUT`; it never returns Pool identity, exact quantity, held quantity, reservations, or stock adjustments. A sold-out active Offer remains visible as a disabled customer option.

## Routes

- `GET /api/digital-products/catalog/games/`
- `GET /api/digital-products/catalog/games/<slug>/`

List filters are `search`, `console`, `capacity`, `ordering`, `limit`, and `offset`. Ordering accepts `newest`, `title`, and `minimum_price`. Invalid or unknown parameters return the stable error shape `{code, detail, fields?}`.

The list returns limit/offset pagination and customer-safe game summaries. Detail returns the same game summary plus public description media and separate Offer rows for every console, capacity, and DeliveredVersion selection. Capacity 1 exposes only in-store fulfillment; capacities 2 and 3 expose the backend-approved in-store and remote methods.

## Privacy and coexistence

Responses exclude Product quantity and legacy Product price, InventoryPool internals, reservation records, Admin sale/readiness state, adjustments, attachments, audit records, payment/financial data, source accounts, credentials, and internal notes. Existing Standard Product routes and serializers are unchanged.

Only these two public GET routes are wired in the Digital namespace. Customer Cart, Checkout, payment, Admin, fulfillment, provider callback, worker, signal, scheduler, and task routes remain dormant.
