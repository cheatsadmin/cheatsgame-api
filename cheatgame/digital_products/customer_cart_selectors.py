from django.db.models import F, IntegerField, OuterRef, Prefetch, Subquery, Sum, Value
from django.db.models.functions import Coalesce, Greatest

from cheatgame.digital_products.models import DigitalInventoryReservation
from cheatgame.digital_products.public_catalog_selectors import EFFECTIVE_PUBLIC_HOLD_STATES
from cheatgame.product.models import SuggestionProduct
from cheatgame.shop.models import CartItem, CartItemAttachment


def owned_customer_cart_items(*, user):
    """Bounded, read-only projection source for one customer's Cart."""
    held_quantity = (
        DigitalInventoryReservation.objects.filter(
            inventory_pool_id=OuterRef("digital_selection__offer__inventory_pool_id"),
            state__in=EFFECTIVE_PUBLIC_HOLD_STATES,
        )
        .values("inventory_pool_id")
        .annotate(total=Sum("quantity"))
        .values("total")[:1]
    )
    return (
        CartItem.objects.filter(cart__user=user)
        .select_related(
            "cart",
            "product",
            "digital_selection__offer__delivered_version__product",
            "digital_selection__offer__inventory_pool",
        )
        .annotate(
            digital_effective_held_quantity=Coalesce(
                Subquery(held_quantity, output_field=IntegerField()),
                Value(0),
            ),
            digital_available_quantity=Greatest(
                F("digital_selection__offer__inventory_pool__sellable_quantity")
                - F("digital_effective_held_quantity"),
                Value(0),
            ),
        )
        .prefetch_related(
            Prefetch(
                "cartitemattachment_set",
                queryset=CartItemAttachment.objects.select_related("attachment"),
                to_attr="owned_attachment_links",
            ),
            Prefetch(
                "product__suggestions",
                queryset=SuggestionProduct.objects.select_related("suggested"),
                to_attr="cart_suggestion_links",
            ),
        )
        .order_by("pk")
    )


def owned_customer_cart_item(*, user, cart_item_id):
    return owned_customer_cart_items(user=user).filter(pk=cart_item_id).first()
