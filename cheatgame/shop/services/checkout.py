from dataclasses import dataclass
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from cheatgame.product.models import ProductCommerceAuthority, ProductStatus, ProductType
from cheatgame.shop.models import (
    Cart,
    CartItem,
    CartLockReason,
    CartState,
    Checkout,
    CheckoutLine,
    CheckoutLineAttachment,
    CheckoutShippingSnapshot,
    CheckoutStatus,
    CommerceActorType,
    CommerceEvent,
    CommerceEventType,
    DeliverySchedule,
    DeliveryScheduleType,
    DeliverySide,
    DeliveryType,
    PaymentTransactionStatus,
    StockReservation,
    StockReservationState,
)
from cheatgame.shop.services.cart import validate_product_attachments
from cheatgame.shop.services.commerce_foundation import (
    append_commerce_event,
    build_cart_fingerprint,
    calculate_checkout_expiry,
    calculate_checkout_expiry_window,
)
from cheatgame.shop.services.delivery_schedule import is_delivery_schedule_full
from cheatgame.shop.services.pricing import (
    product_effective_unit_price,
    product_original_unit_price,
    selected_attachment_unit_total,
)
from cheatgame.users.models import Address


UNSAFE_PAYMENT_STATUSES = (
    PaymentTransactionStatus.PENDING,
    PaymentTransactionStatus.CALLBACK_RECEIVED,
    PaymentTransactionStatus.VERIFYING,
    PaymentTransactionStatus.PAID,
    PaymentTransactionStatus.REQUIRES_MANUAL_REVIEW,
)


class CheckoutServiceError(Exception):
    def __init__(self, code, message, *, details=None):
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(message)


@dataclass(frozen=True)
class CheckoutCreationResult:
    checkout: Checkout
    created: bool


def _resume_data(checkout):
    return {
        "public_id": str(checkout.public_id),
        "status": checkout.status,
        "resume_route": f"/checkout/{checkout.public_id}",
    }


def _event_once(
    *, checkout, event_type, idempotency_reference=None, actor_id=None, metadata=None, request_context=None
):
    request_context = request_context or {}
    filters = {"checkout": checkout, "event_type": event_type}
    if idempotency_reference is not None:
        filters["idempotency_reference"] = str(idempotency_reference)
    event = CommerceEvent.objects.filter(**filters).first()
    if event is not None:
        return event
    return append_commerce_event(
        checkout=checkout,
        event_type=event_type,
        actor_type=CommerceActorType.CUSTOMER if actor_id else CommerceActorType.SYSTEM,
        actor_id=actor_id,
        idempotency_reference=str(idempotency_reference) if idempotency_reference else None,
        request_id=request_context.get("request_id"),
        correlation_id=request_context.get("correlation_id"),
        client_ip=request_context.get("client_ip"),
        user_agent=request_context.get("user_agent"),
        metadata=metadata,
    )


def _load_cart_lines(cart):
    return list(
        CartItem.objects.filter(cart=cart)
        .select_related("product")
        .prefetch_related("cartitemattachment_set__attachment")
        .order_by("product_id", "id")
    )


def _commercial_lines(cart_items):
    if not cart_items:
        raise CheckoutServiceError("CART_EMPTY", "سبد خرید شما خالی است.")

    authorities = {item.commerce_authority for item in cart_items}
    if len(authorities) > 1:
        raise CheckoutServiceError(
            "MIXED_COMMERCE_AUTHORITY_NOT_SUPPORTED",
            "ترکیب محصولات استاندارد و دیجیتال در یک سبد پشتیبانی نمی‌شود.",
        )
    if authorities != {ProductCommerceAuthority.STANDARD_COMMERCE}:
        raise CheckoutServiceError(
            "DIGITAL_CART_REQUIRES_DIGITAL_CHECKOUT",
            "این سبد باید از مسیر خرید محصولات دیجیتال ادامه یابد.",
        )

    lines = []
    for item in cart_items:
        product = item.product
        attachments = [relation.attachment for relation in item.cartitemattachment_set.all()]
        if product.status != ProductStatus.PUBLISHED:
            raise CheckoutServiceError("CART_INVALID", "یکی از محصولات سبد خرید در دسترس نیست.")
        if item.quantity <= 0 or item.quantity > product.quantity:
            raise CheckoutServiceError("CART_INVALID", "تعداد یکی از محصولات سبد خرید معتبر نیست.")
        if product.order_limit is not None and item.quantity > product.order_limit:
            raise CheckoutServiceError("CART_INVALID", "تعداد یکی از محصولات بیش از حد مجاز است.")
        valid, message = validate_product_attachments(product=product, attachments=attachments)
        if not valid:
            raise CheckoutServiceError("CART_INVALID", message)

        attachment_unit_total = selected_attachment_unit_total(attachments=attachments, product=product)
        if product.product_type == ProductType.GAME:
            unit_original = attachment_unit_total
            unit_payable = attachment_unit_total
        else:
            unit_original = product_original_unit_price(product=product) + attachment_unit_total
            unit_payable = product_effective_unit_price(product=product) + attachment_unit_total

        lines.append(
            {
                "cart_item": item,
                "product": product,
                "quantity": item.quantity,
                "unit_original_price": unit_original,
                "unit_payable_price": unit_payable,
                "line_original_total": unit_original * item.quantity,
                "line_payable_total": unit_payable * item.quantity,
                "attachments": attachments,
                "fingerprint_attachments": [
                    {"id": attachment.id, "type": attachment.attachment_type, "unit_price": attachment.price}
                    for attachment in attachments
                ],
            }
        )
    return lines


