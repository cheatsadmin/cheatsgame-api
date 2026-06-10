from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Dict, Optional
from urllib.parse import urlencode

import requests
from django.conf import settings

from cheatgame.shop.models import PaymentTransaction


@dataclass(frozen=True)
class PaymentRequestResult:
    authority: str
    payment_url: str
    payload: Dict


@dataclass(frozen=True)
class PaymentCallbackResult:
    authority: str
    payload: Dict


@dataclass(frozen=True)
class PaymentVerifyResult:
    is_paid: bool
    payload: Dict
    ref_id: str = ""
    trace_no: str = ""
    error_code: str = ""
    error_message: str = ""


class PaymentProvider:
    name = ""

    def create_payment_request(self, *, transaction: PaymentTransaction, callback_url: str) -> PaymentRequestResult:
        raise NotImplementedError

    def parse_callback(self, *, query_params) -> PaymentCallbackResult:
        raise NotImplementedError

    def verify(self, *, transaction: PaymentTransaction) -> PaymentVerifyResult:
        raise NotImplementedError


class PaymentProviderError(Exception):
    pass


class FakePaymentProvider(PaymentProvider):
    name = "fake"

    def create_payment_request(self, *, transaction: PaymentTransaction, callback_url: str) -> PaymentRequestResult:
        authority = f"FAKE-{transaction.id}"
        query = urlencode({"authority": authority, "status": "OK"})
        payment_url = f"{callback_url}?{query}"
        return PaymentRequestResult(
            authority=authority,
            payment_url=payment_url,
            payload={
                "provider": self.name,
                "authority": authority,
                "amount": str(transaction.amount),
                "callback_url": callback_url,
                "payment_url": payment_url,
            },
        )

    def parse_callback(self, *, query_params) -> PaymentCallbackResult:
        payload = {key: query_params.get(key) for key in query_params.keys()}
        authority = payload.get("authority", "")
        return PaymentCallbackResult(authority=authority, payload=payload)

    def verify(self, *, transaction: PaymentTransaction) -> PaymentVerifyResult:
        callback_status = str(transaction.callback_payload.get("status", "")).upper()
        if callback_status == "OK":
            return PaymentVerifyResult(
                is_paid=True,
                ref_id=f"FAKE-REF-{transaction.id}",
                trace_no=f"FAKE-TRACE-{transaction.id}",
                payload={
                    "provider": self.name,
                    "authority": transaction.gateway_authority,
                    "amount": str(transaction.amount),
                    "status": "paid",
                },
            )
        return PaymentVerifyResult(
            is_paid=False,
            error_code="fake_payment_failed",
            error_message="Fake payment was not approved.",
            payload={
                "provider": self.name,
                "authority": transaction.gateway_authority,
                "amount": str(transaction.amount),
                "status": "failed",
                "callback_status": callback_status,
            },
        )


