from cheatgame.common.utils import safe_file_url
from cheatgame.digital_products.models import (
    CapacityDisclosure,
    CompatibilityDisclosure,
    DigitalCartFulfillmentMethod,
    DigitalOfferCapacity,
)
from cheatgame.digital_products.public_catalog_selectors import prefetched_public_offers
from cheatgame.product.models import NativeConsole


PUBLIC_DIGITAL_CURRENCY = "IRT"

CONSOLE_LABELS = {NativeConsole.PS4: "PS4", NativeConsole.PS5: "PS5"}
CAPACITY_LABELS = {
    DigitalOfferCapacity.CAPACITY_1: "ظرفیت ۱",
    DigitalOfferCapacity.CAPACITY_2: "ظرفیت ۲",
    DigitalOfferCapacity.CAPACITY_3: "ظرفیت ۳",
}
FULFILLMENT_METHOD_LABELS = {
    DigitalCartFulfillmentMethod.IN_STORE: "نصب توسط چیتس گیم",
    DigitalCartFulfillmentMethod.REMOTE: "نصب ریموت",
}
COMPATIBILITY_DISCLOSURES = {
    CompatibilityDisclosure.NATIVE_VERSION_V1: (
        "نسخه تحویلی با کنسول انتخاب‌شده شما سازگار و Native است."
    ),
    CompatibilityDisclosure.PS4_ON_PS5_BACKWARD_COMPATIBLE_V1: (
        "نسخه تحویلی PS4 است؛ این نسخه از طریق Backward Compatibility روی PS5 اجرا می‌شود "
        "و نسخه Native PS5 نیست."
    ),
}
CAPACITY_DISCLOSURES = {
    CapacityDisclosure.CAPACITY_1_OFFLINE_IN_STORE_V1: (
        "آفلاین — نیازمند تحویل یا ارسال کنسول به چیتس گیم"
    ),
    CapacityDisclosure.CAPACITY_2_ONLINE_OFFLINE_FLEXIBLE_V1: (
        "آنلاین و آفلاین — نصب حضوری یا ریموت"
    ),
    CapacityDisclosure.CAPACITY_3_ONLINE_FLEXIBLE_V1: "فقط آنلاین — نصب حضوری یا ریموت",
}


def compatibility_code_for(*, customer_console, native_console):
    if customer_console == native_console:
        return CompatibilityDisclosure.NATIVE_VERSION_V1
    if customer_console == NativeConsole.PS5 and native_console == NativeConsole.PS4:
        return CompatibilityDisclosure.PS4_ON_PS5_BACKWARD_COMPATIBLE_V1
    raise ValueError("Public Offer compatibility is incoherent.")


def capacity_code_for(capacity):
    return {
        DigitalOfferCapacity.CAPACITY_1: CapacityDisclosure.CAPACITY_1_OFFLINE_IN_STORE_V1,
        DigitalOfferCapacity.CAPACITY_2: CapacityDisclosure.CAPACITY_2_ONLINE_OFFLINE_FLEXIBLE_V1,
        DigitalOfferCapacity.CAPACITY_3: CapacityDisclosure.CAPACITY_3_ONLINE_FLEXIBLE_V1,
    }[capacity]


def allowed_fulfillment_methods(capacity):
    if capacity == DigitalOfferCapacity.CAPACITY_1:
        methods = (DigitalCartFulfillmentMethod.IN_STORE,)
    else:
        methods = (DigitalCartFulfillmentMethod.IN_STORE, DigitalCartFulfillmentMethod.REMOTE)
    return [{"code": method, "label": FULFILLMENT_METHOD_LABELS[method]} for method in methods]


def public_offer_projection(offer):
    compatibility_code = compatibility_code_for(
        customer_console=offer.customer_console,
        native_console=offer.delivered_version.native_console,
    )
    capacity_code = capacity_code_for(offer.capacity)
    is_available = offer.customer_available_quantity > 0
    return {
        "id": offer.pk,
        "customer_console": offer.customer_console,
        "customer_console_label": CONSOLE_LABELS[offer.customer_console],
        "capacity": offer.capacity,
        "capacity_label": CAPACITY_LABELS[offer.capacity],
        "delivered_version_label": offer.delivered_version.get_native_console_display(),
        "native_console": offer.delivered_version.native_console,
        "native_console_label": CONSOLE_LABELS[offer.delivered_version.native_console],
        "compatibility_code": compatibility_code,
        "compatibility_disclosure": COMPATIBILITY_DISCLOSURES[compatibility_code],
        "capacity_code": capacity_code,
        "capacity_disclosure": CAPACITY_DISCLOSURES[capacity_code],
        "price": offer.price,
        "currency": PUBLIC_DIGITAL_CURRENCY,
        "availability": "AVAILABLE" if is_available else "SOLD_OUT",
        "is_available": is_available,
        "allowed_fulfillment_methods": allowed_fulfillment_methods(offer.capacity),
    }


def public_game_projection(product, *, detail=False):
    offers = prefetched_public_offers(product)
    offer_rows = [public_offer_projection(offer) for offer in offers]
    available_rows = [row for row in offer_rows if row["is_available"]]
    result = {
        "id": product.pk,
        "title": product.title,
        "slug": product.slug,
        "main_image": safe_file_url(file=product.main_image),
        "short_description": product.meta_description or "",
        "purchase_flow": "DIGITAL_GAME",
        "supported_customer_consoles": sorted({row["customer_console"] for row in offer_rows}),
        "available_capacities": sorted({row["capacity"] for row in offer_rows}),
        "starting_price": min(row["price"] for row in offer_rows),
        "currency": PUBLIC_DIGITAL_CURRENCY,
        "availability": "AVAILABLE" if available_rows else "SOLD_OUT",
        "is_available": bool(available_rows),
        "has_native_ps5_offer": any(
            row["customer_console"] == NativeConsole.PS5
            and row["native_console"] == NativeConsole.PS5
            for row in offer_rows
        ),
        "has_ps4_compatible_ps5_offer": any(
            row["compatibility_code"]
            == CompatibilityDisclosure.PS4_ON_PS5_BACKWARD_COMPATIBLE_V1
            for row in offer_rows
        ),
        "updated_at": product.updated_at,
    }
    if detail:
        result.update(
            {
                "seo_title": product.seo_title or product.title,
                "description": safe_file_url(file=product.description),
                "offers": offer_rows,
            }
        )
    return result