def _fingerprint(lines):
    return build_cart_fingerprint(
        lines=[
            {
                "product_id": line["product"].id,
                "variation_id": None,
                "quantity": line["quantity"],
                "unit_original_price": line["unit_original_price"],
                "unit_payable_price": line["unit_payable_price"],
                "attachments": line["fingerprint_attachments"],
            }
            for line in lines
        ]
    )


def _create_snapshots(checkout, lines):
    for source in lines:
        product = source["product"]
        line = CheckoutLine.objects.create(
            checkout=checkout,
            source_cart_item_id=source["cart_item"].id,
            product_id=product.id,
            product_name=product.title,
            product_sku=product.slug or None,
            product_type=product.product_type,
            variation_id=None,
            variation_name=None,
            unit_original_price=source["unit_original_price"],
            unit_payable_price=source["unit_payable_price"],
            quantity=source["quantity"],
            line_original_total=source["line_original_total"],
            line_payable_total=source["line_payable_total"],
            snapshot={"device_model": product.device_model or ""},
            commerce_authority=ProductCommerceAuthority.STANDARD_COMMERCE,
        )
        for attachment in sorted(source["attachments"], key=lambda value: (value.attachment_type, value.id)):
            CheckoutLineAttachment.objects.create(
                checkout_line=line,
                attachment_id=attachment.id,
                attachment_type=attachment.attachment_type,
                name=attachment.title,
                unit_price=attachment.price,
                quantity_basis=source["quantity"],
                total_price=Decimal(attachment.price) * source["quantity"],
            )


@transaction.atomic
def create_or_reuse_checkout(*, user, client_checkout_uuid, request_context=None):
    request_context = request_context or {}
    cart = Cart.objects.select_for_update().filter(user=user).first()
    if cart is None:
        raise CheckoutServiceError("CART_EMPTY", "سبد خرید شما خالی است.")

    lines = _commercial_lines(_load_cart_lines(cart))
    fingerprint = _fingerprint(lines)
    existing = Checkout.objects.filter(user=user, client_checkout_uuid=client_checkout_uuid).first()
    if existing is not None:
        if existing.cart_fingerprint != fingerprint:
            raise CheckoutServiceError(
                "IDEMPOTENCY_CONFLICT",
                "این درخواست قبلاً با محتوای دیگری ثبت شده است.",
                details=_resume_data(existing),
            )
        _event_once(
            checkout=existing,
            event_type=CommerceEventType.CHECKOUT_DRAFT_REUSED,
            idempotency_reference=client_checkout_uuid,
            actor_id=user.id,
            request_context=request_context,
            metadata={"checkout_public_id": existing.public_id, "outcome": "reused"},
        )
        return CheckoutCreationResult(existing, False)

    if cart.state == CartState.LOCKED:
        active = cart.active_checkout
        details = _resume_data(active) if active else {}
        raise CheckoutServiceError("CART_LOCKED", "سبد خرید در یک فرایند پرداخت فعال است.", details=details)

    now = timezone.now()
    expires_at, maximum_expires_at = calculate_checkout_expiry_window(now=now)
    checkout = Checkout.objects.create(
        user=user,
        cart=cart,
        client_checkout_uuid=client_checkout_uuid,
        cart_fingerprint=fingerprint,
        expires_at=expires_at,
        maximum_expires_at=maximum_expires_at,
        locked_at=now,
    )
    _create_snapshots(checkout, lines)
    cart.state = CartState.LOCKED
    cart.lock_reason = CartLockReason.CHECKOUT_IN_PROGRESS
    cart.active_checkout = checkout
    cart.locked_at = now
    cart.lock_version += 1
    cart.save(update_fields=["state", "lock_reason", "active_checkout", "locked_at", "lock_version", "updated_at"])
    _event_once(
        checkout=checkout,
        event_type=CommerceEventType.CHECKOUT_DRAFT_CREATED,
        idempotency_reference=client_checkout_uuid,
        actor_id=user.id,
        request_context=request_context,
        metadata={"checkout_public_id": checkout.public_id, "outcome": "created"},
    )
    _event_once(
        checkout=checkout,
        event_type=CommerceEventType.CART_LOCKED,
        actor_id=user.id,
        request_context=request_context,
        metadata={"reason_code": CartLockReason.CHECKOUT_IN_PROGRESS},
    )
    return CheckoutCreationResult(checkout, True)


