from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from cheatgame.product.models import (
    Product,
    ProductSlugHistory,
    ProductStatus,
    ProductType,
)
from cheatgame.product.selectors.product import product_detail
from cheatgame.product.services.product import build_unique_product_slug, update_product


class ProductSlugHistoryTests(TestCase):
    def product(self, title, slug):
        return Product.objects.create(
            product_type=ProductType.GAME,
            title=title,
            slug=slug,
            status=ProductStatus.PUBLISHED,
            main_image="tests/cover.webp",
            description="tests/description.html",
            price=Decimal("1"),
            off_price=Decimal("0"),
            quantity=1,
        )

    def rename(self, product, slug):
        return update_product(
            product_id=product.pk,
            product_type=product.product_type,
            title=product.title,
            price=product.price,
            off_price=product.off_price,
            quantity=product.quantity,
            discount_end_time=product.discount_end_time,
            order_limit=product.order_limit,
            device_model=product.device_model,
            slug=slug,
            status=product.status,
            seo_title=product.seo_title,
            meta_description=product.meta_description,
        )

    def test_unicode_former_slug_resolves_to_current_product(self):
        product = self.product("First Light", "اکانت-بازی-first-light-007")
        self.rename(product, "007-first-light")

        history = ProductSlugHistory.objects.get()
        self.assertEqual(history.slug, "اکانت-بازی-first-light-007")
        self.assertEqual(history.product_id, product.pk)
        self.assertEqual(
            product_detail(slug=history.slug).slug,
            "007-first-light",
        )

    def test_historical_slug_cannot_be_claimed_by_another_product(self):
        first = self.product("First", "first")
        self.rename(first, "first-current")
        second = self.product("Second", "second")

        with self.assertRaises(ValidationError):
            self.rename(second, "first")
        self.assertEqual(build_unique_product_slug("first"), "first-2")

    def test_active_slug_wins_and_reverting_does_not_loop(self):
        product = self.product("First", "first")
        self.rename(product, "second")
        self.rename(product, "first")

        self.assertEqual(product_detail(slug="first").pk, product.pk)
        self.assertEqual(product_detail(slug="second").pk, product.pk)
        self.assertEqual(
            list(product.slug_history.values_list("slug", flat=True)),
            ["second"],
        )

    def test_direct_model_writes_cannot_cross_active_and_historical_namespaces(self):
        first = self.product("First", "first")
        second = self.product("Second", "second")

        with self.assertRaises(ValidationError):
            ProductSlugHistory.objects.create(product=first, slug="second")

        self.rename(first, "first-current")
        second.slug = "first"
        with self.assertRaises(ValidationError):
            second.save(update_fields=("slug", "updated_at"))
