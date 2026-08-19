# Production successor operational contract

This document describes preparation only. None of these commands targets Production until the owner approves the cutover.

## Immutable behavioral baseline

- Backend: `a43ab056b965cfeb98b5318863412cf0b249438d`
- Storefront: `fa0e38cc8822e8d6b9022461fe5cc5137fe75c82`
- Admin: `91dc2493558b3eaaecd8419c3977dc68a460eb6a`

The successor release uses Python 3.13 / Django 5.2 LTS and Node 24 LTS. Production must use its own PostgreSQL database and media bucket.

## Catalog review and bootstrap

1. Generate a read-only manifest from the approved source database:

   `python manage.py catalog_promotion_manifest --pretty > catalog-promotion.json`

2. The owner reviews every record. Only `PRODUCTION_READY` records are eligible. `OWNER_REVIEW` and `EXCLUDE_STAGING_TEST` records are never imported.
3. Copy only the reviewed manifest's referenced media keys into the dedicated Production bucket. Do not copy customer, financial, fulfillment, certification, or test evidence.
4. On a fresh migrated disposable database, run:

   `python manage.py import_production_catalog catalog-promotion.json`

   This is a dry-run. Apply only after review with `--apply`, then run the dry-run again and compare the deterministic counts.
5. Generate and review the public-content manifest separately:

   `python manage.py public_content_promotion_manifest --pretty > public-content-promotion.json`

   It contains only public Stories, Sliders, Banners, Blogs, and Common Questions. It never exports comments, contact forms, messages, users, or other private submissions. Rehearse with `python manage.py import_production_public_content public-content-promotion.json`; use `--apply` only after owner review.
6. Provision the official Zarinpal provider using `configure_zarinpal` and inspect it before applying.
7. Inspect Production accounting policy readiness:

   `python manage.py configure_production_accounting --merchant-account-key <approved-key>`

   Apply only after the dry-run is reviewed. Repeated apply operations converge. A conflicting active receipt or Digital Products policy fails closed.
8. Configure the new Production owner Admin through secret environment values,
   inspect with `python manage.py configure_production_admin`, and apply once
   with `--apply`. The command never imports a staging identity, never prints
   the phone or password, is replay-safe, and rejects a conflicting identity.
   After the exact Admin can log in, remove the bootstrap identity/password
   variables from the application environment; the persisted Admin remains.

The catalog importer creates catalog identity, release metadata, delivered versions, Offers, and initial Inventory Pools. The public-content importer creates only its reviewed public records. Neither imports Customers, staff identities, comments, contact submissions, Carts, Checkouts, Orders, Payments, PaymentAttempts, ReviewCases, journals, allocations, reservations, fulfillment records, entitlements, or staging certification evidence.

## Media isolation

Use a dedicated Production bucket. Set `AWS_STORAGE_ENVIRONMENT=production`, an owner-approved bucket name without staging/test markers, and `AWS_S3_CUSTOM_DOMAIN=cdn.cheatsg.ir`. Staging keeps its current bucket. Copy reviewed catalog objects before import, verify object checksums and HTTPS reads, then make Production the sole writer to the Production bucket. Rollback keeps the bucket intact and switches application releases only; media objects are never deleted during rollback.

Keep `cdn.cheatsg.ir` attached to the legacy bucket while retained customer evidence still references it. Attach `media.cheatsg.ir` to the dedicated Production bucket and set `BLOG_MEDIA_PUBLIC_DOMAIN=media.cheatsg.ir` for new Blog HTML and inline media. After configuring Production storage delivery, run `python manage.py verify_public_media_delivery`. The check must retrieve both its temporary HTML and image probes from the application-generated public URLs before a release may accept new media writes.

## Runtime supervision

Liara's Django cron contract runs one `commerce_runtime_tick` every minute. The command obtains a PostgreSQL advisory lock, executes one bounded Financial Runtime batch, then activates eligible Digital Fulfillment obligations. The manifest also imposes a 50-second OS timeout; the command has a 45-second internal deadline. Overlapping executions exit successfully as `skipped_overlap`. Unresolved financial or fulfillment work returns a non-zero exit and emits sanitized counts/codes only.

Runtime health checks for launch operations:

- `python manage.py financial_runtime stats`
- `python manage.py activate_digital_fulfillment stats`
- Liara cron exit/log history
- ReviewCase counts and oldest due-work age

Application startup and health endpoints do not depend on cron availability.

## Production sequence after owner approval

1. Freeze the three approved successor hashes.
2. Verify a restorable Production database backup and record its identifier.
3. Create a new empty Production PostgreSQL database; never restore staging into it.
4. Configure all required Production secrets and exact origins. Leave indexing disabled.
5. Deploy Backend, run `migrate_with_advisory_lock`, and require liveness/readiness HTTP 200.
6. Copy only reviewed media into the dedicated Production bucket and verify checksums/HTTPS.
7. Import the approved catalog manifest and provision/inspect Zarinpal and accounting policies.
8. Deploy Admin and Storefront candidate hashes; verify role enforcement, canonical origin, sitemap, and `noindex` state.
9. Obtain owner approval for DNS and Zarinpal terminal/callback domain changes.
10. Perform one owner-approved low-value real payment and verify allocation, balanced journal, Commercial Finalization, inventory, and fulfillment without exposing evidence.
11. Enable indexing only after the real smoke passes.

Rollback trigger: any migration error, readiness failure, origin/canonical mismatch, authentication failure, payment outcome that cannot be observed, financial invariant failure, or missing runtime supervision. Restore the prior application releases. If a forward migration changed data incompatibly, restore the verified pre-cutover database backup rather than attempting ad-hoc reverse SQL. Revert DNS only if it had been changed, using the recorded pre-cutover values and TTLs.
