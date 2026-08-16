import hashlib
import mimetypes
import re
from io import BytesIO
from pathlib import Path, PurePosixPath

from django.core.files.storage import default_storage
from PIL import Image, UnidentifiedImageError

from cheatgame.common.upload_fields import (
    ALLOWED_IMAGE_EXTENSIONS,
    ALLOWED_IMAGE_FORMATS,
    IMAGE_FORMAT_POLICY,
    MAX_HTML_UPLOAD_BYTES,
    MAX_IMAGE_UPLOAD_BYTES,
)
from cheatgame.digital_products.models import (
    DigitalGameUpcomingStatus,
    DigitalOfferCapacity,
    DigitalOfferSaleState,
    InventoryPoolStatus,
)
from cheatgame.digital_products.public_catalog import allowed_fulfillment_methods
from cheatgame.product.models import (
    NativeConsole,
    Product,
    ProductCommerceAuthority,
    ProductStatus,
    ProductType,
)


PRODUCTION_READY = "PRODUCTION_READY"
OWNER_REVIEW = "OWNER_REVIEW"
EXCLUDE_STAGING_TEST = "EXCLUDE_STAGING_TEST"
PLATFORM_CAPABILITY_REQUIRED = "PLATFORM_CAPABILITY_REQUIRED"

TEST_MARKERS = ("staging test", "stage-test", "seed-test", "qa-test", "تست")
SUPPORTED_PUBLIC_STATES = {
    DigitalGameUpcomingStatus.ANNOUNCED,
    DigitalGameUpcomingStatus.COMING_SOON,
    DigitalGameUpcomingStatus.DELAYED,
    DigitalGameUpcomingStatus.PREORDER_OPEN,
    DigitalGameUpcomingStatus.RELEASED,
}
UPCOMING_STATES = {
    DigitalGameUpcomingStatus.ANNOUNCED,
    DigitalGameUpcomingStatus.COMING_SOON,
    DigitalGameUpcomingStatus.DELAYED,
}
DATED_STATES = {
    DigitalGameUpcomingStatus.COMING_SOON,
    DigitalGameUpcomingStatus.PREORDER_OPEN,
}


def _gate(code, passed, *, detail=None, owner_decision=None):
    result = {"code": code, "passed": bool(passed)}
    if detail:
        result["detail"] = detail
    if owner_decision:
        result["owner_decision"] = owner_decision
    return result


def _safe_storage_key(name):
    name = str(name or "")
    path = PurePosixPath(name)
    return bool(name and not path.is_absolute() and ".." not in path.parts)


def inspect_stored_file(name, *, kind, storage=None):
    """Read-only content-addressed inspection of one catalog-owned object."""
    storage = storage or default_storage
    result = {
        "storage_key": str(name or ""),
        "exists": False,
        "safe_path": _safe_storage_key(name),
        "sha256": None,
        "byte_size": None,
        "content_type": None,
    }
    if not result["safe_path"]:
        result["error"] = "unsafe_or_missing_storage_key"
        return result
    try:
        if not storage.exists(name):
            result["error"] = "storage_object_missing"
            return result
        with storage.open(name, "rb") as source:
            payload = source.read(
                (MAX_IMAGE_UPLOAD_BYTES if kind == "image" else MAX_HTML_UPLOAD_BYTES)
                + 1
            )
    except OSError:
        result["error"] = "storage_object_unreadable"
        return result

    result["exists"] = True
    result["byte_size"] = len(payload)
    result["sha256"] = hashlib.sha256(payload).hexdigest()
    extension = Path(name).suffix.lower()
    if kind == "description":
        result["content_type"] = "text/html"
        if len(payload) > MAX_HTML_UPLOAD_BYTES:
            result["error"] = "description_too_large"
        elif extension not in {".html", ".htm"}:
            result["error"] = "description_extension_invalid"
        else:
            try:
                payload.decode("utf-8")
            except UnicodeDecodeError:
                result["error"] = "description_not_utf8"
        return result

    result["content_type"] = mimetypes.guess_type(name)[0]
    if len(payload) > MAX_IMAGE_UPLOAD_BYTES:
        result["error"] = "image_too_large"
        return result
    if extension not in ALLOWED_IMAGE_EXTENSIONS:
        result["error"] = "image_extension_invalid"
        return result
    try:
        with Image.open(BytesIO(payload)) as image:
            image.verify()
        with Image.open(BytesIO(payload)) as image:
            image_format = (image.format or "").upper()
            width, height = image.size
    except (OSError, UnidentifiedImageError, ValueError):
        result["error"] = "image_payload_invalid"
        return result
    if image_format not in ALLOWED_IMAGE_FORMATS:
        result["error"] = "image_format_invalid"
        return result
    valid_extensions, valid_content_types = IMAGE_FORMAT_POLICY[image_format]
    if extension not in valid_extensions:
        result["error"] = "image_extension_mismatch"
        return result
    result.update(
        {
            "content_type": next(iter(valid_content_types)),
            "format": image_format,
            "width": width,
            "height": height,
        }
    )
    return result


