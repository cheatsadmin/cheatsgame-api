import json
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from cheatgame.product.models import (
    Category,
    Product,
    ProductCategory,
    ProductSlugHistory,
)


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
            "legacy_slugs": ["بازی-approved-game"],
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
        history = ProductSlugHistory.objects.get()
        self.assertEqual(history.slug, "بازی-approved-game")
        self.assertEqual(history.product.slug, "approved-game")

    def test_production_ready_record_cannot_omit_owner_review_fields(self):
        path = self._manifest_path({
            "schema": "cheatsg.catalog-promotion.v1",
            "products": [{"classification": "PRODUCTION_READY", "slug": "incomplete"}],
        })
        with self.assertRaises(CommandError):
            call_command("import_production_catalog", str(path), apply=True)

    def test_production_categories_and_product_links_import_idempotently(self):
        complete = {
            "classification": "PRODUCTION_READY",
            "title": "Approved Controller",
            "slug": "approved-controller",
            "product_type": 3,
            "publication_state": "published",
            "seo_title": "Approved Controller SEO",
            "meta_description": "Approved Controller metadata",
            "description_storage_key": "production/descriptions/controller.html",
            "main_image_storage_key": "production/products/controller.webp",
            "digital_authority": "standard_commerce",
            "generic_commerce": {
                "price_irr": "1000000",
                "off_price_irr": "0",
                "quantity": 1,
                "order_limit": None,
                "device_model": None,
            },
            "release": None,
            "versions": [],
        }
        product_path = self._manifest_path({
            "schema": "cheatsg.catalog-promotion.v1",
            "products": [complete],
        })
        call_command("import_production_catalog", str(product_path), apply=True)

        category_path = self._manifest_path({
            "schema": "cheatsg.catalog-promotion.v1",
            "categories": [
                {
                    "name": "PlayStation",
                    "slug": "playstation",
                    "category_type": 1,
                    "parent_slug": None,
                },
                {
                    "name": "Controllers",
                    "slug": "controllers",
                    "category_type": 1,
                    "parent_slug": "playstation",
                },
            ],
            "product_category_links": [
                {
                    "product_slug": "approved-controller",
                    "category_slugs": ["controllers"],
                }
            ],
            "products": [],
        })

        call_command("import_production_catalog", str(category_path))
        self.assertFalse(Category.objects.exists())
        call_command("import_production_catalog", str(category_path), apply=True)
        call_command("import_production_catalog", str(category_path), apply=True)

        controller = Category.objects.get(slug="controllers")
        self.assertEqual(controller.parent.slug, "playstation")
        self.assertEqual(Category.objects.count(), 2)
        self.assertEqual(ProductCategory.objects.count(), 1)
        self.assertEqual(
            ProductCategory.objects.get().category_id,
            controller.pk,
        )

        response = self.client.get("/api/product/category-list/1/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 1)
        self.assertEqual(response.json()[0]["slug"], "playstation")
        self.assertEqual(response.json()[0]["children"][0]["slug"], "controllers")

        output = StringIO()
        call_command("catalog_promotion_manifest", stdout=output)
        exported = json.loads(output.getvalue())
        self.assertEqual(
            [category["slug"] for category in exported["categories"]],
            ["playstation", "controllers"],
        )
        self.assertEqual(
            exported["product_category_links"],
            [
                {
                    "product_slug": "approved-controller",
                    "category_slugs": ["controllers"],
                }
            ],
        )
