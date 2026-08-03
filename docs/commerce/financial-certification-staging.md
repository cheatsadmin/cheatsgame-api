# Controlled Financial Certification (staging only)

This adapter certifies the real Financial Core lifecycle without contacting a
monetary provider. It exposes no callback, customer action URL, or browser-side
success control. A customer may create the ordinary PaymentAttempt; only the
bounded server command records certification authority.

## Required staging configuration

- `CHEATSGAME_RUNTIME_ENVIRONMENT=staging`
- `FINANCIAL_CERTIFICATION_PROVIDER_ENABLED=True`
- `FINANCIAL_CERTIFICATION_SECRET` (at least 32 high-entropy characters)
- `FINANCIAL_CERTIFICATION_ALLOWED_HOSTS` (explicit staging Backend host only)
- `FINANCIAL_CERTIFICATION_ACCOUNT_KEY`
- `FINANCIAL_CERTIFICATION_OWNER_KEY`

The provider is rejected during settings initialization outside an explicit
staging/test runtime. Staging hosts must also be present in `ALLOWED_HOSTS` and
must contain the staging identity. No secret is stored in the database.

## Provisioning and use

```bash
python manage.py configure_financial_certification
python manage.py configure_financial_certification --apply
```

The apply operation also provisions the staging-only receipt accounts and
policy required by funds recognition. If no active Digital Products commercial
accounting policy exists, it provisions the staging certification policy; an
existing active Digital Products policy remains authoritative.

After the customer creates a payment request with provider
`financial_certification`, an authorized server operator records one explicit
Admin certification:

```bash
python manage.py certify_staging_payment \
  --payment-attempt <public-uuid> \
  --actor-id <active-admin-id> \
  --confirm
```

The command locks and validates the immutable attempt, amount, IRR currency,
provider authority, Checkout state, and Admin role. It appends sanitized audit
evidence and one durable verification work item. It does not update Payment,
Order, allocation, journals, inventory, or commercial finalization.

The ordinary bounded runtime remains authoritative:

```bash
python manage.py financial_runtime run-batch --apply --limit 10
```

Repeated command and runtime invocations converge on the existing idempotency
boundaries. Zarinpal configuration remains independent and unchanged.
