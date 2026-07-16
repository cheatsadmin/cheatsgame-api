import hashlib
import json
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from cheatgame.shop.models import (
    Cart,
    CartLockReason,
    CartState,
    Checkout,
    CheckoutStatus,
    CommerceActorType,
    CommerceEvent,
    CommerceEventType,
    FulfillmentStatus,
    ManualReviewReason,
    OrderUserStatus,
    PaymentTransaction,
    PaymentTransactionStatus,
)


DEFAULT_CHECKOUT_TTL = timedelta(minutes=30)
DEFAULT_CHECKOUT_MAXIMUM_LIFETIME = timedelta(hours=2)

SAFE_METADATA_KEYS = frozenset(
    {
        "reason_code",
        "status",
        "previous_status",
        "new_status",
        "checkout_public_id",
        "transaction_id",
        "order_id",
        "product_id",
        "quantity",
        "expires_at",
        "provider",
        "event_source",
        "outcome",
        "request_method",
        "path",
        "details",
    }
)
SENSITIVE_KEY_FRAGMENTS = (
    "authorization",
    "password",
    "secret",
    "merchant",
    "token",
    "otp",
    "card",
    "cookie",
    "api_key",
    "apikey",
)


class CartLockConflict(Exception):
    pass


def _money_string(value):
    decimal_value = Decimal(str(value or 0))
    return format(decimal_value.quantize(Decimal("1")), "f")


def _canonical_attachment(attachment):
    return {
        "id": int(attachment["id"]),
        "type": int(attachment["type"]),
        "unit_price": _money_string(attachment["unit_price"]),
    }


def _canonical_line(line):
    attachments = sorted(
        (_canonical_attachment(item) for item in line.get("attachments", [])),
        key=lambda item: (item["type"], item["id"]),
    )
    variation_id = line.get("variation_id")
    canonical = {
        "product_id": int(line["product_id"]),
        "variation_id": int(variation_id) if variation_id is not None else None,
        "quantity": int(line["quantity"]),
        "unit_original_price": _money_string(line["unit_original_price"]),
        "unit_payable_price": _money_string(line["unit_payable_price"]),
        "attachments": attachments,
    }
    if line.get("commerce_authority") is not None:
        canonical["commerce_authority"] = str(line["commerce_authority"])
    if line.get("digital_selection") is not None:
        selection = line["digital_selection"]
        canonical["digital_selection"] = {
            "offer_id": int(selection["offer_id"]),
            "delivered_version_id": int(selection["delivered_version_id"]),
            "inventory_pool_id": int(selection["inventory_pool_id"]),
            "customer_console": str(selection["customer_console"]),
            "capacity": str(selection["capacity"]),
            "fulfillment_method": str(selection["fulfillment_method"]),
        }
    return canonical


def build_cart_fingerprint(*, lines, currency="IRT"):
    """Build a deterministic SHA-256 fingerprint from server-owned cart terms."""
    canonical_lines = sorted(
        (_canonical_line(line) for line in lines),
        key=lambda line: (line["product_id"], line["variation_id"] or 0),
    )
    payload = {"currency": currency.upper(), "lines": canonical_lines}
    canonical_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def sanitize_commerce_metadata(metadata, *, allowed_keys=SAFE_METADATA_KEYS):
    """Return a shallow allowlisted event payload with bounded safe values."""
    if not isinstance(metadata, dict):
        return {}

    clean = {}
    for key, value in metadata.items():
        normalized_key = str(key).lower()
        if key not in allowed_keys or any(fragment in normalized_key for fragment in SENSITIVE_KEY_FRAGMENTS):
            continue
        if value is None or isinstance(value, (bool, int, float)):
            clean[key] = value
        elif isinstance(value, (str, Decimal)):
            clean[key] = str(value)[:512]
        elif key == "details" and isinstance(value, dict):
            clean[key] = {
                str(nested_key)[:64]: str(nested_value)[:256]
                for nested_key, nested_value in value.items()
                if not any(fragment in str(nested_key).lower() for fragment in SENSITIVE_KEY_FRAGMENTS)
                and nested_value is not None
                and isinstance(nested_value, (str, int, float, bool, Decimal))
            }
    return clean


