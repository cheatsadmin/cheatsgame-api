from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from cheatgame.digital_products.models import (
    DigitalCartFulfillmentMethod,
    DigitalCartSelection,
    DigitalOffer,
    DigitalOfferCapacity,
    DigitalOfferSaleState,
    InventoryPool,
    InventoryPoolStatus,
)
from cheatgame.digital_products.services import (
    DigitalCartLockedError,
    DigitalOfferUnavailableError,
    DigitalProductsConflictError,
    DigitalProductsValidationError,
    MixedCommerceAuthorityError,
)
from cheatgame.digital_products.services.inventory import get_available_quantity
from cheatgame.product.models import ProductCommerceAuthority, ProductType
from cheatgame.shop.models import Cart, CartItem, CartState
from cheatgame.users.models import UserTypes


def _require_actor(actor, cart):
    if not actor or not getattr(actor, "is_authenticated", False) or not getattr(actor, "is_active", False):
        raise PermissionDenied("An active authenticated actor is required.")
    if actor.pk != cart.user_id and actor.user_type not in (UserTypes.MANAGER, UserTypes.ADMIN):
        raise PermissionDenied("The actor cannot modify this Cart.")


def _assert_open(cart):
    if cart.state != CartState.OPEN or cart.active_checkout_id is not None:
        raise DigitalCartLockedError("The Cart is locked by an active Checkout.")


def _validate_method(offer, method):
    if method not in DigitalCartFulfillmentMethod.values:
        raise DigitalProductsValidationError("Fulfillment method is invalid.")
    if offer.capacity == DigitalOfferCapacity.CAPACITY_1 and method != DigitalCartFulfillmentMethod.IN_STORE:
        raise DigitalProductsValidationError("Capacity 1 requires in-store fulfillment.")


def _validate_offer(offer):
    product = offer.delivered_version.product
    if product.product_type != ProductType.GAME or product.commerce_authority != ProductCommerceAuthority.DIGITAL_PRODUCTS:
        raise DigitalOfferUnavailableError("Offer Product is not eligible for Digital Products.")
    if offer.sale_state != DigitalOfferSaleState.ACTIVE or not offer.delivered_version.is_active:
        raise DigitalOfferUnavailableError("Digital Offer is not active.")
    if offer.inventory_pool.status != InventoryPoolStatus.ENABLED:
        raise DigitalOfferUnavailableError("Digital Offer is unavailable.")
    try:
        offer.full_clean()
    except ValidationError as exc:
        raise DigitalOfferUnavailableError("Digital Offer configuration is invalid.") from exc


@transaction.atomic
def add_digital_offer_to_cart(*, cart, offer, fulfillment_method, actor):
    locked_cart = Cart.objects.select_for_update().get(pk=cart.pk)
    _require_actor(actor, locked_cart)
    _assert_open(locked_cart)
    if CartItem.objects.filter(cart=locked_cart).exclude(
        commerce_authority=ProductCommerceAuthority.DIGITAL_PRODUCTS
    ).exists():
        raise MixedCommerceAuthorityError("Mixed Standard and Digital Carts are not supported.")
    locked_offer = DigitalOffer.objects.select_for_update().select_related(
        "delivered_version__product", "inventory_pool"
    ).get(pk=offer.pk)
    _validate_offer(locked_offer)
    _validate_method(locked_offer, fulfillment_method)
    pool = InventoryPool.objects.select_for_update().get(pk=locked_offer.inventory_pool_id)
    if get_available_quantity(pool_id=pool.pk) < 1:
        raise DigitalOfferUnavailableError("Digital Offer is unavailable.")
    if DigitalCartSelection.objects.filter(cart_item__cart=locked_cart, offer=locked_offer).exists():
        raise DigitalProductsConflictError("This Digital Offer is already selected.")
    item = CartItem.objects.create(
        cart=locked_cart,
        product=locked_offer.delivered_version.product,
        quantity=1,
        price=locked_offer.price,
        commerce_authority=ProductCommerceAuthority.DIGITAL_PRODUCTS,
    )
    selection = DigitalCartSelection.objects.create(
        cart_item=item, offer=locked_offer, fulfillment_method=fulfillment_method
    )
    return item, selection


@transaction.atomic
def remove_digital_cart_item(*, cart_item_id, actor):
    cart_id = CartItem.objects.values_list("cart_id", flat=True).get(pk=cart_item_id)
    cart = Cart.objects.select_for_update().get(pk=cart_id)
    _require_actor(actor, cart)
    _assert_open(cart)
    item = CartItem.objects.select_for_update().get(pk=cart_item_id, cart=cart)
    if item.commerce_authority != ProductCommerceAuthority.DIGITAL_PRODUCTS:
        raise DigitalProductsValidationError("CartItem is not Digital.")
    item.delete()


@transaction.atomic
def change_digital_cart_fulfillment_method(*, cart_item_id, fulfillment_method, actor):
    cart_id = CartItem.objects.values_list("cart_id", flat=True).get(pk=cart_item_id)
    cart = Cart.objects.select_for_update().get(pk=cart_id)
    _require_actor(actor, cart)
    _assert_open(cart)
    selection = DigitalCartSelection.objects.select_for_update().select_related("offer").get(
        cart_item_id=cart_item_id
    )
    _validate_method(selection.offer, fulfillment_method)
    if selection.fulfillment_method != fulfillment_method:
        selection.fulfillment_method = fulfillment_method
        selection.save(update_fields=["fulfillment_method", "updated_at"])
    return selection