def _product_queryset():
    return Product.objects.select_related("digital_release_metadata").prefetch_related(
        "slug_history",
        "delivered_versions__digital_offers__inventory_pool",
    )


def validate_product_for_catalog_expansion(product, *, storage=None):
    searchable = f"{product.title} {product.slug}".casefold()
    is_test = any(marker in searchable for marker in TEST_MARKERS)
    metadata = getattr(product, "digital_release_metadata", None)
    state = metadata.upcoming_status if metadata else None
    versions = list(product.delivered_versions.all())
    active_versions = [version for version in versions if version.is_active]
    offers = [
        offer
        for version in versions
        for offer in version.digital_offers.all()
        if offer.sale_state != DigitalOfferSaleState.ARCHIVED
    ]
    active_offers = [
        offer for offer in offers if offer.sale_state == DigitalOfferSaleState.ACTIVE
    ]

    media = {
        "main_image": inspect_stored_file(
            product.main_image.name, kind="image", storage=storage
        ),
        "description": inspect_stored_file(
            product.description.name, kind="description", storage=storage
        ),
    }
    identity = [
        _gate("GAME_PRODUCT", product.product_type == ProductType.GAME.value),
        _gate(
            "DIGITAL_AUTHORITY",
            product.commerce_authority == ProductCommerceAuthority.DIGITAL_PRODUCTS,
        ),
        _gate("PRODUCT_PUBLISHED", product.status == ProductStatus.PUBLISHED),
        _gate("TITLE_PRESENT", bool(str(product.title or "").strip())),
        _gate("STABLE_SLUG_PRESENT", bool(str(product.slug or "").strip())),
        _gate(
            "STABLE_ENGLISH_SLUG",
            bool(re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", product.slug or "")),
        ),
    ]
    content = [
        _gate("DESCRIPTION_PRESENT", bool(product.description.name)),
        _gate(
            "DESCRIPTION_OBJECT_VALID",
            media["description"]["exists"] and not media["description"].get("error"),
            detail=media["description"].get("error"),
        ),
    ]
    seo = [
        _gate("SEO_TITLE_PRESENT", bool(str(product.seo_title or "").strip())),
        _gate(
            "META_DESCRIPTION_PRESENT",
            bool(str(product.meta_description or "").strip()),
        ),
    ]
    media_gates = [
        _gate("MAIN_IMAGE_PRESENT", bool(product.main_image.name)),
        _gate(
            "MAIN_IMAGE_OBJECT_VALID",
            media["main_image"]["exists"] and not media["main_image"].get("error"),
            detail=media["main_image"].get("error"),
        ),
        _gate(
            "MAIN_IMAGE_DIMENSIONS_VALID",
            bool(media["main_image"].get("width") and media["main_image"].get("height")),
        ),
    ]

    state_gates = [
        _gate("RELEASE_METADATA_PRESENT", metadata is not None),
        _gate("PUBLIC_STATE_SUPPORTED", state in SUPPORTED_PUBLIC_STATES),
        _gate("ACTIVE_VERSION", bool(active_versions)),
    ]
    commerce = []
    if state in UPCOMING_STATES:
        state_gates.extend(
            [
                _gate(
                    "RELEASE_INFORMATION",
                    state not in DATED_STATES
                    or bool(metadata and metadata.release_date),
                ),
                _gate(
                    "PREORDER_DISABLED",
                    bool(metadata and not metadata.preorder_enabled),
                ),
                _gate("NO_ACTIVE_PURCHASE_OFFER", not active_offers),
            ]
        )
    elif state in {
        DigitalGameUpcomingStatus.PREORDER_OPEN,
        DigitalGameUpcomingStatus.RELEASED,
    }:
        if state == DigitalGameUpcomingStatus.PREORDER_OPEN:
            state_gates.extend(
                [
                    _gate("RELEASE_DATE_PRESENT", bool(metadata.release_date)),
                    _gate("PREORDER_ENABLED", bool(metadata.preorder_enabled)),
                    _gate("PREORDER_SCHEMA_SUPPORTED", True),
                ]
            )
        else:
            state_gates.append(
                _gate("PREORDER_DISABLED", not bool(metadata.preorder_enabled))
            )
        commerce.append(_gate("ACTIVE_OFFER", bool(active_offers)))
        for offer in active_offers:
            label = f"offer:{offer.pk}"
            methods = allowed_fulfillment_methods(offer.capacity)
            commerce.extend(
                [
                    _gate(f"{label}:ACTIVE_VERSION", offer.delivered_version.is_active),
                    _gate(
                        f"{label}:CONSOLE_VALID",
                        offer.customer_console in NativeConsole.values,
                    ),
                    _gate(
                        f"{label}:CAPACITY_VALID",
                        offer.capacity in DigitalOfferCapacity.values,
                    ),
                    _gate(f"{label}:POSITIVE_PRICE", offer.price > 0),
                    _gate(
                        f"{label}:INVENTORY_ENABLED",
                        offer.inventory_pool.status == InventoryPoolStatus.ENABLED,
                    ),
                    _gate(
                        f"{label}:POSITIVE_INVENTORY",
                        offer.inventory_pool.sellable_quantity > 0,
                    ),
                    _gate(f"{label}:FULFILLMENT_VALID", bool(methods)),
                ]
            )

    sections = {
        "identity": identity,
        "content": content,
        "seo": seo,
        "media": media_gates,
        "commerce": commerce,
        "state": state_gates,
    }
    failed = [gate["code"] for gates in sections.values() for gate in gates if not gate["passed"]]
    escalation = None
    if product.product_type != ProductType.GAME.value or (
        state is not None and state not in SUPPORTED_PUBLIC_STATES
    ):
        escalation = PLATFORM_CAPABILITY_REQUIRED
    if is_test:
        classification = EXCLUDE_STAGING_TEST
        blockers = ["staging_test_marker"]
    elif failed:
        classification = OWNER_REVIEW
        blockers = failed
    else:
        classification = PRODUCTION_READY
        blockers = []

    return {
        "source_product_id": product.pk,
        "title": product.title,
        "slug": product.slug,
        "state": state,
        "classification": classification,
        "escalation": escalation,
        "ready": classification == PRODUCTION_READY,
        "sections": sections,
        "blockers": blockers,
        "owner_decisions": [
            gate["owner_decision"]
            for gates in sections.values()
            for gate in gates
            if not gate["passed"] and gate.get("owner_decision")
        ],
        "media_objects": media,
        "source_updated_at": product.updated_at.isoformat(),
    }


def validate_game_for_production(product_id, *, storage=None):
    product = _product_queryset().filter(pk=product_id).first()
    if product is None:
        raise Product.DoesNotExist(f"Digital Game Product {product_id} does not exist.")
    return validate_product_for_catalog_expansion(product, storage=storage)