def get_active_checkout(*, user):
    return (
        Checkout.objects.filter(user=user, status__in=Checkout.ACTIVE_STATUSES)
        .prefetch_related("lines__attachments", "payment_transactions")
        .select_related("shipping_snapshot")
        .order_by("-created_at")
        .first()
    )


def get_owned_checkout(*, user, public_id, for_update=False):
    queryset = Checkout.objects.filter(user=user, public_id=public_id)
    if for_update:
        queryset = queryset.select_for_update()
    checkout = queryset.first()
    if checkout is None:
        raise CheckoutServiceError("CHECKOUT_NOT_FOUND", "فرایند خرید یافت نشد.")
    return checkout


def _lock_owned_checkout_and_cart(*, user, public_id):
    identity = Checkout.objects.filter(user=user, public_id=public_id).values("id", "cart_id").first()
    if identity is None:
        raise CheckoutServiceError("CHECKOUT_NOT_FOUND", "فرایند خرید یافت نشد.")
    cart = None
    if identity["cart_id"] is not None:
        cart = Cart.objects.select_for_update().get(id=identity["cart_id"])
    checkout = Checkout.objects.select_for_update().get(id=identity["id"], user=user)
    return checkout, cart


def _require_editable(checkout):
    if checkout.status != CheckoutStatus.CHECKOUT_DRAFT:
        raise CheckoutServiceError("CHECKOUT_NOT_EDITABLE", "این فرایند خرید دیگر قابل ویرایش نیست.")


def _require_standard_checkout(checkout):
    authorities = set(checkout.lines.values_list("commerce_authority", flat=True))
    if authorities not in (set(), {ProductCommerceAuthority.STANDARD_COMMERCE}):
        raise CheckoutServiceError(
            "DIGITAL_CHECKOUT_STANDARD_MUTATION_NOT_SUPPORTED",
            "این عملیات برای فرایند خرید دیجیتال قابل استفاده نیست.",
        )


def _touch(checkout):
    checkout.version += 1
    checkout.expires_at = calculate_checkout_expiry(maximum_expires_at=checkout.maximum_expires_at)
    checkout.save(update_fields=["version", "expires_at", "updated_at"])


@transaction.atomic
def select_checkout_address(*, user, public_id, address_id):
    checkout, _ = _lock_owned_checkout_and_cart(user=user, public_id=public_id)
    _require_standard_checkout(checkout)
    _require_editable(checkout)
    address = Address.objects.filter(id=address_id, user=user).first()
    if address is None:
        raise CheckoutServiceError("ADDRESS_NOT_FOUND", "آدرس انتخاب‌شده معتبر نیست.")
    values = {
            "address_id": address.id,
            "recipient_name": f"{user.firstname} {user.lastname}".strip(),
            "recipient_phone": user.phone_number,
            "province": address.province,
            "city": address.city,
            "full_address": address.address_detail,
            "postal_code": address.postal_code,
            "delivery_method_id": None,
            "delivery_method_name": "",
            "delivery_cost": 0,
            "is_pricing_finalized": False,
            "schedule_id": None,
            "schedule_start": None,
            "schedule_end": None,
            "snapshot": {},
    }
    snapshot = CheckoutShippingSnapshot.objects.select_for_update().filter(checkout=checkout).first()
    if snapshot is not None and all(getattr(snapshot, key) == value for key, value in values.items() if key not in {
        "delivery_method_id", "delivery_method_name", "delivery_cost", "is_pricing_finalized",
        "schedule_id", "schedule_start", "schedule_end", "snapshot"
    }):
        return checkout, snapshot
    if snapshot is None:
        snapshot = CheckoutShippingSnapshot.objects.create(checkout=checkout, **values)
    else:
        for key, value in values.items():
            setattr(snapshot, key, value)
        snapshot.save()
    _touch(checkout)
    append_commerce_event(
        checkout=checkout,
        event_type=CommerceEventType.ADDRESS_SELECTED,
        actor_type=CommerceActorType.CUSTOMER,
        actor_id=user.id,
        metadata={"outcome": "selected"},
    )
    return checkout, snapshot


