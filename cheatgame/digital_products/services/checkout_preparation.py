from uuid import UUID, uuid5

from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from cheatgame.digital_products.models import (
    CapacityDisclosure,
    CompatibilityDisclosure,
    DigitalCartSelection,
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
    DigitalCheckoutExpiredError,
    DigitalCheckoutIntegrityError,
    DigitalCheckoutIdempotencyError,
    DigitalOfferUnavailableError,
    DigitalProductsConflictError,
    EmptyDigitalCartError,
    InsufficientDigitalAvailabilityError,
    MixedCommerceAuthorityError,
    StandardCartNotSupportedError,
)
from cheatgame.product.models import (
    DeliveredVersion,
    NativeConsole,
    Product,
    ProductCommerceAuthority,
    ProductStatus,
    ProductType,
)
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


COMMERCIAL_SNAPSHOT_REVISION = 1


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
    items = list(CartItem.objects.select_for_update().filter(cart=cart).order_by("pk"))
    if not items:
        raise EmptyDigitalCartError("Digital Cart is empty.")
    authorities = {item.commerce_authority for item in items}
    if len(authorities) > 1:
        raise MixedCommerceAuthorityError("Mixed Standard and Digital Carts are not supported.")
    if authorities != {ProductCommerceAuthority.DIGITAL_PRODUCTS}:
        raise StandardCartNotSupportedError("Standard Cart requires Standard Checkout.")
    product_ids = sorted({item.product_id for item in items})
    locked_products = {
        product.pk: product
        for product in Product.objects.select_for_update().filter(pk__in=product_ids).order_by("pk")
    }
    if len(locked_products) != len(product_ids):
        raise DigitalCartStaleError("Digital Cart Product identity is incomplete.")

    selections = {
        selection.cart_item_id: selection
        for selection in DigitalCartSelection.objects.select_for_update()
        .filter(cart_item_id__in=[item.pk for item in items])
        .order_by("cart_item_id")
    }
    if len(selections) != len(items):
        raise DigitalCartStaleError("Digital Cart selection is incomplete.")
    offer_ids = sorted({selection.offer_id for selection in selections.values()})
    locked_offers = {
        offer.id: offer
        for offer in DigitalOffer.objects.select_for_update()
        .filter(pk__in=offer_ids)
        .select_related("delivered_version__product", "inventory_pool")
        .order_by("pk")
    }
    if len(locked_offers) != len(set(offer_ids)):
        raise DigitalCartStaleError("Digital Cart selection is incomplete.")

    version_ids = sorted({offer.delivered_version_id for offer in locked_offers.values()})
    locked_versions = {
        version.pk: version
        for version in DeliveredVersion.objects.select_for_update()
        .filter(pk__in=version_ids)
        .order_by("pk")
    }
    if len(locked_versions) != len(version_ids):
        raise DigitalCartStaleError("Digital Cart Delivered Version is incomplete.")

    pool_ids = sorted({offer.inventory_pool_id for offer in locked_offers.values()})
    locked_pools = {
        pool.pk: pool
        for pool in InventoryPool.objects.select_for_update().filter(pk__in=pool_ids).order_by("pk")
    }
    if len(locked_pools) != len(pool_ids):
        raise DigitalCartStaleError("Digital Cart inventory authority is incomplete.")

    terms = []
    for item in items:
        try:
            selection = selections[item.pk]
            offer = locked_offers[selection.offer_id]
            product = locked_products[item.product_id]
            delivered_version = locked_versions[offer.delivered_version_id]
            pool = locked_pools[offer.inventory_pool_id]
        except (AttributeError, KeyError, ObjectDoesNotExist) as exc:
            raise DigitalCartStaleError("Digital Cart selection is incomplete.") from exc
        offer.delivered_version = delivered_version
        offer.inventory_pool = pool
        delivered_version.product = product
        item.product = product
        selection.offer = offer
        if item.product_id != delivered_version.product_id or item.quantity != 1:
            raise DigitalCartStaleError("Digital Cart selection is incoherent.")
        if item.price != offer.price:
            raise DigitalCartStaleError("Digital Cart price is stale.")
        if offer.updated_at > item.created_at or delivered_version.updated_at > item.created_at:
            raise DigitalCartStaleError("Digital Cart selection terms changed after selection.")
        if item.cartitemattachment_set.exists():
            raise DigitalCartStaleError("Digital Cart cannot contain Standard options.")
        if (
            product.status != ProductStatus.PUBLISHED
            or product.product_type != ProductType.GAME
            or product.commerce_authority != ProductCommerceAuthority.DIGITAL_PRODUCTS
            or offer.sale_state != DigitalOfferSaleState.ACTIVE
            or not delivered_version.is_active
            or pool.status != InventoryPoolStatus.ENABLED
        ):
            raise DigitalOfferUnavailableError("Digital Offer is unavailable.")
        try:
            selection.full_clean()
            offer.full_clean()
        except ValidationError as exc:
            raise DigitalCartStaleError("Digital Cart selection is invalid.") from exc
        _compatibility(delivered_version, offer.customer_console)
        terms.append((item, selection, offer))
    return terms, locked_pools


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
                    "commercial_revision": COMMERCIAL_SNAPSHOT_REVISION,
                },
            }
            for item, selection, offer in terms
        ]
    )


