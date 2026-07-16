from uuid import UUID, uuid5

from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from cheatgame.digital_products.models import (
    CapacityDisclosure,
    CompatibilityDisclosure,
    DigitalCheckoutLineSnapshot,
    DigitalInventoryReservation,
    DigitalInventoryReservationState,
    DigitalOffer,
    DigitalOfferCapacity,
    DigitalOfferSaleState,
    InventoryPool,
    InventoryPoolStatus,
)
from cheatgame.digital_products.services import (
    DigitalCartLockedError,
    DigitalCartStaleError,
    DigitalCheckoutIdempotencyError,
    DigitalOfferUnavailableError,
    DigitalProductsConflictError,
    EmptyDigitalCartError,
    InsufficientDigitalAvailabilityError,
    MixedCommerceAuthorityError,
    StandardCartNotSupportedError,
)
from cheatgame.product.models import NativeConsole, ProductCommerceAuthority
from cheatgame.shop.models import (
    Cart,
    CartItem,
    CartLockReason,
    CartState,
    Checkout,
    CheckoutLine,
    CheckoutStatus,
    CommerceActorType,
    CommerceEventType,
)
from cheatgame.shop.services.commerce_foundation import (
    append_commerce_event,
    build_cart_fingerprint,
    calculate_checkout_expiry_window,
)
from cheatgame.users.models import UserTypes


def _require_customer(actor):
    if (
        actor is None
        or not getattr(actor, "is_authenticated", False)
        or not getattr(actor, "is_active", False)
        or actor.user_type != UserTypes.CUSTOMER
        or not actor.phone_verified
    ):
        raise PermissionDenied("An active verified Customer is required.")


def _resume(checkout):
    return {
        "public_id": str(checkout.public_id),
        "status": checkout.status,
        "resume_route": f"/checkout/{checkout.public_id}",
    }


def _event_once(checkout, event_type, *, actor_id, reference, metadata=None):
    event = checkout.events.filter(event_type=event_type, idempotency_reference=str(reference)).first()
    if event:
        return event
    return append_commerce_event(
        checkout=checkout,
        event_type=event_type,
        actor_type=CommerceActorType.CUSTOMER,
        actor_id=actor_id,
        idempotency_reference=str(reference),
        metadata=metadata or {},
    )


def _compatibility(version, customer_console):
    if version.native_console == customer_console:
        return CompatibilityDisclosure.NATIVE_VERSION_V1
    if version.native_console == NativeConsole.PS4 and customer_console == NativeConsole.PS5:
        return CompatibilityDisclosure.PS4_ON_PS5_BACKWARD_COMPATIBLE_V1
    raise DigitalOfferUnavailableError("Digital Offer console compatibility is invalid.")


def _capacity_disclosure(capacity):
    return {
        DigitalOfferCapacity.CAPACITY_1: CapacityDisclosure.CAPACITY_1_OFFLINE_IN_STORE_V1,
        DigitalOfferCapacity.CAPACITY_2: CapacityDisclosure.CAPACITY_2_ONLINE_OFFLINE_FLEXIBLE_V1,
        DigitalOfferCapacity.CAPACITY_3: CapacityDisclosure.CAPACITY_3_ONLINE_FLEXIBLE_V1,
    }[capacity]


def _load_terms(cart):
    items = list(
        CartItem.objects.select_for_update(of=("self",))
        .filter(cart=cart)
        .select_related(
            "product",
            "digital_selection__offer__delivered_version",
            "digital_selection__offer__inventory_pool",
        )
        .prefetch_related("cartitemattachment_set")
        .order_by("pk")
    )
    if not items:
        raise EmptyDigitalCartError("Digital Cart is empty.")
    authorities = {item.commerce_authority for item in items}
    if len(authorities) > 1:
        raise MixedCommerceAuthorityError("Mixed Standard and Digital Carts are not supported.")
    if authorities != {ProductCommerceAuthority.DIGITAL_PRODUCTS}:
        raise StandardCartNotSupportedError("Standard Cart requires Standard Checkout.")
    offer_ids = []
    for item in items:
        try:
            offer_ids.append(item.digital_selection.offer_id)
        except ObjectDoesNotExist as exc:
            raise DigitalCartStaleError("Digital Cart selection is incomplete.") from exc
    locked_offers = {
        offer.id: offer
        for offer in DigitalOffer.objects.select_for_update()
        .filter(pk__in=offer_ids)
        .select_related("delivered_version__product", "inventory_pool")
        .order_by("pk")
    }
    if len(locked_offers) != len(set(offer_ids)):
        raise DigitalCartStaleError("Digital Cart selection is incomplete.")
    terms = []
    for item in items:
        try:
            selection = item.digital_selection
            offer = locked_offers[selection.offer_id]
        except (AttributeError, ObjectDoesNotExist) as exc:
            raise DigitalCartStaleError("Digital Cart selection is incomplete.") from exc
        if item.product_id != offer.delivered_version.product_id or item.quantity != 1:
            raise DigitalCartStaleError("Digital Cart selection is incoherent.")
        if item.cartitemattachment_set.exists():
            raise DigitalCartStaleError("Digital Cart cannot contain Standard options.")
        if (
            item.product.commerce_authority != ProductCommerceAuthority.DIGITAL_PRODUCTS
            or offer.sale_state != DigitalOfferSaleState.ACTIVE
            or not offer.delivered_version.is_active
            or offer.inventory_pool.status != InventoryPoolStatus.ENABLED
        ):
            raise DigitalOfferUnavailableError("Digital Offer is unavailable.")
        try:
            selection.full_clean()
            offer.full_clean()
        except ValidationError as exc:
            raise DigitalCartStaleError("Digital Cart selection is invalid.") from exc
        terms.append((item, selection, offer))
    return terms


