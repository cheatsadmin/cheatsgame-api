from dataclasses import dataclass

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


@dataclass(frozen=True)
class DigitalPaymentHoldPolicy:
    provider_pending_seconds: int
    verification_pending_seconds: int
    review_hold_seconds: int
    abandonment_seconds: int
    nominal_expiry_renewal_seconds: int
    finalization_retry_seconds: int
    finalization_retry_max_seconds: int


def _positive_setting(name, default):
    value = int(getattr(settings, name, default))
    if value <= 0:
        raise ImproperlyConfigured(f"{name} must be a positive number of seconds.")
    return value


def get_digital_payment_hold_policy():
    policy = DigitalPaymentHoldPolicy(
        provider_pending_seconds=_positive_setting(
            "DIGITAL_PAYMENT_PROVIDER_PENDING_HOLD_SECONDS", 1800
        ),
        verification_pending_seconds=_positive_setting(
            "DIGITAL_PAYMENT_VERIFICATION_PENDING_HOLD_SECONDS", 1800
        ),
        review_hold_seconds=_positive_setting(
            "DIGITAL_PAYMENT_REVIEW_HOLD_SECONDS", 86400
        ),
        abandonment_seconds=_positive_setting(
            "DIGITAL_PAYMENT_ABANDONMENT_SECONDS", 86400
        ),
        nominal_expiry_renewal_seconds=_positive_setting(
            "DIGITAL_PAYMENT_NOMINAL_EXPIRY_RENEWAL_SECONDS", 1800
        ),
        finalization_retry_seconds=_positive_setting(
            "DIGITAL_PAYMENT_FINALIZATION_RETRY_SECONDS", 300
        ),
        finalization_retry_max_seconds=_positive_setting(
            "DIGITAL_PAYMENT_FINALIZATION_RETRY_MAX_SECONDS", 3600
        ),
    )
    if policy.finalization_retry_max_seconds < policy.finalization_retry_seconds:
        raise ImproperlyConfigured(
            "DIGITAL_PAYMENT_FINALIZATION_RETRY_MAX_SECONDS cannot be shorter than the base delay."
        )
    return policy
