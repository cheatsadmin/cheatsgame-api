# API-06 Financial Truth Verification

API-06 is a dormant internal worker boundary. It consumes one durable
`VerificationWorkItem`, claims a finite lease, evaluates the immutable provider
identity and evidence, optionally performs one read-only provider query outside
all database transactions, and appends one `Verification` observation. It adds
no URL, task registration, scheduler, signal, Admin action, or provider adapter.

## Truth boundary

`Verification` is an immutable financial observation, not recognized funds.
Multiple observations may exist for one `PaymentTransaction`; no observation is
updated or deleted. `derive_current_verification_interpretation` applies
Financial Core policy across the full history. Review or contradiction evidence
blocks recognition and cannot disappear behind a newer row.

The API-06 path uses the existing result persistence command in `truth_only`
mode. It updates only verification work/claim control state, append-only
verification evidence, provider-reference uniqueness evidence, and idempotency
evidence. It does not mutate Payment, PaymentAttempt, PaymentTransaction,
confirmed amounts, Journals, `FinancialAllocation`, ReviewCase, reservations,
inventory, commercial finalization, fulfillment, or Entitlement. It does not
create or execute `APPLY_VERIFIED_FUNDS` work.

Eligibility requires an authoritative evidence basis: either a frozen
server-to-server provider query or settlement-grade authenticated callback
evidence. Missing evidence, browser evidence, callback hints, and ordinary
callback assertions remain review-only. Provider-reference ownership is bound
to the exact Verification observation, transaction, merchant-account version,
and provider reference.

## Provider policy and evidence

Every claim freezes the PaymentTransaction, ProviderDefinition,
MerchantAccountVersion, ProviderCapabilityVersion, adapter contract, operation,
merchant reference, provider money, and canonical IRR obligation. Retries create
new immutable claims but retain that same query identity.

Provider query is required unless the exact immutable capability explicitly sets
`callback_verification_is_final` and the linked receipt is authenticated with the
capability's required strength, method, authentication version, historical
signing-key identity, and valid replay window. The immutable ProviderEvent must
itself contain a trusted event identity, merchant and provider references,
operation, amount, unit, financial effect, finality, and provider occurrence
evidence. Missing callback fields are never filled from expected transaction
values. Incomplete evidence falls back to a read-only provider query when the
frozen capability permits lookup; otherwise it becomes review evidence. Callback
authentication alone is not settlement authority. Browser hints, contradictions,
pending states, and unknown outcomes require a provider query when supported.
Unsupported or disabled frozen policy fails closed.

The adapter receives only an immutable verification envelope and returns a
normalized result. Exact amount and unit comparisons use integer/Decimal money;
there is no tolerance, float conversion, or client price. Provider, account,
capability, merchant reference, provider reference, operation, amount, unit,
financial effect, and finality must all agree for eligible final success.

## Idempotency and recovery

Worker stage UUIDs are derived from the caller's root UUID plus immutable work,
transaction, provider, account, capability, operation, and stage identity.
Identical replay returns the existing claim/observation without another provider
call. A crash before persistence leaves the claim durable; after lease expiry a
new bounded read-only query can recover without inferring unpaid status. Pending,
retryable unknown, timeout, and transport outcomes remain waiting according to
configurable retry policy. Claims and observations remain append-only.

## Interpretation precedence

Current interpretation is a bounded read-only projection with this precedence:

1. Any review, mismatch, security, protocol, or contradictory observation blocks
   recognition.
2. Final success after authoritative final-unpaid evidence is a temporal
   contradiction and remains review-blocked.
3. Otherwise, exact final-paid evidence is eligible only when immutable policy
   lineage and provider-reference ownership are coherent.
4. Authoritative final-unpaid evidence is final unpaid only when no blocker exists.
5. Retryable pending or unknown evidence is waiting while attempts remain.
6. Exhausted pending, unknown, timeout, or unavailable-provider evidence becomes
   terminal review; exhaustion never implies unpaid.

PostgreSQL insertion guards validate claim, work, transaction, ProviderEvent,
provider policy, sequence, and successful provider-reference ownership. The
ownership check is deferred so a valid service transaction can append the
Verification and reference ownership evidence atomically.

## Deferred boundaries

Funds Recognition, `FinancialAllocation`, Journal posting, Payment paid-state
transitions, Commercial Finalization, inventory consumption, fulfillment,
Entitlement, refunds, and chargebacks remain dormant and outside API-06.
