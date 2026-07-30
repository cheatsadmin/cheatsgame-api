# Digital inventory pool operations

New `InventoryPool` rows remain `PAUSED` until an authorized Admin explicitly
reviews and enables them. Offer activation and Product publication do not enable
inventory automatically.

The Admin catalog exposes only two inventory-state commands:

- `PAUSED -> ENABLED` through the Offer `enable-inventory` action.
- `ENABLED -> PAUSED` through the Offer `pause-inventory` action.

The commands are transactional, row-locked, idempotent, and Admin-only. They do
not change quantity, create reservations, publish Products, or alter historical
reservations. Actual customer availability continues to require an active Offer,
a published Digital Game, a valid active Delivered Version, a positive Offer
price, an `ENABLED` Pool, and positive quantity after effective reservations.

Activation rejects archived Pools, inactive or incoherent Offers, non-Digital
Product authority, unpublished games, invalid Console/Capacity lineage, inactive
Delivered Versions, and non-positive prices. Zero quantity is permitted but
remains sold out.

Each real status change emits the structured
`digital_inventory_pool_status_changed` operational audit log with actor, Offer,
Pool, previous status, and target status identifiers. Idempotent replays do not
emit a second change record.
