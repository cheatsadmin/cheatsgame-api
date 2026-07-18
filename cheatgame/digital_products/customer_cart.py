from cheatgame.common.utils import safe_file_url
from cheatgame.digital_products.models import (
    DigitalCartFulfillmentMethod,
    DigitalOfferCapacity,
    DigitalOfferSaleState,
    InventoryPoolStatus,
)
from cheatgame.digital_products.public_catalog import (
    CAPACITY_DISCLOSURES,
    CAPACITY_LABELS,
    COMPATIBILITY_DISCLOSURES,
    CONSOLE_LABELS,
    FULFILLMENT_METHOD_LABELS,
    PUBLIC_DIGITAL_CURRENCY,
    capacity_code_for,
    compatibility_code_for,
)
from cheatgame.product.models import (
    ProductCommerceAuthority,
    ProductStatus,
    ProductType,
)


class DigitalCartProjectionIntegrityError(ValueError):
    pass


def cart_authority_code(item):
    return dict(ProductCommerceAuthority.choices).get(
        item.commerce_authority,
        item.commerce_authority,
    )


def _attachment_links(item):
    links = getattr(item, "owned_attachment_links", None)
    if links is None:
        return tuple(item.cartitemattachment_set.all())
    return links


def coherent_digital_selection(item):
    if item.commerce_authority != ProductCommerceAuthority.DIGITAL_PRODUCTS:
        raise DigitalCartProjectionIntegrityError("The CartItem is not Digital.")
    selection = getattr(item, "digital_selection", None)
    if selection is None:
        raise DigitalCartProjectionIntegrityError("The Digital selection is missing.")
    offer = selection.offer
    if item.quantity != 1 or item.product_id != offer.delivered_version.product_id:
        raise DigitalCartProjectionIntegrityError("The Digital selection identity is incoherent.")
    if _attachment_links(item):
        raise DigitalCartProjectionIntegrityError("Digital CartItems cannot contain attachments.")
    if selection.fulfillment_method not in DigitalCartFulfillmentMethod.values:
        raise DigitalCartProjectionIntegrityError("The fulfillment method is incoherent.")
    if (
        offer.capacity == DigitalOfferCapacity.CAPACITY_1
        and selection.fulfillment_method != DigitalCartFulfillmentMethod.IN_STORE
    ):
        raise DigitalCartProjectionIntegrityError("Capacity 1 requires in-store fulfillment.")
    try:
        compatibility_code_for(
            customer_console=offer.customer_console,
            native_console=offer.delivered_version.native_console,
        )
    except ValueError as exc:
        raise DigitalCartProjectionIntegrityError("The console selection is incoherent.") from exc
    return selection


def _offer_is_currently_available(item, offer):
    product = offer.delivered_version.product
    return bool(
        offer.sale_state == DigitalOfferSaleState.ACTIVE
        and offer.delivered_version.is_active
        and offer.inventory_pool.status == InventoryPoolStatus.ENABLED
        and product.status == ProductStatus.PUBLISHED
        and product.product_type == ProductType.GAME
        and product.commerce_authority == ProductCommerceAuthority.DIGITAL_PRODUCTS
        and (getattr(item, "digital_available_quantity", 0) or 0) > 0
    )


def digital_selection_projection(item):
    selection = coherent_digital_selection(item)
    offer = selection.offer
    version = offer.delivered_version
    product = item.product
    compatibility_code = compatibility_code_for(
        customer_console=offer.customer_console,
        native_console=version.native_console,
    )
    capacity_code = capacity_code_for(offer.capacity)
    is_available = _offer_is_currently_available(item, offer)
    return {
        "offer_id": offer.pk,
        "game": {
            "id": product.pk,
            "slug": product.slug,
            "title": product.title,
            "main_image": safe_file_url(file=product.main_image),
        },
        "customer_console": offer.customer_console,
        "customer_console_label": CONSOLE_LABELS[offer.customer_console],
        "capacity": offer.capacity,
        "capacity_label": CAPACITY_LABELS[offer.capacity],
        "delivered_version_label": version.get_native_console_display(),
        "native_console": version.native_console,
        "native_console_label": CONSOLE_LABELS[version.native_console],
        "compatibility_code": compatibility_code,
        "compatibility_disclosure": COMPATIBILITY_DISCLOSURES[compatibility_code],
        "capacity_code": capacity_code,
        "capacity_disclosure": CAPACITY_DISCLOSURES[capacity_code],
        "fulfillment_method": {
            "code": selection.fulfillment_method,
            "label": FULFILLMENT_METHOD_LABELS[selection.fulfillment_method],
        },
        "unit_price": item.price,
        "line_total": item.price,
        "currency": PUBLIC_DIGITAL_CURRENCY,
        "availability": "AVAILABLE" if is_available else "SOLD_OUT",
        "is_available": is_available,
    }


def digital_cart_item_projection(item):
    return {
        "id": item.pk,
        "commerce_authority": cart_authority_code(item),
        "price": item.price,
        "quantity": item.quantity,
        "digital_selection": digital_selection_projection(item),
    }


def digital_cart_product_projection(item):
    return {
        "id": item.product_id,
        "product_type": item.product.product_type,
        "title": item.product.title,
        "slug": item.product.slug,
        "main_image": safe_file_url(file=item.product.main_image),
        "device_model": item.product.device_model,
    }
