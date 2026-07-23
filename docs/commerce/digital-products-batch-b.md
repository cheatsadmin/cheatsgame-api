# Digital Products Batch B: shared Cart and Checkout integration

## Scope and dormancy

Batch B adds authority-aware shared Cart and Checkout behavior and Digital domain services. It adds no customer Digital API or URL, payment request/callback, PaymentTransaction ownership change, Order finalization, fulfillment, entitlement, Admin frontend, or Storefront behavior. `COMMERCE_CHECKOUT_V2_ENABLED` remains false by default; the existing Standard Checkout V2 APIs therefore remain dormant. No migration creates a Digital Product, Offer, selection, snapshot, reservation, Checkout, or active record.

## Authority ownership and mixed-Cart policy

`CartItem.commerce_authority` and `CheckoutLine.commerce_authority` persist either `standard_commerce` or `digital_products`; existing rows receive the safe Standard default. Product, line, and Digital child records must agree. Authority is never inferred solely from the presence of a nullable child row.

One Cart may contain only one authority. Standard and Digital add services reject an attempted cross-authority addition with `MIXED_COMMERCE_AUTHORITY_NOT_SUPPORTED` before mutation. Standard Checkout rejects Digital-only and mixed Carts. Digital preparation rejects Standard-only and mixed Carts. There is no split or conversion.

## Digital Cart and commercial truth

`DigitalCartSelection` is one-to-one with a Digital CartItem and identifies an active, coherent DigitalOffer and fulfillment method. DigitalOffer price is the only Digital price authority. Product price, Product quantity, Standard attachments, and Standard option pricing are not used for Digital Checkout or inventory.

## Checkout preparation and snapshots

The domain-only preparation service locks the Cart, validates every selection, recalculates terms from backend-owned Offers, builds an authority-aware fingerprint, creates or safely reuses a shared Checkout, and creates Digital CheckoutLines, immutable `DigitalCheckoutLineSnapshot` rows, and reservations atomically. It then locks the Cart to the Checkout. A failure rolls back the Checkout, lines, snapshots, reservations, events, and Cart lock.

Snapshots preserve the source Offer, Pool, DeliveredVersion, Product identity, console/version/capacity/method disclosures, price, quantity, total, and authority. Later catalog changes do not rewrite snapshots. Immutability is enforced at the Django application boundary; no database trigger is claimed.

## Reservations and availability

`DigitalInventoryReservation` is one-to-one with an authoritative CheckoutLine and references its Checkout and Pool. Quantity is currently exactly one. Active and held-for-review reservations reduce availability:

`available = InventoryPool.sellable_quantity - active_or_held_DigitalInventoryReservation.quantity`

Reservation creation locks Pools in stable primary-key order and never changes Pool total stock. Cancellation changes active Digital reservations to `released`; expiry changes them to `expired`. Both restore availability solely by removing the active hold. Payment consumption is deferred to Batch C.

## Cart lock, cancellation, and expiry

An actual active Checkout lock protects Cart mutations regardless of the Standard Checkout feature flag. Owners receive only public Checkout UUID, safe status, and a safe resume route on lock conflicts. Internal Checkout IDs, fingerprints, Pool IDs, reservation IDs, exact inventory, and event internals are not projected.

Cancellation and expiry inspect persisted CheckoutLine authority. Standard Checkouts retain B1 StockReservation behavior. Digital Checkouts release only Digital reservations without changing Pool total. Mixed or incoherent Checkouts are left untouched for review. Cancellation is idempotent; `expire_checkouts` remains report-only unless `--apply` is supplied and continues skipping protected payment states.

The operative lock order is Cart, authority/catalog rows, Checkout, CheckoutLines/snapshots, InventoryPools in ascending ID order, DigitalInventoryReservations, then events. Checkout-address/shipping/schedule mutation and cancellation resolve and lock the owning Cart before the Checkout. PaymentTransaction ordering remains deferred.

## Migration lineage

The deployed `shop.0017_checkoutshippingsnapshot_is_pricing_finalized` remains unchanged. The original, never-deployed Digital Shop migrations were rebased semantically onto that deployed head:

- original `shop.0017_cartitem_commerce_authority` → integrated `shop.0018_cartitem_commerce_authority`
- original `shop.0018_checkoutline_commerce_authority` → integrated `shop.0019_checkoutline_commerce_authority`

This creates one forward-only graph without a sibling 0017, fake application, merge-only node, or migration-history edit. Digital migrations then add selection, snapshot, and reservation models as `digital_products.0002` through `0004`. All existing shared rows default to Standard and no data conversion or Digital record creation runs.

## Events

Batch B reuses the B1 append-only event vocabulary: `checkout_draft_created`, `checkout_draft_reused`, `cart_locked`, `stock_reservation_created`, `stock_reservation_released`, `checkout_canceled`, `checkout_expired`, and `cart_unlocked`. Events are in the same transaction as their state change and metadata is allowlisted. No new event enum or customer projection is introduced.

## Batch C prerequisites

Batch C must separately review payment request and callback ownership, PaymentTransaction changes, paid-state reservation consumption, Order and OrderItem authority/origin, finalization, fulfillment, entitlement, uncertain-payment recovery, and PaymentTransaction lock order. Batch D remains responsible for Digital APIs, URLs, full external projections, and integration documentation. Production remains NO-GO.
