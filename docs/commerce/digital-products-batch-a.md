# Commerce B1 + Digital Products: Batch A foundation

## Scope and dormancy

Batch A adds only the Digital catalog and inventory domain on top of deployed Commerce B1. It does not add customer APIs, Cart or Checkout behavior, reservations, payments, callbacks, Orders, fulfillment, entitlements, Admin mutation screens, or Storefront behavior. `COMMERCE_CHECKOUT_V2_ENABLED` continues to default to `False`; no Digital payment setting or implementation is present. No migration activates or converts a Product, creates an Offer, creates a Pool, or creates stock.

This foundation is dormant until authorized staff explicitly configure the domain through its internal service boundary. Deployment and production activation remain NO-GO.

## Authority and ownership

`Product.commerce_authority` is explicit and persisted:

- `standard_commerce` (`STANDARD_COMMERCE`) remains the default for all existing and new Products.
- `digital_products` (`DIGITAL_PRODUCTS`) is allowed only for game Products and is set only by the explicit activation service after readiness checks.

Presence of a DeliveredVersion or DigitalOffer never infers or changes authority. Standard Commerce continues to own `Product.price`, `Product.off_price`, and `Product.quantity`. Digital Products never uses `Product.quantity` for stock.

## DeliveredVersion

DeliveredVersion identifies an active or historical deliverable native console version (PS4 or PS5) for a game Product. One active record per Product and native console is allowed. It is protected from cascading Product deletion. It is neither price nor inventory.

## InventoryPool and stock evidence

InventoryPool is the authoritative Digital stock balance. In Batch A, with no reservation model, `sellable_quantity` is both total sellable and available quantity. Batch B must define held and available semantics when reservations are introduced.

Stock adjustments are performed by `adjust_pool_stock`, which locks the Pool row with `SELECT ... FOR UPDATE`, rejects negative results, records actor/reason/before/after values, and uses a unique UUID idempotency key. Matching retries return the existing adjustment; conflicting reuse is rejected. Initial nonzero stock created with an Offer also produces adjustment evidence.

PoolStockAdjustment is append-only at the application/model layer. Database constraints enforce nonzero deltas, nonnegative quantities, the balance equation, valid reasons, and idempotency uniqueness. There is no database trigger preventing a privileged raw SQL update, so database-level immutability is intentionally not claimed. The models are not registered in generic Django Admin and no stock mutation API is exposed.

Compatible Offers may share a Pool only when they use the same DeliveredVersion and capacity. Linking locks both Pools and never merges or transfers balances. Moving an Offer to a new independent Pool creates a paused, zero-stock Pool and preserves the old Pool and its history.

## DigitalOffer and readiness

DigitalOffer owns the Digital sale price as a whole backend Decimal amount. It links one DeliveredVersion to one InventoryPool and starts in `draft`. Offer activation requires:

- explicit `DIGITAL_PRODUCTS` Product authority;
- an active, compatible DeliveredVersion;
- a non-archived Pool;
- a valid console/capacity relationship.

Product activation is a separate explicit Admin operation. Readiness requires a game Product, at least one active DeliveredVersion, at least one valid non-archived Offer, and compatible shared Pools. Offers may be configured while the Product is Standard, but cannot become active before authority activation. Deactivation requires all active Offers to be paused, hidden, or archived and preserves catalog and stock history.

## Migration lineage

```text
product.0019_category_name_not_globally_unique
└── product.0020_deliveredversion_product_commerce_authority
    └── digital_products.0001_initial

shop.0017_checkoutshippingsnapshot_is_pricing_finalized  (unchanged Commerce B1 head)
```

`digital_products.0001_initial` creates only InventoryPool, DigitalOffer, and PoolStockAdjustment and also depends on the configured user model for adjustment actors. There is no Shop dependency, destructive operation, data migration, migration replacement, or automatic backfill beyond the safe Product field default.

## Batch B prerequisites and deferred conflicts

Batch B must manually reconcile the Digital branch's conflicting `shop.0017_cartitem_commerce_authority` lineage with deployed Commerce B1 `shop.0017`, then introduce authority-aware Cart/Checkout snapshots, reservations, expiry, and the eventual Shop merge migration. Reservation-aware availability and restrictions on moving Pools with effective reservations belong to Batch B. Payment, callbacks, finalization, fulfillment, APIs, URLs, and feature activation remain later-batch work.