@transaction.atomic
def select_checkout_shipping(*, user, public_id, delivery_method_id):
    checkout, _ = _lock_owned_checkout_and_cart(user=user, public_id=public_id)
    _require_standard_checkout(checkout)
    _require_editable(checkout)
    snapshot = CheckoutShippingSnapshot.objects.select_for_update().filter(checkout=checkout).first()
    if snapshot is None or snapshot.address_id is None:
        raise CheckoutServiceError("ADDRESS_REQUIRED", "ابتدا آدرس ارسال را انتخاب کنید.")
    method = DeliveryType.objects.filter(id=delivery_method_id, side=DeliverySide.SENDTOUSER.value).first()
    if method is None:
        raise CheckoutServiceError("SHIPPING_METHOD_INVALID", "روش ارسال انتخاب‌شده معتبر نیست.")
    if snapshot.delivery_method_id == method.id:
        return checkout, snapshot
    snapshot.delivery_method_id = method.id
    snapshot.delivery_method_name = method.name
    snapshot.delivery_cost = 0
    snapshot.is_pricing_finalized = False
    snapshot.schedule_id = None
    snapshot.schedule_start = None
    snapshot.schedule_end = None
    snapshot.snapshot = {"delivery_type": method.delivery_type, "pricing_state": "not_finalized"}
    snapshot.save()
    _touch(checkout)
    append_commerce_event(
        checkout=checkout,
        event_type=CommerceEventType.SHIPPING_SELECTED,
        actor_type=CommerceActorType.CUSTOMER,
        actor_id=user.id,
        metadata={"outcome": "selected"},
    )
    return checkout, snapshot


@transaction.atomic
def select_checkout_schedule(*, user, public_id, schedule_id):
    checkout, _ = _lock_owned_checkout_and_cart(user=user, public_id=public_id)
    _require_standard_checkout(checkout)
    _require_editable(checkout)
    snapshot = CheckoutShippingSnapshot.objects.select_for_update().filter(checkout=checkout).first()
    if snapshot is None or snapshot.delivery_method_id is None:
        raise CheckoutServiceError("SHIPPING_METHOD_REQUIRED", "ابتدا روش ارسال را انتخاب کنید.")
    schedule = DeliverySchedule.objects.filter(id=schedule_id, type=DeliveryScheduleType.ORDER.value).first()
    if schedule is None or schedule.end <= timezone.now() or is_delivery_schedule_full(schedule=schedule):
        raise CheckoutServiceError("SCHEDULE_INVALID", "زمان ارسال انتخاب‌شده معتبر یا در دسترس نیست.")
    if snapshot.schedule_id == schedule.id:
        return checkout, snapshot
    snapshot.schedule_id = schedule.id
    snapshot.schedule_start = schedule.start
    snapshot.schedule_end = schedule.end
    snapshot.save()
    _touch(checkout)
    append_commerce_event(
        checkout=checkout,
        event_type=CommerceEventType.SCHEDULE_SELECTED,
        actor_type=CommerceActorType.CUSTOMER,
        actor_id=user.id,
        metadata={"outcome": "selected"},
    )
    return checkout, snapshot


