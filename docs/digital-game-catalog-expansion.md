# Digital Game Catalog Expansion V1

This workflow publishes an already-supported Digital Game as data. It does not
require a code commit, build, or application deployment.

## What the owner provides

- Game name and main image.
- State: `RELEASED`, `ANNOUNCED`/`COMING_SOON`, or `PREORDER_OPEN`.
- Platform/delivered version.
- Capacity, real price, inventory, and fulfillment constraints when the state is
  purchasable.
- Any store-specific commercial promise or wording that is not an objective game
  fact.

Admin/operators may prepare original Persian description, SEO title, meta
description, stable English slug, and verified public facts. Uncertain facts or
commercial promises remain `OWNER_REVIEW`; they are never invented.

## 1. Validate on Staging

```sh
python manage.py validate_game_for_production PRODUCT_ID --pretty
```

The command is read-only and reports six sections: identity, content, SEO,
media, commerce, and state. Its classification is exactly one of:

- `PRODUCTION_READY`: every required gate passes.
- `OWNER_REVIEW`: the output lists only the failed gates/decisions.
- `EXCLUDE_STAGING_TEST`: the record is an actual QA/seed/test record.

An unsupported state, pricing model, or fulfillment contract additionally emits
`PLATFORM_CAPABILITY_REQUIRED`. Stop publication and request an engineering
review; do not turn an ordinary publication into feature development.

## 2. Dry-run and prepare the promotion bundle

```sh
python manage.py promote_game_to_production PRODUCT_ID \
  --dry-run \
  --bundle-dir /protected/catalog-bundles/GAME-SLUG \
  --pretty
```

This does not mutate the database or authoritative storage. It creates an
operator-transfer artifact containing one manifest and the exact description and
image bytes. Every media object is bound to its storage key, byte size, and
SHA-256 checksum. Review `classification`, `blockers`, `intended_deltas`, and the
media list before transfer.

Only a `PRODUCTION_READY` bundle may be applied. Transfer the protected bundle
to the Production Backend through the approved Liara operator path; never place
it in Git.

## 3. Apply on Production

```sh
python manage.py promote_game_to_production \
  --apply \
  --bundle-dir /protected/catalog-bundles/GAME-SLUG \
  --manifest-sha256 SHA256_REPORTED_BY_DRY_RUN \
  --site-url https://cheatsg.ir \
  --api-url https://api.cheatsg.ir \
  --pretty
```

Apply verifies every bundled byte before writing. Existing media must have the
same checksum; conflicts fail closed. Database changes use the existing atomic,
exact-field importer. Replaying the same bundle creates no duplicate Product,
version, Offer, inventory pool, category link, or slug history. No Customer,
Cart, Checkout, Order, Payment, journal, Fulfillment, or Entitlement model is in
the promotion boundary.

The optional live verification checks the Backend detail, Storefront SSR detail,
canonical, structured data, and sitemap. Complete one bounded browser check for
the image, state-specific CTA, responsive layout, and absence of console errors.

## State rules

- `RELEASED`: published Digital authority, active version and Offer, valid
  Capacity, positive price, enabled positive inventory, valid fulfillment,
  description/image/SEO. It must not expose preorder state.
- `ANNOUNCED` / `COMING_SOON` / `DELAYED`: published identity, supported release
  information, active version, description/image/SEO, and no active purchase
  Offer. Price, inventory, and fulfillment are not required and no CTA is
  exposed.
- `PREORDER_OPEN`: all purchasable gates plus a release date, coherent preorder
  enablement, and the existing PreOrder structured-data contract.

## Slug changes

Put every former public slug in the manifest’s `legacy_slugs`. Apply resolves the
existing Product by active or historical identity, changes the active slug once,
and preserves the former slug in the global collision-safe history. The live
check requires only the new canonical in the sitemap. Separately verify that the
old URL permanently redirects in one hop.

## When a deployment is required

Do not deploy for a normal game. Engineering release work is justified only for
a genuinely unsupported reusable capability: a new lifecycle state, fulfillment
model, pricing model, media contract, schema contract, or another platform-level
behavior. Content, SEO, offers, inventory, media, and supported slug changes are
data operations.
