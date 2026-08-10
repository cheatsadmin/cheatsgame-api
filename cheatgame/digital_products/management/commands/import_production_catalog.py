import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from cheatgame.digital_products.models import (
    DigitalGameReleaseMetadata,
    DigitalOffer,
    InventoryPool,
)
from cheatgame.product.models import DeliveredVersion, Product


class Command(BaseCommand):
    help = "Dry-run or idempotently import only owner-approved PRODUCTION_READY catalog records."

    def add_arguments(self, parser):
        parser.add_argument("manifest")
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        del args
        payload = self._load(options["manifest"])
        records = [
            item for item in payload["products"]
            if item.get("classification") == "PRODUCTION_READY"
        ]
        skipped = len(payload["products"]) - len(records)
        self._validate_records(records)
        if not options["apply"]:
            self.stdout.write(
                f"dry_run=true production_ready={len(records)} skipped={skipped}"
            )
            return
        with transaction.atomic():
            for record in records:
                self._import_product(record)
        self.stdout.write(
            self.style.SUCCESS(
                f"Production catalog import complete: imported={len(records)} skipped={skipped}"
            )
        )

    @staticmethod
    def _load(filename):
        path = Path(filename)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CommandError("Catalog manifest is unreadable or invalid JSON.") from exc
        if payload.get("schema") != "cheatsg.catalog-promotion.v1" or not isinstance(payload.get("products"), list):
            raise CommandError("Unsupported catalog promotion manifest schema.")
        return payload

    @staticmethod
    def _validate_records(records):
        slugs = set()
        for record in records:
            slug = str(record.get("slug") or "")
            if not slug or slug in slugs:
                raise CommandError("Production-ready Product slugs must be non-empty and unique.")
            slugs.add(slug)
            for field in ("title", "seo_title", "meta_description", "description_storage_key", "main_image_storage_key"):
                if not record.get(field):
                    raise CommandError(f"Production-ready Product {slug} is missing {field}.")

    @staticmethod
    def _ensure_exact(instance, expected, *, label):
        mismatches = []
        for field, value in expected.items():
            actual = getattr(instance, field)
            if hasattr(actual, "name"):
                actual = actual.name
            if actual != value:
                mismatches.append(field)
        if mismatches:
            raise CommandError(f"Existing {label} conflicts in: " + ", ".join(sorted(mismatches)))
        return instance

    def _import_product(self, record):
        generic = record["generic_commerce"]
        expected = {
            "title": record["title"],
            "product_type": int(record["product_type"]),
            "status": record["publication_state"],
            "seo_title": record["seo_title"],
            "meta_description": record["meta_description"],
            "commerce_authority": record["digital_authority"],
            "main_image": record["main_image_storage_key"],
            "description": record["description_storage_key"],
            "price": Decimal(generic["price_irr"]),
            "off_price": Decimal(generic["off_price_irr"]),
            "quantity": int(generic["quantity"]),
            "order_limit": generic.get("order_limit"),
            "device_model": generic.get("device_model"),
        }
        product, _ = Product.objects.get_or_create(
            slug=record["slug"], defaults=expected
        )
        self._ensure_exact(product, expected, label=f"Product {record['slug']}")
        self._import_release(product, record.get("release"))

        pools = {}
        for version_record in record.get("versions") or []:
            version_expected = {"is_active": bool(version_record["is_active"])}
            version, _ = DeliveredVersion.objects.get_or_create(
                product=product,
                native_console=version_record["native_console"],
                defaults=version_expected,
            )
            self._ensure_exact(version, version_expected, label="Delivered Version")
            for offer_record in version_record.get("offers") or []:
                pool_key = str(offer_record["inventory"]["source_pool_id"])
                existing_offer = DigitalOffer.objects.filter(
                    delivered_version=version,
                    customer_console=offer_record["customer_console"],
                    capacity=offer_record["capacity"],
                ).exclude(sale_state="archived").first()
                if existing_offer:
                    pool = existing_offer.inventory_pool
                else:
                    pool = pools.get(pool_key)
                    if pool is None:
                        pool = InventoryPool.objects.create(
                            sellable_quantity=int(offer_record["inventory"]["initial_quantity"]),
                            status=offer_record["inventory"]["status"],
                        )
                        pools[pool_key] = pool
                offer_expected = {
                    "inventory_pool_id": pool.pk,
                    "price": Decimal(offer_record["price_irr"]),
                    "sale_state": offer_record["sale_state"],
                }
                offer, _ = DigitalOffer.objects.get_or_create(
                    delivered_version=version,
                    customer_console=offer_record["customer_console"],
                    capacity=offer_record["capacity"],
                    defaults={
                        "inventory_pool": pool,
                        "price": offer_expected["price"],
                        "sale_state": offer_expected["sale_state"],
                    },
                )
                self._ensure_exact(offer, offer_expected, label="Digital Offer")

    def _import_release(self, product, release):
        if not release:
            return
        expected = {
            "release_date": date.fromisoformat(release["release_date"]) if release.get("release_date") else None,
            "upcoming_status": release["state"],
            "preorder_enabled": bool(release["preorder_enabled"]),
            "preorder_open_at": datetime.fromisoformat(release["preorder_open_at"]) if release.get("preorder_open_at") else None,
            "preorder_close_at": datetime.fromisoformat(release["preorder_close_at"]) if release.get("preorder_close_at") else None,
        }
        metadata, _ = DigitalGameReleaseMetadata.objects.get_or_create(
            product=product, defaults=expected
        )
        self._ensure_exact(metadata, expected, label="Digital release metadata")
