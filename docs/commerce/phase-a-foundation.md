# Commerce Phase A Foundation

## Purpose

Phase A introduces dormant, backward-compatible commerce primitives. It does not switch the live Cart, Order, payment request, callback, result page, or shipping flow.

## Models Introduced

- Checkout: server public UUID, client idempotency UUID, fingerprint, lifecycle, expiry, and manual-review fields.
- CheckoutLine and CheckoutLineAttachment: immutable commercial snapshots.
- CheckoutShippingSnapshot: address, delivery, cost, and schedule snapshot container.
- StockReservation: dormant inventory reservation records.
- CommerceEvent: application-level append-only safe diagnostic timeline. Admin editing/deletion is disabled, but unrestricted ORM or database access can still mutate it because no database trigger is installed.

## Existing Models Extended

- Cart: state, lock reason, active Checkout, lock timestamp, and version.
- Order: nullable Checkout relation and fulfillment status.
- PaymentTransaction: nullable Checkout relation, verification claim, result-token hash, provider amount, manual-review fields, and new statuses.

Legacy Orders and PaymentTransactions remain valid with `checkout=NULL`. Existing serializers and endpoints continue to operate.

## Active Behavior

- Django can persist and inspect the new schema.
- Cart lock/unlock helpers are transaction-safe when explicitly called.
- Fingerprinting, expiry calculation, event sanitization, event append, legacy fulfillment mapping, and manual-review helpers are available to future services.
- New models are inspectable through read-only Django admin pages.
- Management commands can report integrity and reconciliation conditions.

## Intentionally Dormant

- Legacy Cart mutations do not enforce locks.
- Legacy submit-order does not create Checkout records.
- Legacy callbacks do not perform backend-owned V2 verification.
- PaymentSuccess remains unchanged.
- Stock reservations are not created by the live flow.
- Checkout snapshots are not created by the live flow.
- Shipping price remains outside the payment total architecture.
- No automatic refund or manual-review resolution action exists.
- No Phase A feature flag is implemented or enabled; later activation flags remain a rollout-plan item.

## Commands

```bash
python manage.py expire_checkouts
python manage.py expire_checkouts --apply
python manage.py reconcile_payment_transactions
python manage.py audit_checkout_integrity
```

`expire_checkouts` is dry-run/report-only by default. Mutation requires the explicit `--apply` flag. Apply mode affects only eligible records in the new Checkout table and skips active or uncertain payment attempts and manual-review records. Because Phase A does not route live traffic into that table, legacy Orders, Carts, and PaymentTransactions are not eligible.

`reconcile_payment_transactions` and `audit_checkout_integrity` are report-only. They output internal numeric IDs and state codes, never customer personal data or secrets.

## Migration Notes

- Migration is additive.
- All fields added to legacy commerce records are nullable or have non-destructive defaults.
- Existing Orders are not backfilled into Checkout snapshots.
- Conditional uniqueness constraints require PostgreSQL in staging/production; Django's SQLite test backend supports the model tests used here but is not the deployment target.
- Rollout rollback must disable new behavior rather than reverse the migration.
- Future deployments gate migrations with the advisory-locked Liara pre-start mechanism documented in `docs/deployment/database-migrations.md`.

## Tests

```bash
python manage.py check
python manage.py makemigrations --check
python manage.py test cheatgame.shop.test_commerce_foundation
```

Run the migration and conditional-constraint tests against PostgreSQL before staging deployment.

## Phase B Prerequisites

- Review transition and locking services.
- Confirm expiry job scheduling and monitoring.
- Implement provider callback verification claims.
- Implement idempotent Checkout creation and payment attempts.
- Add server reconciliation for provider-paid but unfinalized transactions.
- Keep frontend result pages read-only after callback V2 activates.

## Operational Warning

Phase A does not make payment processing safe for real money. The real gateway remains NO-GO until server-owned shipping cost, backend-owned callback verification, idempotency, recovery, reconciliation, and concurrency testing are complete.
