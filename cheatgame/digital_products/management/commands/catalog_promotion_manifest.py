import json

from django.core.management.base import BaseCommand

from cheatgame.digital_products.models import DigitalOffer
from cheatgame.product.models import Product, ProductCommerceAuthority


EXCLUDE_MARKERS = ("fc26", "staging", "stage-test", "seed-test", "تست")


def _classification(product):
    searchable = f"{product.title} {product.slug}".casefold()
    reasons = []
    if any(marker in searchable for marker in EXCLUDE_MARKERS):
        return "EXCLUDE_STAGING_TEST", ["staging_test_marker"]
    if not product.seo_title:
        reasons.append("missing_seo_title")
    if not product.meta_description:
        reasons.append("missing_meta_description")
    if not product.description.name:
        reasons.append("missing_description")
    if not product.main_image.name:
        reasons.append("missing_main_image")
    if reasons:
        return "OWNER_REVIEW", reasons
    return "PRODUCTION_READY", []


class Command(BaseCommand):
    help = "Emit a read-only, owner-reviewable Product and Digital Game Production promotion manifest."

    def add_arguments(self, parser):
        parser.add_argument("--pretty", action="store_true")

    def handle(self, *args, **options):
        del args
        products = Product.objects.prefetch_related(
            "delivered_versions__digital_offers__inventory_pool",
        ).order_by("pk")
        records = []
        for product in products:
            classification, reasons = _classification(product)
            versions = []
            for version in product.delivered_versions.order_by("pk"):
                offers = []
                for offer in version.digital_offers.select_related("inventory_pool").order_by("pk"):
                    offers.append(
                        {
                            "source_offer_id": offer.pk,
                            "customer_console": offer.customer_console,
                            "capacity": offer.capacity,
                            "price_irr": str(offer.price),
                            "sale_state": offer.sale_state,
                            "inventory": {
                                "source_pool_id": offer.inventory_pool_id,
                                "status": offer.inventory_pool.status,
                                "initial_quantity": offer.inventory_pool.sellable_quantity,
                            },
                        }
                    )
                versions.append(
                    {
                        "source_version_id": version.pk,
                        "native_console": version.native_console,
                        "is_active": version.is_active,
                        "offers": offers,
                    }
                )
            release = getattr(product, "digital_release_metadata", None)
            records.append(
                {
                    "classification": classification,
                    "review_reasons": reasons,
                    "source_product_id": product.pk,
                    "title": product.title,
                    "slug": product.slug,
                    "product_type": product.product_type,
                    "publication_state": product.status,
                    "seo_title": product.seo_title,
                    "meta_description": product.meta_description,
                    "description_present": bool(product.description.name),
                    "description_storage_key": product.description.name,
                    "main_image_storage_key": product.main_image.name,
                    "digital_authority": product.commerce_authority,
                    "generic_commerce": {
                        "price_irr": str(product.price),
                        "off_price_irr": str(product.off_price),
                        "quantity": product.quantity,
                        "order_limit": product.order_limit,
                        "device_model": product.device_model,
                    },
                    "release": (
                        {
                            "state": release.upcoming_status,
                            "release_date": release.release_date.isoformat() if release.release_date else None,
                            "preorder_enabled": release.preorder_enabled,
                            "preorder_open_at": release.preorder_open_at.isoformat() if release.preorder_open_at else None,
                            "preorder_close_at": release.preorder_close_at.isoformat() if release.preorder_close_at else None,
                        }
                        if release
                        else None
                    ),
                    "versions": versions,
                }
            )
        payload = {
            "schema": "cheatsg.catalog-promotion.v1",
            "policy": "Only PRODUCTION_READY records may be imported without owner reclassification.",
            "counts": {
                key: sum(record["classification"] == key for record in records)
                for key in ("PRODUCTION_READY", "OWNER_REVIEW", "EXCLUDE_STAGING_TEST")
            },
            "products": records,
        }
        self.stdout.write(
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2 if options["pretty"] else None,
                sort_keys=True,
            )
        )