def _fingerprint(terms):
    return build_cart_fingerprint(
        lines=[
            {
                "product_id": item.product_id,
                "variation_id": None,
                "quantity": 1,
                "unit_original_price": offer.price,
                "unit_payable_price": offer.price,
                "attachments": [],
                "commerce_authority": ProductCommerceAuthority.DIGITAL_PRODUCTS,
                "digital_selection": {
                    "offer_id": offer.id,
                    "delivered_version_id": offer.delivered_version_id,
                    "inventory_pool_id": offer.inventory_pool_id,
                    "customer_console": offer.customer_console,
                    "capacity": offer.capacity,
                    "fulfillment_method": selection.fulfillment_method,
                },
            }
            for item, selection, offer in terms
        ]
    )


def _assert_reusable(checkout, cart, terms, pool_ids):
    if checkout.status != CheckoutStatus.CHECKOUT_DRAFT or checkout.expires_at <= timezone.now():
        raise DigitalProductsConflictError("Checkout is no longer reusable.")
    if cart.state != CartState.LOCKED or cart.active_checkout_id != checkout.id:
        raise DigitalCartLockedError("Cart lock does not match Checkout ownership.")
    line_ids = list(
        CheckoutLine.objects.select_for_update()
        .filter(checkout=checkout)
        .order_by("source_cart_item_id")
        .values_list("source_cart_item_id", flat=True)
    )
    if line_ids != sorted(item.id for item, _, _ in terms):
        raise DigitalCartStaleError("Checkout lines no longer match the Cart.")
    if checkout.lines.filter(commerce_authority=ProductCommerceAuthority.DIGITAL_PRODUCTS).count() != len(terms):
        raise DigitalCartStaleError("Checkout authority is incoherent.")
    list(InventoryPool.objects.select_for_update().filter(pk__in=pool_ids).order_by("pk"))
    reservations = DigitalInventoryReservation.objects.select_for_update().filter(checkout=checkout)
    if reservations.count() != len(terms) or reservations.exclude(
        state=DigitalInventoryReservationState.ACTIVE
    ).exists():
        raise DigitalCartStaleError("Checkout reservations are incomplete.")


