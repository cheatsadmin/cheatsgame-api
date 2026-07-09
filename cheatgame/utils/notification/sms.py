import json
import logging
from typing import Any

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


class SmsSendError(Exception):
    """Raised when the configured SMS provider rejects or fails a send."""


def send_sms(*, to: str, otp: str, pattern: str) -> dict[str, Any]:
    if not pattern:
        raise SmsSendError("SMS pattern is not configured.")

    if getattr(settings, "PANEL_SMS_API_KEY", None):
        return _send_faraz_pattern_sms(to=to, otp=otp, pattern=pattern)

    return _send_legacy_pattern_sms(to=to, otp=otp, pattern=pattern)


def _send_faraz_pattern_sms(*, to: str, otp: str, pattern: str) -> dict[str, Any]:
    url = settings.PANEL_SMS_URL or "https://api.iranpayamak.com/ws/v1/sms/pattern"
    line_number = getattr(settings, "PANEL_SMS_FROM", None)
    variable_name = getattr(settings, "PANEL_SMS_PATTERN_VARIABLE", None) or "code"

    if not line_number:
        raise SmsSendError("SMS sender line is not configured.")

    payload = {
        "code": pattern,
        "attributes": {
            variable_name: str(otp),
        },
        "recipient": to,
        "line_number": line_number,
        "number_format": "english",
    }
    headers = {
        "Accept": "application/json",
        "Api-Key": settings.PANEL_SMS_API_KEY,
        "Content-Type": "application/json",
    }

    response = requests.post(
        url=url,
        json=payload,
        headers=headers,
        timeout=settings.PANEL_SMS_TIMEOUT_SECONDS,
    )
    return _validate_provider_response(response=response, provider="faraz_pattern", pattern=pattern, recipient=to)


def _send_legacy_pattern_sms(*, to: str, otp: str, pattern: str) -> dict[str, Any]:
    if not settings.PANEL_SMS_URL or not settings.PANEL_SMS_USER or not settings.PANEL_SMS_PASS:
        raise SmsSendError("Legacy SMS credentials are not configured.")

    variable_name = getattr(settings, "PANEL_SMS_PATTERN_VARIABLE", None) or "verfication-code"
    data = {
        "username": settings.PANEL_SMS_USER,
        "password": settings.PANEL_SMS_PASS,
        "from": getattr(settings, "PANEL_SMS_FROM", None) or "+983000505",
        "to": to,
        "pattern_code": pattern,
        "input_data": json.dumps({variable_name: str(otp)}),
    }
    headers = {"Content-Type": "application/json"}
    response = requests.post(
        url=settings.PANEL_SMS_URL,
        params=data,
        headers=headers,
        timeout=settings.PANEL_SMS_TIMEOUT_SECONDS,
    )
    return _validate_provider_response(response=response, provider="legacy_pattern", pattern=pattern, recipient=to)


def _validate_provider_response(*, response: requests.Response, provider: str, pattern: str, recipient: str) -> dict[str, Any]:
    response_payload = _response_payload(response)

    if not response.ok:
        _log_provider_failure(
            provider=provider,
            pattern=pattern,
            recipient=recipient,
            http_status=response.status_code,
            payload=response_payload,
        )
        raise SmsSendError("SMS provider rejected the request.")

    if isinstance(response_payload, dict):
        status_value = str(response_payload.get("status", "")).lower()
        success_value = response_payload.get("success")
        if status_value and status_value != "success":
            _log_provider_failure(
                provider=provider,
                pattern=pattern,
                recipient=recipient,
                http_status=response.status_code,
                payload=response_payload,
            )
            raise SmsSendError("SMS provider returned a non-success status.")
        if success_value is False:
            _log_provider_failure(
                provider=provider,
                pattern=pattern,
                recipient=recipient,
                http_status=response.status_code,
                payload=response_payload,
            )
            raise SmsSendError("SMS provider returned success=false.")

    return {
        "http_status": response.status_code,
        "payload": response_payload,
    }


def _response_payload(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


def _log_provider_failure(*, provider: str, pattern: str, recipient: str, http_status: int, payload: Any) -> None:
    logger.warning(
        "sms_provider_failure provider=%s http_status=%s pattern=%s recipient_suffix=%s payload=%s",
        provider,
        http_status,
        pattern,
        recipient[-4:] if recipient else "",
        _redact_payload(payload),
    )


def _redact_payload(payload: Any) -> Any:
    if isinstance(payload, dict):
        return {
            key: "***" if _is_secret_key(key) else _redact_payload(value)
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [_redact_payload(item) for item in payload]
    return payload


def _is_secret_key(key: str) -> bool:
    lowered = str(key).lower()
    return "key" in lowered or "password" in lowered or "token" in lowered
