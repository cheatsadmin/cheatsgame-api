import json
from pathlib import Path
from tempfile import TemporaryDirectory

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from cheatgame.product.models import Product


class ProductionCatalogCommandTests(TestCase):
    def _manifest_path(self, payload):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "catalog.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_only_production_ready_records_are_imported(self):
        complete = {
            "classification": "PRODUCTION_READY",
            "title": "Approved Game",
            "slug": "approved-game",
            "product_type": 2,
            "publication_state": "published",
            "seo_title": "Approved SEO",
            "meta_description": "Approved metadata",
            "description_storage_key": "production/descriptions/approved.html",
            "main_image_storage_key": "production/products/approved.webp",
            "digital_authority": "digital_products",
            "generic_commerce": {
                "price_irr": "0",
                "off_price_irr": "0",
                "quantity": 0,
                "order_limit": None,
                "device_model": None,
            },
            "release": None,
            "versions": [],
        }
        excluded = dict(complete, classification="EXCLUDE_STAGING_TEST", slug="fc26-test")
        path = self._manifest_path({
            "schema": "cheatsg.catalog-promotion.v1",
            "products": [complete, excluded],
        })
        call_command("import_production_catalog", str(path))
        self.assertFalse(Product.objects.exists())
        call_command("import_production_catalog", str(path), apply=True)
        call_command("import_production_catalog", str(path), apply=True)
        self.assertEqual(list(Product.objects.values_list("slug", flat=True)), ["approved-game"])

    def test_production_ready_record_cannot_omit_owner_review_fields(self):
        path = self._manifest_path({
            "schema": "cheatsg.catalog-promotion.v1",
            "products": [{"classification": "PRODUCTION_READY", "slug": "incomplete"}],
        })
        with self.assertRaises(CommandError):
            call_command("import_production_catalog", str(path), apply=True)
