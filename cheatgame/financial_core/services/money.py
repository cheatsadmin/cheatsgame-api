import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal

from django.core.exceptions import ValidationError

from cheatgame.financial_core.models import CANONICAL_CURRENCY, MoneyUnit


LEGACY_IRT_BRIDGE_VERSION = "legacy-irt-to-irr-v1"
CANONICAL_IRR_BRIDGE_VERSION = "canonical-irr-pass-through-v1"


@dataclass(frozen=True)
class NormalizedObligationMoney:
    source_amount: Decimal
    source_unit: str
    canonical_amount: Decimal
    canonical_currency: str
    bridge_version: str
    evidence_fingerprint: str


@dataclass(frozen=True)
class ProviderMoneyRepresentation:
    canonical_amount: Decimal
    canonical_currency: str
    provider_amount: Decimal
    provider_unit: str
    conversion_policy_version: str


def exact_integer_money(value, *, field, positive=True):
    if isinstance(value, (float, bool)) or not isinstance(value, (int, Decimal)):
        raise ValidationError({field: "Money must be supplied as an integer or exact Decimal."})
    amount = Decimal(value)
    if amount != amount.to_integral_value():
        raise ValidationError({field: "Money must use an exact integer unit."})
    if positive and amount <= 0:
        raise ValidationError({field: "Money must be greater than zero."})
    if not positive and amount < 0:
        raise ValidationError({field: "Money cannot be negative."})
    return amount


def _fingerprint(payload):
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def normalize_obligation_money(
    *,
    source_amount,
    source_unit,
    source_model,
    source_object_id,
    source_field,
):
    amount = exact_integer_money(source_amount, field="source_amount")
    if not source_unit:
        raise ValidationError({"source_unit": "An explicit source money unit is required."})
    unit = str(source_unit).upper()
    if unit == MoneyUnit.IRT:
        canonical = amount * Decimal("10")
        bridge_version = LEGACY_IRT_BRIDGE_VERSION
    elif unit == MoneyUnit.IRR:
        canonical = amount
        bridge_version = CANONICAL_IRR_BRIDGE_VERSION
    else:
        raise ValidationError({"source_unit": "Only explicit IRT or canonical IRR is supported."})
    payload = {
        "source_model": str(source_model),
        "source_object_id": str(source_object_id),
        "source_field": str(source_field),
        "source_amount": str(amount),
        "source_unit": unit,
        "canonical_amount": str(canonical),
        "canonical_currency": CANONICAL_CURRENCY,
        "bridge_version": bridge_version,
    }
    return NormalizedObligationMoney(
        source_amount=amount,
        source_unit=unit,
        canonical_amount=canonical,
        canonical_currency=CANONICAL_CURRENCY,
        bridge_version=bridge_version,
        evidence_fingerprint=_fingerprint(payload),
    )


def represent_provider_money(*, canonical_amount, capability_version):
    amount = exact_integer_money(canonical_amount, field="canonical_amount")
    unit = capability_version.provider_unit
    if unit == MoneyUnit.IRR:
        provider_amount = amount
    elif unit == MoneyUnit.IRT:
        if amount % Decimal("10") != 0:
            raise ValidationError(
                {"canonical_amount": "Canonical IRR is not exactly representable in provider IRT."}
            )
        provider_amount = amount / Decimal("10")
    else:
        raise ValidationError({"provider_unit": "Unsupported provider money unit."})
    return ProviderMoneyRepresentation(
        canonical_amount=amount,
        canonical_currency=CANONICAL_CURRENCY,
        provider_amount=provider_amount,
        provider_unit=unit,
        conversion_policy_version=capability_version.conversion_policy_version,
    )