def calculate_checkout_expiry(
    *,
    now=None,
    ttl=DEFAULT_CHECKOUT_TTL,
    maximum_expires_at=None,
):
    now = now or timezone.now()
    proposed_expiry = now + ttl
    if maximum_expires_at is not None:
        return min(proposed_expiry, maximum_expires_at)
    return proposed_expiry


def calculate_checkout_expiry_window(*, now=None):
    now = now or timezone.now()
    maximum_expires_at = now + DEFAULT_CHECKOUT_MAXIMUM_LIFETIME
    return calculate_checkout_expiry(now=now, maximum_expires_at=maximum_expires_at), maximum_expires_at


def is_checkout_active(checkout):
    return checkout.status in Checkout.ACTIVE_STATUSES


def assert_lock_owner(*, cart, checkout):
    if cart.state != CartState.LOCKED or cart.active_checkout_id != checkout.id:
        raise CartLockConflict("Cart is not locked by this checkout.")
    if checkout.cart_id != cart.id:
        raise CartLockConflict("Checkout does not belong to this cart.")
    return True


@transaction.atomic
def lock_for_checkout(*, cart_id, checkout_id, reason=CartLockReason.CHECKOUT_IN_PROGRESS):
    cart = Cart.objects.select_for_update().get(id=cart_id)
    checkout = Checkout.objects.select_for_update().get(id=checkout_id)
    if checkout.cart_id != cart.id:
        raise CartLockConflict("Checkout does not belong to this cart.")
    if cart.state == CartState.LOCKED and cart.active_checkout_id != checkout.id:
        raise CartLockConflict("Cart is already locked by another checkout.")

    cart.state = CartState.LOCKED
    cart.lock_reason = reason
    cart.active_checkout = checkout
    cart.locked_at = timezone.now()
    cart.lock_version += 1
    cart.save(
        update_fields=[
            "state",
            "lock_reason",
            "active_checkout",
            "locked_at",
            "lock_version",
            "updated_at",
        ]
    )
    return cart


@transaction.atomic
def unlock_from_checkout(*, cart_id, checkout_id):
    cart = Cart.objects.select_for_update().get(id=cart_id)
    checkout = Checkout.objects.select_for_update().get(id=checkout_id)
    assert_lock_owner(cart=cart, checkout=checkout)

    cart.state = CartState.OPEN
    cart.lock_reason = None
    cart.active_checkout = None
    cart.locked_at = None
    cart.lock_version += 1
    cart.save(
        update_fields=[
            "state",
            "lock_reason",
            "active_checkout",
            "locked_at",
            "lock_version",
            "updated_at",
        ]
    )
    return cart


def append_commerce_event(
    *,
    checkout,
    event_type,
    actor_type=CommerceActorType.SYSTEM,
    order=None,
    payment_transaction=None,
    actor_id=None,
    idempotency_reference=None,
    request_id=None,
    correlation_id=None,
    client_ip=None,
    user_agent=None,
    metadata=None,
):
    if event_type not in CommerceEventType.values:
        raise ValidationError({"event_type": "Unsupported commerce event type."})
    if actor_type not in CommerceActorType.values:
        raise ValidationError({"actor_type": "Unsupported commerce actor type."})
    return CommerceEvent.objects.create(
        checkout=checkout,
        order=order,
        payment_transaction=payment_transaction,
        event_type=event_type,
        actor_type=actor_type,
        actor_id=actor_id,
        idempotency_reference=idempotency_reference,
        request_id=request_id,
        correlation_id=correlation_id,
        client_ip=client_ip,
        user_agent=(user_agent or "")[:512] or None,
        metadata=sanitize_commerce_metadata(metadata or {}),
    )


