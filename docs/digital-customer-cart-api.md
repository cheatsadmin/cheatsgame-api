# Customer Digital Cart API

API-02 is an authenticated adapter over the existing Cart, CartItem, DigitalCartSelection, and Digital cart command services. It stops at Cart and does not prepare Checkout, reserve inventory, request payment, create an Order, or provision fulfillment.

## Routes and ownership

- `POST /api/digital-products/customer/cart/items/`
- `DELETE /api/digital-products/customer/cart/items/<cart_item_id>/`
- `PATCH /api/digital-products/customer/cart/items/<cart_item_id>/fulfillment-method/`
- Existing owned read: `GET /api/shop/cart-item-list/`

Only an authenticated, active, phone-verified Customer may use the Digital mutation routes. The server resolves the Customer's Cart; payloads never accept customer or Cart identity. CartItem lookup is ownership-scoped, so another customer's identifier returns the same not-found result as an absent identifier.

## Selection and duplicate policy

`offer_id` is an untrusted public selection reference, not commercial authority. Add accepts only `offer_id` and `fulfillment_method`. The adapter resolves the Offer through public eligibility rules and the domain command revalidates the locked Cart, Offer, DeliveredVersion, Pool, method, and effective availability.

The locked command also revalidates that the Product is still published. A catalog lookup is therefore not final authority when publication changes concurrently with the mutation.

The current deterministic domain policy is:

- the same Offer is a conflict, whether the submitted method is identical or different;
- a different Offer remains a distinct quantity-one CartItem, including another console, capacity, version, or an Offer sharing the same Pool;
- Digital CartItems are never merged or incremented;
- Standard and Digital items cannot be added into the same Cart through controlled mutation services.

Cart acquisition is shared by Standard and Digital commerce. It serializes first creation on the customer identity, so concurrent first requests resolve one authoritative Cart. Concurrent exact Digital selection yields one success and one `digital_cart_selection_conflict`; it never increments quantity or leaves a partial selection. A concurrent Standard/Digital first add similarly leaves one authority-coherent item and returns a controlled mixed-authority conflict for the losing request.

Capacity 1 accepts only `in_store`. Capacities 2 and 3 accept the currently approved `in_store` and `remote` methods. Method change modifies only DigitalCartSelection; it never changes Product, Offer, price, quantity, console, capacity, or delivered version.

## Price, inventory, and lifecycle boundary

DigitalOffer is price authority at selection time. The controlled domain service copies its price into the quantity-one CartItem. Cart responses present that captured CartItem price as `unit_price` and `line_total`; they do not recalculate from Product price or silently refresh a changed Offer price.

Public Cart money remains `IRT`. API-02 performs no IRT-to-IRR conversion. Financial Core placement remains the canonical conversion boundary.

InventoryPool minus effective holds is used only for safe `AVAILABLE`/`SOLD_OUT` projection and add validation. API-02 creates no DigitalInventoryReservation and never decrements InventoryPool or Product quantity.

## Authority-aware Cart response

The existing Cart list preserves all Standard fields and adds:

- `commerce_authority`: `STANDARD_COMMERCE` or `DIGITAL_PRODUCTS`;
- nullable `digital_selection`.

Digital selection includes customer-facing game, console, capacity, delivered version, compatibility and capacity disclosures, selected fulfillment method, captured line price, currency, and safe availability. Digital Product projections omit legacy Product price, discount, and quantity. Pool identity, exact stock, reservations, Admin readiness, payment, financial, fulfillment, and Entitlement data are never returned. A contradictory Digital Cart row fails closed with `digital_cart_integrity_conflict`; reading never repairs it.

## Stable conflicts

The adapter uses `{code, detail, fields?}`. Stable codes cover invalid input, permission denial, Offer not found/unavailable, method restrictions, duplicate selection, mixed authority, locked Cart, owned CartItem not found, Standard item misuse, and integrity conflict. Raw database, model, Pool, and ownership details are not returned.

Checkout preparation, reservations, Order placement, payment/provider operations, Admin catalog and fulfillment, operational provisioning, tasks, signals, workers, and schedulers remain unchanged and dormant from these routes.