class ZarinpalProvider(PaymentProvider):
    name = "zarinpal"
    paid_codes = {"100", "101"}

    def __init__(self):
        self.merchant_id = settings.ZARINPAL_MERCHANT_ID
        self.sandbox = settings.ZARINPAL_SANDBOX
        self.request_url = settings.ZARINPAL_REQUEST_URL
        self.verify_url = settings.ZARINPAL_VERIFY_URL
        self.startpay_url = settings.ZARINPAL_STARTPAY_URL
        self.amount_unit = settings.PAYMENT_AMOUNT_UNIT.upper()

    def _require_merchant_id(self):
        if not self.merchant_id:
            raise PaymentProviderError("ZARINPAL_MERCHANT_ID is required for Zarinpal payments.")

    def _to_zarinpal_amount(self, amount) -> int:
        try:
            decimal_amount = Decimal(amount)
        except (InvalidOperation, TypeError) as exc:
            raise PaymentProviderError("Payment amount is invalid.") from exc

        if self.amount_unit == "IRT":
            decimal_amount *= Decimal("10")
        elif self.amount_unit != "IRR":
            raise PaymentProviderError("PAYMENT_AMOUNT_UNIT must be IRT or IRR.")

        if decimal_amount <= 0 or decimal_amount != decimal_amount.to_integral_value():
            raise PaymentProviderError("Payment amount must be a positive whole number.")
        return int(decimal_amount)

    def _post_json(self, *, url: str, payload: Dict) -> Dict:
        try:
            response = requests.post(url=url, json=payload, timeout=10)
            try:
                response_payload = response.json()
            except ValueError as exc:
                if response.status_code >= 400:
                    raise PaymentProviderError(
                        f"Zarinpal HTTP {response.status_code}: {response.text}"
                    ) from exc
                raise PaymentProviderError("Zarinpal returned an invalid JSON response.") from exc
            if response.status_code >= 400:
                raise PaymentProviderError(f"Zarinpal HTTP {response.status_code}: {response.text}")
            return response_payload
        except requests.RequestException as exc:
            raise PaymentProviderError("Zarinpal request failed.") from exc

    def _extract_error(self, *, response_payload: Dict, fallback_code: str = "zarinpal_error") -> tuple:
        errors = response_payload.get("errors") or {}
        data = response_payload.get("data") or {}
        if isinstance(errors, dict) and errors:
            return str(errors.get("code", fallback_code)), str(errors.get("message", "Zarinpal payment failed."))
        if isinstance(errors, list) and errors:
            first_error = errors[0]
            if isinstance(first_error, dict):
                return str(first_error.get("code", fallback_code)), str(
                    first_error.get("message", "Zarinpal payment failed.")
                )
            return fallback_code, str(first_error)
        return str(data.get("code", fallback_code)), str(data.get("message", "Zarinpal payment failed."))

    def _build_startpay_url(self, *, authority: str) -> str:
        if "{" in self.startpay_url:
            return self.startpay_url.format(authority=authority, Authority=authority)
        return f"{self.startpay_url.rstrip('/')}/{authority}"

    def _mask_sensitive_payload(self, payload: Dict) -> Dict:
        return {**payload, "merchant_id": "***"}

    def create_payment_request(self, *, transaction: PaymentTransaction, callback_url: str) -> PaymentRequestResult:
        self._require_merchant_id()
        gateway_amount = self._to_zarinpal_amount(transaction.amount)
        request_payload = {
            "merchant_id": self.merchant_id,
            "amount": gateway_amount,
            "callback_url": callback_url,
            "description": f"Cheats order #{transaction.order_id}",
        }
        response_payload = self._post_json(url=self.request_url, payload=request_payload)
        data = response_payload.get("data") or {}
        if str(data.get("code")) != "100":
            error_code, error_message = self._extract_error(response_payload=response_payload)
            raise PaymentProviderError(f"{error_code}: {error_message}")

        authority = str(data.get("authority", ""))
        if not authority:
            raise PaymentProviderError("Zarinpal did not return a payment authority.")

        payment_url = self._build_startpay_url(authority=authority)
        return PaymentRequestResult(
            authority=authority,
            payment_url=payment_url,
            payload={
                "provider": self.name,
                "sandbox": self.sandbox,
                "amount": str(transaction.amount),
                "amount_unit": self.amount_unit,
                "gateway_amount": gateway_amount,
                "callback_url": callback_url,
                "payment_url": payment_url,
                "request": self._mask_sensitive_payload(request_payload),
                "response": response_payload,
            },
        )

    def parse_callback(self, *, query_params) -> PaymentCallbackResult:
        payload = {key: query_params.get(key) for key in query_params.keys()}
        authority = payload.get("Authority") or payload.get("authority") or ""
        callback_status = payload.get("Status") or payload.get("status") or ""
        payload.update(
            {
                "provider": self.name,
                "authority": authority,
                "status": callback_status,
            }
        )
        return PaymentCallbackResult(authority=authority, payload=payload)

    def verify(self, *, transaction: PaymentTransaction) -> PaymentVerifyResult:
        self._require_merchant_id()
        callback_status = str(transaction.callback_payload.get("status", "")).upper()
        if callback_status and callback_status != "OK":
            return PaymentVerifyResult(
                is_paid=False,
                error_code="zarinpal_callback_not_ok",
                error_message="Zarinpal callback status was not OK.",
                payload={
                    "provider": self.name,
                    "authority": transaction.gateway_authority,
                    "callback_status": callback_status,
                    "status": "failed",
                },
            )

        gateway_amount = self._to_zarinpal_amount(transaction.amount)
        request_payload = {
            "merchant_id": self.merchant_id,
            "amount": gateway_amount,
            "authority": transaction.gateway_authority,
        }
        response_payload = self._post_json(url=self.verify_url, payload=request_payload)
        data = response_payload.get("data") or {}
        code = str(data.get("code", ""))

        payload = {
            "provider": self.name,
            "authority": transaction.gateway_authority,
            "amount": str(transaction.amount),
            "amount_unit": self.amount_unit,
            "gateway_amount": gateway_amount,
            "request": self._mask_sensitive_payload(request_payload),
            "response": response_payload,
        }

        if code in self.paid_codes:
            return PaymentVerifyResult(
                is_paid=True,
                ref_id=str(data.get("ref_id", "")),
                trace_no=str(data.get("card_hash") or data.get("trace_no") or data.get("card_pan") or ""),
                payload={**payload, "status": "paid", "code": code},
            )

        error_code, error_message = self._extract_error(response_payload=response_payload)
        return PaymentVerifyResult(
            is_paid=False,
            error_code=error_code,
            error_message=error_message,
            payload={**payload, "status": "failed", "code": code},
        )


def get_payment_provider(*, provider: Optional[str] = None) -> PaymentProvider:
    provider_name = (provider or settings.PAYMENT_GATEWAY_PROVIDER or FakePaymentProvider.name).lower()
    if provider_name == FakePaymentProvider.name:
        return FakePaymentProvider()
    if provider_name == ZarinpalProvider.name:
        return ZarinpalProvider()
    raise PaymentProviderError(f"Unsupported payment provider: {provider_name}")
