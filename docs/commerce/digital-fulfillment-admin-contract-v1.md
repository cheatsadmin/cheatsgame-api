# Digital Fulfillment Admin Contract v1

This internal contract projects the existing Digital Fulfillment domain for the
existing Admin queue and detail pages. The Backend remains the sole authority
for identity, state, prerequisites, permissions, and transitions.

## Endpoints

- `GET /api/digital-products/admin/fulfillments/`
- `GET /api/digital-products/admin/fulfillments/{fulfillment_uuid}/`
- `GET /api/digital-products/admin/operators/`
- `GET /api/digital-products/admin/fulfillment-options/`
- `POST /api/digital-products/admin/fulfillments/{fulfillment_uuid}/{command}/`

All endpoints require an authenticated, active `ADMIN` or `MANAGER`.

## Queue projection

Each row returns the fulfillment UUID, Order identity and tracking code,
customer display name and phone number, frozen game and selection data,
commercial payment summary, operational status and waiting reason, assigned
operator and assignment state, appointment, latest activity, timestamps,
`next_permitted_action`, and complete `allowed_actions`.

Queue filters, ordering, limit, and offset are server validated. Queue payloads
never contain account credentials, operational notes, or activity history.

## Detail projection

Detail adds the authoritative customer email when present, payment summary,
appointment and Entitlement context, installation evidence, exception context,
safe operational notes, activity timeline, and a revision timestamp.

There is no secure credential domain in the current repository. Therefore
`credential_state` is `not_supported` and `account_information` is `null`.
Credentials must not be stored in notes or activity records.

## Commands

Canonical action identifiers are:

- `assign_operator`
- `record_contact`
- `change_method`
- `record_console_received`
- `start_work`
- `record_purchased_installation`
- `record_remote_handling`
- `staff_verify`
- `open_exception`
- `retry`
- `add_note`
- `record_bonus`

The Admin renders state-changing controls only from `allowed_actions`.
Commands invoke the existing service layer and use its lock, permission,
transition, audit, and idempotency rules. A successful command returns the
refreshed detail projection.

Conflicting idempotency or concurrent state returns HTTP 409 with
`fulfillment_conflict`. A command invalid in the current authoritative state
returns HTTP 409 with `invalid_fulfillment_transition`. The Admin must refetch
on either conflict.

## Sensitive-data boundary

Passwords, recovery data, tokens, two-factor codes, and account secrets are
forbidden from queue payloads, detail history, notes, URLs, browser storage,
logs, notifications, and generic errors. Introducing secure credential
persistence is a separate security-scoped change.
