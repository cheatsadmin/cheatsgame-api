# Zarinpal Financial Core adapter

## Boundary

Zarinpal is registered only as adapter key `zarinpal-v4` under Financial Core
contract `c2a-v1`. Digital Checkout continues to create the existing
`Payment`, `PaymentAttempt`, and `PaymentTransaction`. The adapter performs
provider HTTP I/O outside database transactions and returns only canonical
request or verification results.

The legacy `cheatgame.shop.payments` Zarinpal flow remains isolated to Standard
commerce. Digital Checkout, the Financial callback route, verification runtime,
funds recognition, commercial finalization, reservations, and fulfillment do
not import or call the legacy provider or its mutation services.

## Official API contract

The implementation follows Zarinpal's current official Payment Gateway v4
documentation:

- production request:
  `https://payment.zarinpal.com/pg/v4/payment/request.json`
- production verification:
  `https://payment.zarinpal.com/pg/v4/payment/verify.json`
- production customer handoff:
  `https://payment.zarinpal.com/pg/StartPay/{authority}`
- sandbox substitutes host `sandbox.zarinpal.com`;
- request and verification use JSON over HTTPS;
- the callback is an unsigned browser `GET` claim containing exactly
  `Authority` and `Status=OK|NOK`;
- request code `100` creates an Authority;
- verification code `100` is first verified success;
- verification code `101` is already-verified success;
- `ref_id` is the provider financial reference.

Because Zarinpal does not sign the callback, it is persisted only as an
`UNAUTHENTICATED_HINT`. It cannot mark a Payment paid. The route-bound
transaction and exact write-once Authority must match, after which durable
verification work performs the authoritative server-to-server verify call.

## Amount mapping

Financial Core and the launch capability are both canonical `IRR`.

| Source | Example | Conversion |
|---|---:|---:|
| Digital Offer / Cart public IRT | `51,000 IRT` | placement bridge ×10 |
| Financial Core canonical amount | `510,000 IRR` | authoritative |
| Zarinpal request currency | `IRR` | explicit `currency=IRR` |
| Zarinpal request amount | `510,000` | identity |
| Zarinpal verification amount | `510,000` | exact replay of frozen provider amount |

The adapter rejects non-positive, fractional, mismatched, non-IRR, or
non-exact amounts. Storefront amounts never enter provider verification.

## Configuration

No values or credentials are committed. Configuration uses:

- `FINANCIAL_ZARINPAL_ENABLED`
- `ZARINPAL_MERCHANT_ID`
- `ZARINPAL_SANDBOX`
- `ZARINPAL_REQUEST_URL`
- `ZARINPAL_VERIFY_URL`
- `ZARINPAL_STARTPAY_URL`
- `ZARINPAL_CONNECT_TIMEOUT_SECONDS`
- `ZARINPAL_READ_TIMEOUT_SECONDS`
- `FINANCIAL_PROVIDER_CALLBACK_BASE_URL`
- `DIGITAL_PAYMENT_CUSTOMER_RETURN_BASE_URL`
- `FINANCIAL_ZARINPAL_ACCOUNT_KEY`
- `FINANCIAL_ZARINPAL_OWNER_KEY`
- `FINANCIAL_ZARINPAL_AUTHORITY_EXPIRY_SECONDS`
- `FINANCIAL_ZARINPAL_FINALITY_WINDOW_SECONDS`

The merchant row stores only
`env://ZARINPAL_MERCHANT_ID`. Enabling Financial Core Zarinpal with missing or
malformed configuration fails closed. Production settings prohibit sandbox
mode. Request, verify, and StartPay hosts must all match the explicit mode.

Inspect configuration without mutation:

```text
python manage.py configure_zarinpal
```

Create missing immutable provider/capability/account rows and enable the
provider kill switches:

```text
python manage.py configure_zarinpal --apply
```

The command is idempotent and rejects conflicting existing immutable
configuration. It does not create a Payment or contact Zarinpal.

## Result mapping

### Request

| Zarinpal result | Canonical result |
|---|---|
| `100` plus valid mode-matching Authority | `CUSTOMER_ACTION_REQUIRED` |
| `-12` rate limit | `NO_EFFECT_RETRYABLE` |
| `-9`, `-10`, `-11`, `-13` through `-19`, `-41` | `CONFIGURATION_FAILURE` |
| provider HTTP 5xx | `OUTCOME_UNKNOWN`, retained hold/review |
| timeout/network failure | `OUTCOME_UNKNOWN` |
| malformed JSON/envelope/Authority | `PROTOCOL_FAILURE` |
| Authority prefix contradicts mode | `SECURITY_FAILURE` |
| other rejection | `PROTOCOL_FAILURE` |

### Verification

| Zarinpal result | Canonical result |
|---|---|
| `100` | final paid `CONFIRMED_SUCCESS` |
| `101` | final paid idempotent `CONFIRMED_SUCCESS` |
| `-50` amount mismatch | `MISMATCH` |
| `-51` unsuccessful payment | final unpaid `CONFIRMED_DECLINE` |
| `-52` provider error | retryable `OUTCOME_UNKNOWN` |
| `-53` merchant mismatch | `SECURITY_FAILURE` |
| `-54` invalid Authority | final unpaid `NOT_FOUND_FINAL` |
| `-55` payment not found | final unpaid `NOT_FOUND_FINAL` |
| HTTP 5xx, timeout, network failure | retryable `OUTCOME_UNKNOWN` |
| malformed JSON/envelope or missing successful `ref_id` | `PROTOCOL_FAILURE` |
| unknown code | `PROTOCOL_FAILURE` |

Code `100/101` is recognized only from a fresh server-to-server response bound
to the frozen merchant account, exact Authority, exact IRR amount, transaction,
and adapter version. `ref_id` is persisted through the existing immutable
provider-reference evidence and allocation guards.

## Customer return

The callback URL is Backend-owned and transaction-specific. After evidence
ingestion, a known transaction is redirected with HTTP 303 to the fixed
`DIGITAL_PAYMENT_CUSTOMER_RETURN_BASE_URL/{checkout_uuid}/?provider_return=1`.
The return target cannot be supplied by the provider or customer. Storefront
then refreshes Backend authority and never infers paid state from the query.

## Sandbox checklist

No automated test performs network I/O. With approved local sandbox
configuration:

1. set the variables above with `ZARINPAL_SANDBOX=true`;
2. run Django system checks;
3. run `configure_zarinpal`, inspect, then explicitly use `--apply`;
4. request one disposable Digital Checkout payment;
5. confirm the returned Authority starts with `S`;
6. confirm handoff host is `sandbox.zarinpal.com`;
7. complete or cancel only in Zarinpal sandbox;
8. confirm callback creates verification work but does not mark paid;
9. run one bounded Financial Runtime batch;
10. confirm `100` or `101`, exact amount, immutable `ref_id`, one allocation,
    one journal, one finalization, and one fulfillment obligation;
11. replay callback and runtime and confirm no duplicate commercial mutation.