@transaction.atomic
def transition_to_manual_review(*, checkout_id, reason, message="", payment_transaction_id=None):
    if reason not in ManualReviewReason.values:
        raise ValidationError({"reason": "Unsupported manual-review reason."})

    checkout = Checkout.objects.select_for_update().get(id=checkout_id)
    previous_status = checkout.status
    checkout.status = CheckoutStatus.REQUIRES_MANUAL_REVIEW
    checkout.manual_review_reason = reason
    checkout.manual_review_message = message
    checkout.version += 1
    checkout.save(
        update_fields=[
            "status",
            "manual_review_reason",
            "manual_review_message",
            "version",
            "updated_at",
        ]
    )

    payment_transaction = None
    if payment_transaction_id is not None:
        payment_transaction = PaymentTransaction.objects.select_for_update().get(id=payment_transaction_id)
        if payment_transaction.checkout_id != checkout.id:
            raise ValidationError("Payment transaction does not belong to checkout.")
        payment_transaction.status = PaymentTransactionStatus.REQUIRES_MANUAL_REVIEW
        payment_transaction.manual_review_reason = reason
        payment_transaction.manual_review_message = message
        payment_transaction.save(
            update_fields=[
                "status",
                "manual_review_reason",
                "manual_review_message",
                "updated_at",
            ]
        )

    if checkout.cart_id:
        cart = Cart.objects.select_for_update().get(id=checkout.cart_id)
        if cart.active_checkout_id in (None, checkout.id):
            cart.state = CartState.LOCKED
            cart.lock_reason = CartLockReason.MANUAL_REVIEW
            cart.active_checkout = checkout
            cart.locked_at = cart.locked_at or timezone.now()
            cart.lock_version += 1
            cart.save(
                update_fields=[
                    "state",
                    "lock_reason",
                    "active_checkout",
                    "locked_at",
                    "lock_version",
                    "updated_at",
                ]
            )

    append_commerce_event(
        checkout=checkout,
        payment_transaction=payment_transaction,
        event_type=CommerceEventType.MANUAL_REVIEW_REQUIRED,
        metadata={
            "reason_code": reason,
            "previous_status": previous_status,
            "new_status": CheckoutStatus.REQUIRES_MANUAL_REVIEW,
        },
    )
    return checkout


LEGACY_USER_STATUS_TO_FULFILLMENT = {
    OrderUserStatus.NOTCOMPLETED.value: FulfillmentStatus.NOT_STARTED,
    OrderUserStatus.NOTSEEN.value: FulfillmentStatus.NOT_STARTED,
    OrderUserStatus.RECEIVED.value: FulfillmentStatus.PROCESSING,
    OrderUserStatus.SENDING.value: FulfillmentStatus.SENDING,
    OrderUserStatus.CANCLED.value: FulfillmentStatus.CANCELED,
    OrderUserStatus.FINISHED.value: FulfillmentStatus.DELIVERED,
}

FULFILLMENT_TO_LEGACY_USER_STATUS = {
    FulfillmentStatus.NOT_STARTED: OrderUserStatus.NOTCOMPLETED.value,
    FulfillmentStatus.PROCESSING: OrderUserStatus.RECEIVED.value,
    FulfillmentStatus.SENDING: OrderUserStatus.SENDING.value,
    FulfillmentStatus.DELIVERED: OrderUserStatus.FINISHED.value,
    FulfillmentStatus.CANCELED: OrderUserStatus.CANCLED.value,
}


def fulfillment_from_legacy_user_status(value):
    return LEGACY_USER_STATUS_TO_FULFILLMENT.get(value, FulfillmentStatus.NOT_STARTED)


def legacy_user_status_from_fulfillment(value):
    return FULFILLMENT_TO_LEGACY_USER_STATUS.get(value, OrderUserStatus.NOTCOMPLETED.value)