def _assert_reusable(checkout, cart):
    if checkout.status == CheckoutStatus.EXPIRED or checkout.expires_at <= timezone.now():
        raise DigitalCheckoutExpiredError("Checkout has expired.", details=_resume(checkout))
    if checkout.status != CheckoutStatus.CHECKOUT_DRAFT:
        raise DigitalProductsConflictError("Checkout is no longer reusable.")
    if cart.state != CartState.LOCKED or cart.active_checkout_id != checkout.id:
        raise DigitalCartLockedError("Cart lock does not match Checkout ownership.")
    item_ids = list(
        CartItem.objects.select_for_update().filter(cart=cart).order_by("pk").values_list("pk", flat=True)
    )
    line_ids = list(
        CheckoutLine.objects.select_for_update()
        .filter(checkout=checkout)
        .order_by("source_cart_item_id")
        .values_list("source_cart_item_id", flat=True)
    )
    if line_ids != item_ids:
        raise DigitalCartStaleError("Checkout lines no longer match the Cart.")
    if not line_ids or checkout.lines.filter(
        commerce_authority=ProductCommerceAuthority.DIGITAL_PRODUCTS
    ).count() != len(item_ids):
        raise DigitalCartStaleError("Checkout authority is incoherent.")
    snapshots = list(
        DigitalCheckoutLineSnapshot.objects.select_for_update()
        .filter(checkout_line__checkout=checkout)
        .order_by("checkout_line_id")
    )
    if len(snapshots) != len(item_ids):
        raise DigitalCartStaleError("Checkout snapshots are incomplete.")
    pool_ids = sorted({snapshot.inventory_pool_id for snapshot in snapshots})
    locked_pool_ids = list(
        InventoryPool.objects.select_for_update()
        .filter(pk__in=pool_ids)
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    if locked_pool_ids != pool_ids:
        raise DigitalCartStaleError("Checkout inventory authority is incomplete.")
    revisions = set(checkout.lines.values_list("snapshot__commercial_revision", flat=True))
    if revisions != {COMMERCIAL_SNAPSHOT_REVISION}:
        raise DigitalCartStaleError("Checkout commercial revision is incoherent.")
    reservations = list(
        DigitalInventoryReservation.objects.select_for_update()
        .filter(checkout=checkout)
        .order_by("checkout_line_id")
    )
    if len(reservations) != len(item_ids) or any(
        reservation.state != DigitalInventoryReservationState.ACTIVE for reservation in reservations
    ):
        raise DigitalCartStaleError("Checkout reservations are incomplete.")


def _expire_locked_checkout_if_due(*, checkout, cart, now):
    if checkout.status != CheckoutStatus.CHECKOUT_DRAFT or checkout.expires_at > now:
        return False
    authorities = set(checkout.lines.values_list("commerce_authority", flat=True))
    reservations = list(
        DigitalInventoryReservation.objects.select_for_update()
        .filter(checkout=checkout)
        .order_by("checkout_line_id")
    )
    line_count = checkout.lines.count()
    if (
        authorities != {ProductCommerceAuthority.DIGITAL_PRODUCTS}
        or line_count == 0
        or cart.state != CartState.LOCKED
        or cart.active_checkout_id != checkout.id
        or checkout.lines.filter(digital_snapshot__isnull=True).exists()
        or len(reservations) != line_count
        or any(reservation.state != DigitalInventoryReservationState.ACTIVE for reservation in reservations)
        or checkout.stock_reservations.exists()
        or checkout.orders.exists()
    ):
        raise DigitalCheckoutIntegrityError("Expired Checkout ownership graph is incoherent.")

    checkout.status = CheckoutStatus.EXPIRED
    checkout.expired_at = now
    checkout.version += 1
    checkout.save(update_fields=["status", "expired_at", "version", "updated_at"])
    DigitalInventoryReservation.objects.filter(pk__in=[reservation.pk for reservation in reservations]).update(
        state=DigitalInventoryReservationState.EXPIRED,
        state_changed_at=now,
        resolution_reason="checkout_expired",
        updated_at=now,
    )
    if cart.active_checkout_id == checkout.id:
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
    append_commerce_event(
        checkout=checkout,
        event_type=CommerceEventType.STOCK_RESERVATION_RELEASED,
        actor_type=CommerceActorType.SYSTEM,
        metadata={"reason_code": "checkout_expired"},
    )
    append_commerce_event(
        checkout=checkout,
        event_type=CommerceEventType.CHECKOUT_EXPIRED,
        actor_type=CommerceActorType.SYSTEM,
        metadata={"new_status": CheckoutStatus.EXPIRED},
    )
    append_commerce_event(
        checkout=checkout,
        event_type=CommerceEventType.CART_UNLOCKED,
        actor_type=CommerceActorType.SYSTEM,
        metadata={"reason_code": "checkout_expired"},
    )
    return True


@transaction.atomic
def expire_owned_digital_checkout_if_due(*, actor, checkout_id):
    _require_customer(actor)
    identity = Checkout.objects.filter(pk=checkout_id, user=actor).values("id", "cart_id").first()
    if identity is None:
        return None
    cart = Cart.objects.select_for_update().filter(pk=identity["cart_id"]).first()
    checkout = Checkout.objects.select_for_update().get(pk=identity["id"], user=actor)
    if cart is not None:
        _expire_locked_checkout_if_due(checkout=checkout, cart=cart, now=timezone.now())
    return checkout


def prepare_digital_checkout(*, actor, client_checkout_uuid):
    _require_customer(actor)
    active_id = Cart.objects.filter(user=actor).values_list("active_checkout_id", flat=True).first()
    if active_id is not None:
        expire_owned_digital_checkout_if_due(actor=actor, checkout_id=active_id)
    return _prepare_digital_checkout_atomic(actor=actor, client_checkout_uuid=client_checkout_uuid)


@transaction.atomic
def _prepare_digital_checkout_atomic(*, actor, client_checkout_uuid):
    client_checkout_uuid = UUID(str(client_checkout_uuid))
    cart = Cart.objects.select_for_update().filter(user=actor).first()
    if cart is None:
        raise EmptyDigitalCartError("Digital Cart is empty.")
    existing = Checkout.objects.select_for_update().filter(
        user=actor, client_checkout_uuid=client_checkout_uuid
    ).first()
    try:
        terms, pools = _load_terms(cart)
    except DigitalCartStaleError as exc:
        if existing is not None:
            raise DigitalCheckoutIdempotencyError(
                "Checkout UUID was already used for different terms.", details=_resume(existing)
            ) from exc
        raise
    pool_ids = sorted(pools)
    fingerprint = _fingerprint(terms)
    if existing:
        if existing.cart_id != cart.id:
            raise DigitalCheckoutIdempotencyError(
                "Checkout UUID was already used for different terms.", details=_resume(existing)
            )
        if existing.cart_fingerprint != fingerprint:
            raise DigitalCheckoutIdempotencyError(
                "Checkout UUID was already used for different terms.", details=_resume(existing)
            )
        _assert_reusable(existing, cart)
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
            snapshot={
                "commerce_authority": ProductCommerceAuthority.DIGITAL_PRODUCTS,
                "commercial_revision": COMMERCIAL_SNAPSHOT_REVISION,
            },
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

    held = dict(
        DigitalInventoryReservation.objects.filter(
            inventory_pool_id__in=pool_ids,
            state__in=(
                DigitalInventoryReservationState.ACTIVE,
                DigitalInventoryReservationState.PAYMENT_HOLD,
                DigitalInventoryReservationState.HELD_FOR_REVIEW,
            ),
        )
        .values_list("inventory_pool_id")
        .annotate(total=Sum("quantity"))
    )
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
