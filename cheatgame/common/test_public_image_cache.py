import io
import json
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from cheatgame.product.models import Product, ProductStatus


class _StorageClient:
    def __init__(self):
        self.cache_control = None
        self.copy_calls = 0

    def head_object(self, **kwargs):
        del kwargs
        return {
            "CacheControl": self.cache_control,
            "ContentLength": 123,
            "ContentType": "image/webp",
            "ETag": '"stable-etag"',
            "Metadata": {"owner": "catalog"},
        }

    def copy_object(self, **kwargs):
        self.copy_calls += 1
        self.cache_control = kwargs["CacheControl"]


@override_settings(
    AWS_STORAGE_BUCKET_NAME="production-media",
    AWS_STORAGE_ENVIRONMENT="production",
)
class PublicImageCacheCommandTests(TestCase):
    def setUp(self):
        Product.objects.create(
            product_type=3,
            title="Published product",
            slug="published-product",
            status=ProductStatus.PUBLISHED,
            main_image="product/main_images/published.webp",
            description="product/descriptions/published.html",
            price=1,
            off_price=1,
        )

    def test_inspect_is_read_only_and_apply_is_verified_and_idempotent(self):
        client = _StorageClient()
        storage = Mock()
        storage.connection.meta.client = client
        output = io.StringIO()
        with patch(
            "cheatgame.common.management.commands.configure_public_image_cache.default_storage",
            storage,
        ):
            call_command("configure_public_image_cache", stdout=output)
            self.assertEqual(client.copy_calls, 0)
            self.assertEqual(json.loads(output.getvalue())["changed_or_pending"], 1)

            output = io.StringIO()
            call_command("configure_public_image_cache", apply=True, stdout=output)
            self.assertEqual(client.copy_calls, 1)
            self.assertEqual(json.loads(output.getvalue())["failures"], [])

            call_command("configure_public_image_cache", apply=True)
            self.assertEqual(client.copy_calls, 1)

    def test_unpublished_products_are_not_cached_as_public_media(self):
        Product.objects.filter(slug="published-product").update(status=ProductStatus.HIDDEN)
        client = _StorageClient()
        storage = Mock()
        storage.connection.meta.client = client
        output = io.StringIO()
        with patch(
            "cheatgame.common.management.commands.configure_public_image_cache.default_storage",
            storage,
        ):
            call_command("configure_public_image_cache", stdout=output)
        self.assertEqual(json.loads(output.getvalue())["inspected"], 0)

    @override_settings(AWS_STORAGE_ENVIRONMENT="staging")
    def test_apply_boundary_rejects_non_production_storage(self):
        with self.assertRaisesMessage(CommandError, "only in Production"):
            call_command("configure_public_image_cache", apply=True)
