# API-05 — Provider Callback Ingestion

API-05 exposes one provider-facing Financial Core boundary:

`POST /api/financial-core/providers/{provider_key}/callbacks/{transaction_public_id}/`

The transaction UUID is the opaque callback identity created by the immutable API-04 request envelope. The route resolves the exact provider, capability, merchant-account, adapter, and authentication policy recorded on that transaction. Payload data cannot replace that policy.

## Boundary

The endpoint bounds the transport, authenticates exact raw bytes, resolves known exact replay, applies the first-seen replay window, normalizes through the versioned provider adapter, and atomically persists callback evidence. It may create one dormant `VerificationWorkItem`; it never executes Verification.

Callbacks are provider assertions, not payment truth. API-05 cannot update confirmed money, Payment status, Orders, reservations, inventory, Journals, commercial finalization, fulfillment, or Entitlement.

## Evidence

`CallbackReceipt` is append-only delivery evidence. It retains bounded hashes and safe classifications, not the raw body, unrestricted headers, signatures, credentials, or customer data.

`ProviderEvent` is append-only normalized evidence. Exact authenticated replay recovers the existing acknowledgement. Concurrent duplicate delivery may preserve another receipt, but it cannot duplicate the semantic event or verification work.

Changed bytes or immutable authentication context under the same trusted provider event identity create a distinct `CONTRADICTORY` ProviderEvent. It has an immutable link to the original event and one distinct `ESCALATE_UNKNOWN_OUTCOME` work identity. Neither assertion is overwritten.

## Authentication and replay

The exact `ProviderCapabilityVersion` declares the permitted authentication strength. Authentication and normalization occur outside database transactions. Historical key/authentication versions must remain available for the provider retry period; current keys are never substituted for historical exact replay.

Processing order is transport bounds, immutable policy resolution, exact-byte authentication, trusted identity/hash derivation, exact persisted replay lookup, first-seen replay-window validation, normalization, and persistence. A first-seen stale callback fails closed. An already authenticated exact replay remains recoverable after its timestamp becomes stale.

`UNAUTHENTICATED_HINT` is allowed only when the exact capability declares no callback authentication, the route and backend-issued merchant reference resolve the same single transaction, and provider/account/capability ownership is exact. Otherwise only quarantined receipt evidence is retained and no ProviderEvent or work item is created.

## Responses

- `200`: authoritative exact replay or concurrent duplicate recovery.
- `202`: new evidence durably accepted, including safely quarantined unmatched evidence.
- `400`: structurally invalid or malformed callback.
- `401`: authentication or replay-window rejection.
- `404`: provider/callback ingress identity cannot be safely resolved.
- `409`: delivery identity conflict or contradictory callback evidence.
- `413`: body too large.
- `415`: unsupported content type.
- `503`: configured callback authentication capability is unavailable.

Responses contain only safe acknowledgement/error fields. They expose no customer, Order, Payment, account, claim, verification, provider-secret, or matching details.

## Dormancy

The production provider registry remains empty until a separately approved provider activation. API-05 registers no worker, task, signal, scheduler, provider query, callback verification, browser verification, funds-recognition, finalization, or fulfillment invocation. Existing Standard and legacy callback routes remain unchanged.
