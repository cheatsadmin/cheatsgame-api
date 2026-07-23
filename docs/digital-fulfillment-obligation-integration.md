# Digital fulfillment obligation integration

## Boundary

The immutable commercial handoff is:

`Financial Payment PAID → CommercialFinalization → DigitalFulfillmentObligation`

`DigitalFulfillmentObligation` is the only commercial ownership source. It fixes the Order,
OrderItem, CheckoutLine and Digital snapshot, reservation, InventoryPool, quantity, purchased
fulfillment method, and owning customer. Operational code never copies or reassigns those fields.

The mutable execution boundary is:

`DigitalFulfillmentObligation (1) → DigitalFulfillmentItem (1) → operational evidence → Entitlement ACTIVE`

The operational layer does not recognize funds, consume reservations, decrement stock, place an
Order, or write financial/commercial Journals. Those frozen responsibilities remain in Financial
Core and the Commercial Finalizer.

## Dormant intake

`provision_digital_fulfillment_obligation` is an explicit internal transaction. It locks and
validates a finalized, paid obligation and atomically creates exactly one queued execution, one
`PENDING_FULFILLMENT` Entitlement, and one system activity. The command is replay-safe by stable
obligation and idempotency identities. The lock order is commercial obligation, then the
fulfillment-scoped idempotency identity. A complete graph paired with an incomplete or failed
idempotency record fails closed; it is not silently treated as success. Database uniqueness and
deferred graph guards require exactly one provisioning activity. Contradictory partial state fails
closed.

There is intentionally no URL, signal, task, worker, scheduler, or read-side provisioning hook in
this phase. Merely listing an obligation cannot create operational records.

## Execution and entitlement

Operational commands select an execution by public UUID and derive ownership through its
obligation. Allowed transitions, assigned-operator restrictions, Admin override, credential-text
screening, and activity visibility are server-controlled.

The legal status graph is closed:

| From | To | Command/evidence boundary |
|---|---|---|
| `QUEUED` | `WAITING_CUSTOMER` | customer contact |
| `WAITING_CUSTOMER` | `READY_FOR_STAFF` | in-store console receipt |
| `WAITING_CUSTOMER` | `IN_PROGRESS` | remote start after contact |
| `READY_FOR_STAFF` | `IN_PROGRESS` | work start after required evidence |
| `IN_PROGRESS` | `WAITING_CONFIRMATION` | remote handling requesting customer confirmation |
| `IN_PROGRESS` | `COMPLETED` | authorized staff completion with method evidence |
| `WAITING_CONFIRMATION` | `COMPLETED` | owner confirmation or authorized staff verification |
| any incomplete non-exception state | `EXCEPTION` | explicit exception command |
| `EXCEPTION` | `QUEUED`, `WAITING_CUSTOMER`, or `READY_FOR_STAFF` | explicit retry; target derives from retained evidence |

`COMPLETED` is terminal. Status, `completed_at`, method, start time, assignment, ACTIVE Entitlement,
and `activated_at` cannot be reversed through ORM or SQL. Refund, revocation, replacement, and
support review remain deferred rather than being represented by broad mutable entitlement states.

Entitlement is created as `PENDING_FULFILLMENT`. Payment and commercial finalization cannot make it
active. Completion and activation share one transaction and require work-start evidence, one
matching purchased installation record, and method-specific evidence:

- in-store: console received and staff verification;
- remote: remote handling plus customer confirmation or staff verification.

The supported Entitlement lifecycle is only `PENDING_FULFILLMENT → ACTIVE`. An Entitlement has no
expiry field. Its permanent ownership identity cannot be deleted or reassigned. PostgreSQL deferred
guards reject an ACTIVE Entitlement without its completed execution and reject a completed
execution without its ACTIVE Entitlement and evidence.

Every command uses a canonical fingerprint containing the execution, actor, command, and every
semantic payload field. Replay is checked before current-state validation: identical replay returns
the existing coherent result even after later state advancement, while a reused key with a changed
payload fails closed. Activities and installation evidence remain unique by idempotency identity.

## Purchased and bonus installation evidence

PURCHASED evidence must match the Product and DeliveredVersion in the immutable Digital snapshot.
Current evidence is the one immutable `RECORDED` leaf with no successor. Correction creates a new
immutable `RECORDED` successor; removal creates an immutable `REMOVED` successor. The one-to-one
successor relation prevents forks, cross-execution/classification correction is rejected, and a
superseded row cannot satisfy completion. A `REMOVED` leaf is terminal and cannot receive another
successor, so neither mutation nor a new correction can revive a removed chain. BONUS evidence belongs only to
the operational execution: it creates no obligation, OrderItem, Entitlement, inventory mutation,
or commercial amount.

Staff and customer evidence persists a bounded authority classification. On insertion PostgreSQL
checks the active user type, obligation ownership, current assignment, or explicit Admin override.
Completion guards count only activities with valid method-specific actor authority. System evidence
is limited to the initial provisioning event. Writable operational text is screened for password,
OTP, token, cookie, login, credential, recovery-code, and secret-like material.

## Multi-game orders

Each Digital obligation provisions and completes independently, including repeated Product
identities in one Order. The Order fulfillment projection advances to delivered only when every
authoritative Digital obligation has a completed execution. Completing one game never changes a
sibling execution or entitlement.

## Security and projections

Admin/customer projections traverse execution → obligation → immutable snapshot and Financial Core
Payment. They do not use legacy `shop.PaymentTransaction` as payment truth and never expose Journal,
allocation, provider evidence, review diagnostics, Pool/reservation internals, credentials, or
internal-only activities to customers. Notes reject credential-like content.

## Deferred work

Admin and Storefront URLs/UI, automatic intake workers, scheduling integration, and any production
activation remain deferred. ReviewCase integration, source accounts, credentials, providers,
payment callbacks, refunds, revocation, replacement, cancellation, and support workflows are
outside this operational phase.