@transaction.atomic
def prepare_digital_checkout(*, actor, client_checkout_uuid):
    _require_customer(actor)
    client_checkout_uuid = UUID(str(client_checkout_uuid))
    cart = Cart.objects.select_for_update().filter(user=actor).first()
    if cart is None:
        raise EmptyDigitalCartError("Digital Cart is empty.")
    terms = _load_terms(cart)
    pool_ids = sorted({offer.inventory_pool_id for _, _, offer in terms})
    fingerprint = _fingerprint(terms)

    existing = Checkout.objects.select_for_update().filter(
        user=actor, client_checkout_uuid=client_checkout_uuid
    ).first()
    if existing:
        if existing.cart_fingerprint != fingerprint:
            raise DigitalCheckoutIdempotencyError(
                "Checkout UUID was already used for different terms.", details=_resume(existing)
            )
        _assert_reusable(existing, cart, terms, pool_ids)
        _event_once(
            existing,
            CommerceEventType.CHECKOUT_DRAFT_REUSED,
            actor_id=actor.id,
            reference=client_checkout_uuid,
            metadata={"outcome": "reused"},
        )
        return existing, False

    if cart.state != CartState.OPEN or cart.active_checkout_id is not None:
        active = cart.active_checkout
        raise DigitalCartLockedError(
            "Cart is locked by an active Checkout.", details=_resume(active) if active else {}
        )
    if Checkout.objects.filter(cart=cart, status__in=Checkout.ACTIVE_STATUSES).exists():
        raise DigitalCartLockedError("Cart has an inconsistent active Checkout.")

    now = timezone.now()
    expires_at, maximum_expires_at = calculate_checkout_expiry_window(now=now)
    checkout = Checkout.objects.create(
        user=actor,
        cart=cart,
        client_checkout_uuid=client_checkout_uuid,
        cart_fingerprint=fingerprint,
        expires_at=expires_at,
        maximum_expires_at=maximum_expires_at,
        locked_at=now,
    )
    _event_once(
        checkout,
        CommerceEventType.CHECKOUT_DRAFT_CREATED,
        actor_id=actor.id,
        reference=client_checkout_uuid,
        metadata={"outcome": "created", "checkout_public_id": checkout.public_id},
    )

    lines = []
    for item, selection, offer in terms:
        line = CheckoutLine.objects.create(
            checkout=checkout,
            source_cart_item_id=item.id,
            product_id=item.product_id,
            product_name=item.product.title,
            product_sku=item.product.slug or None,
            product_type=item.product.product_type,
            unit_original_price=offer.price,
            unit_payable_price=offer.price,
            quantity=1,
            line_original_total=offer.price,
            line_payable_total=offer.price,
            snapshot={"commerce_authority": ProductCommerceAuthority.DIGITAL_PRODUCTS},
            commerce_authority=ProductCommerceAuthority.DIGITAL_PRODUCTS,
        )
        DigitalCheckoutLineSnapshot.objects.create(
            checkout_line=line,
            offer=offer,
            inventory_pool=offer.inventory_pool,
            delivered_version=offer.delivered_version,
            product_id=item.product_id,
            product_name=item.product.title,
            customer_console=offer.customer_console,
            capacity=offer.capacity,
            fulfillment_method=selection.fulfillment_method,
            version_label=offer.delivered_version.get_native_console_display(),
            native_console=offer.delivered_version.native_console,
            compatibility_disclosure=_compatibility(offer.delivered_version, offer.customer_console),
            capacity_disclosure=_capacity_disclosure(offer.capacity),
            unit_price=offer.price,
            quantity=1,
            line_total=offer.price,
            safe_display_metadata={},
        )
        lines.append((line, offer.inventory_pool_id))

    list(InventoryPool.objects.select_for_update().filter(pk__in=pool_ids).order_by("pk"))
    held = dict(
        DigitalInventoryReservation.objects.filter(
            inventory_pool_id__in=pool_ids,
            state__in=(DigitalInventoryReservationState.ACTIVE, DigitalInventoryReservationState.HELD_FOR_REVIEW),
        )
        .values_list("inventory_pool_id")
        .annotate(total=Sum("quantity"))
    )
    pools = {pool.id: pool for pool in InventoryPool.objects.filter(pk__in=pool_ids)}
    remaining = {pool_id: pools[pool_id].sellable_quantity - held.get(pool_id, 0) for pool_id in pool_ids}
    for line, pool_id in lines:
        if remaining[pool_id] < 1:
            raise InsufficientDigitalAvailabilityError("Digital availability is insufficient.")
        remaining[pool_id] -= 1
        DigitalInventoryReservation.objects.create(
            checkout=checkout,
            checkout_line=line,
            inventory_pool_id=pool_id,
            quantity=1,
            expires_at=checkout.expires_at,
            idempotency_key=uuid5(checkout.public_id, f"digital-reservation:{line.id}"),
        )
    _event_once(
        checkout,
        CommerceEventType.STOCK_RESERVATION_CREATED,
        actor_id=actor.id,
        reference=client_checkout_uuid,
        metadata={"quantity": len(lines), "outcome": "reserved"},
    )
    cart.state = CartState.LOCKED
    cart.lock_reason = CartLockReason.CHECKOUT_IN_PROGRESS
    cart.active_checkout = checkout
    cart.locked_at = now
    cart.lock_version += 1
    cart.save(update_fields=["state", "lock_reason", "active_checkout", "locked_at", "lock_version", "updated_at"])
    _event_once(
        checkout,
        CommerceEventType.CART_LOCKED,
        actor_id=actor.id,
        reference=client_checkout_uuid,
        metadata={"reason_code": CartLockReason.CHECKOUT_IN_PROGRESS},
    )
    return checkout, True