@transaction.atomic
def cancel_checkout(*, user, public_id):
    checkout, cart = _lock_owned_checkout_and_cart(user=user, public_id=public_id)
    if checkout.status == CheckoutStatus.CANCELED:
        return checkout
    if checkout.status in (CheckoutStatus.PAID, CheckoutStatus.REQUIRES_MANUAL_REVIEW, CheckoutStatus.EXPIRED):
        raise CheckoutServiceError("CHECKOUT_NOT_CANCELABLE", "این فرایند خرید قابل لغو نیست.")
    if checkout.orders.filter(financial_payment__isnull=False).exists():
        raise CheckoutServiceError(
            "CHECKOUT_NOT_CANCELABLE",
            "سفارش ثبت شده است و لغو باید از مرز کنترل‌شده سفارش انجام شود.",
        )
    if checkout.payment_transactions.filter(status__in=UNSAFE_PAYMENT_STATUSES).exists():
        raise CheckoutServiceError("CHECKOUT_NOT_CANCELABLE", "وضعیت پرداخت نیازمند بررسی است.")
    line_authorities = set(checkout.lines.values_list("commerce_authority", flat=True))
    if len(line_authorities) > 1:
        raise CheckoutServiceError("CHECKOUT_AUTHORITY_INCOHERENT", "ساختار مرجع خرید ناسازگار است.")
    is_digital = line_authorities == {ProductCommerceAuthority.DIGITAL_PRODUCTS}
    if is_digital:
        from cheatgame.digital_products.models import (
            DigitalInventoryReservation,
            DigitalInventoryReservationState,
        )

        line_count = checkout.lines.count()
        if (
            checkout.lines.filter(digital_snapshot__isnull=True).exists()
            or DigitalInventoryReservation.objects.filter(checkout=checkout).count() != line_count
            or StockReservation.objects.filter(checkout=checkout).exists()
        ):
            raise CheckoutServiceError("CHECKOUT_AUTHORITY_INCOHERENT", "ساختار مرجع خرید ناسازگار است.")
        if DigitalInventoryReservation.objects.filter(
            checkout=checkout, state=DigitalInventoryReservationState.HELD_FOR_REVIEW
        ).exists():
            raise CheckoutServiceError("CHECKOUT_NOT_CANCELABLE", "وضعیت موجودی نیازمند بررسی است.")
    elif line_authorities not in (set(), {ProductCommerceAuthority.STANDARD_COMMERCE}):
        raise CheckoutServiceError("CHECKOUT_AUTHORITY_INCOHERENT", "ساختار مرجع خرید ناسازگار است.")
    elif (
        checkout.lines.filter(digital_snapshot__isnull=False).exists()
        or checkout.digital_inventory_reservations.exists()
    ):
        raise CheckoutServiceError("CHECKOUT_AUTHORITY_INCOHERENT", "ساختار مرجع خرید ناسازگار است.")
    now = timezone.now()
    checkout.status = CheckoutStatus.CANCELED
    checkout.canceled_at = now
    checkout.version += 1
    checkout.save(update_fields=["status", "canceled_at", "version", "updated_at"])
    if is_digital:
        DigitalInventoryReservation.objects.select_for_update().filter(
            checkout=checkout, state=DigitalInventoryReservationState.ACTIVE
        ).update(
            state=DigitalInventoryReservationState.RELEASED,
            state_changed_at=now,
            resolution_reason="checkout_canceled",
            updated_at=now,
        )
        _event_once(
            checkout=checkout,
            event_type=CommerceEventType.STOCK_RESERVATION_RELEASED,
            actor_id=user.id,
            metadata={"reason_code": "checkout_canceled"},
        )
    else:
        StockReservation.objects.filter(checkout=checkout, state=StockReservationState.ACTIVE).update(
            state=StockReservationState.RELEASED, updated_at=now
        )
    _event_once(checkout=checkout, event_type=CommerceEventType.CHECKOUT_CANCELED, actor_id=user.id)
    if cart is not None:
        if cart.active_checkout_id == checkout.id:
            cart.state = CartState.OPEN
            cart.lock_reason = None
            cart.active_checkout = None
            cart.locked_at = None
            cart.lock_version += 1
            cart.save(update_fields=["state", "lock_reason", "active_checkout", "locked_at", "lock_version", "updated_at"])
            _event_once(checkout=checkout, event_type=CommerceEventType.CART_UNLOCKED, actor_id=user.id)
    return checkout


def checkout_totals(checkout):
    lines = list(checkout.lines.all())
    original = sum((line.line_original_total for line in lines), Decimal("0"))
    payable = sum((line.line_payable_total for line in lines), Decimal("0"))
    shipping = getattr(getattr(checkout, "shipping_snapshot", None), "delivery_cost", Decimal("0"))
    return {
        "items_original": original,
        "items_payable": payable,
        "shipping": shipping,
        "payable": payable + shipping,
        "is_pricing_finalized": bool(
            getattr(getattr(checkout, "shipping_snapshot", None), "is_pricing_finalized", False)
        ),
    }
