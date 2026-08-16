import json
from datetime import timedelta
from io import BytesIO, StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone
from PIL import Image

from cheatgame.digital_products.catalog_expansion import (
    EXCLUDE_STAGING_TEST,
    OWNER_REVIEW,
    PLATFORM_CAPABILITY_REQUIRED,
    PRODUCTION_READY,
    validate_game_for_production,
)
from cheatgame.digital_products.models import (
    DigitalGameReleaseMetadata,
    DigitalGameUpcomingStatus,
    DigitalOffer,
    DigitalOfferSaleState,
    InventoryPool,
    InventoryPoolStatus,
)
from cheatgame.product.models import (
    DeliveredVersion,
    NativeConsole,
    Product,
    ProductCommerceAuthority,
    ProductSlugHistory,
    ProductStatus,
    ProductType,
)


def _image_upload(name="cover.webp"):
    output = BytesIO()
    Image.new("RGB", (800, 1000), "navy").save(output, format="WEBP")
    return SimpleUploadedFile(name, output.getvalue(), content_type="image/webp")


class CatalogExpansionWorkflowTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._media = TemporaryDirectory()
        cls._settings = override_settings(MEDIA_ROOT=cls._media.name)
        cls._settings.enable()

    @classmethod
    def tearDownClass(cls):
        cls._settings.disable()
        cls._media.cleanup()
        super().tearDownClass()

    def _game(
        self,
        *,
        title="Workflow Game",
        slug="workflow-game",
        state=DigitalGameUpcomingStatus.RELEASED,
        seo_title="خرید Workflow Game برای PS5",
        with_offer=None,
    ):
        if with_offer is None:
            with_offer = state in {
                DigitalGameUpcomingStatus.RELEASED,
                DigitalGameUpcomingStatus.PREORDER_OPEN,
            }
        product = Product.objects.create(
            product_type=ProductType.GAME.value,
            commerce_authority=ProductCommerceAuthority.DIGITAL_PRODUCTS,
            title=title,
            slug=slug,
            status=ProductStatus.PUBLISHED,
            seo_title=seo_title,
            meta_description="توضیح دقیق و مفید برای تصمیم خرید مشتری.",
            main_image=_image_upload(f"{slug}.webp"),
            description=SimpleUploadedFile(
                f"{slug}.html",
                "<p>شرح اصلی و کاربردی بازی برای مشتری.</p>".encode(),
                content_type="text/html",
            ),
            price=0,
            off_price=0,
            quantity=0,
        )
        release_date = None
        if state == DigitalGameUpcomingStatus.PREORDER_OPEN:
            release_date = timezone.localdate() + timedelta(days=60)
        elif state == DigitalGameUpcomingStatus.COMING_SOON:
            release_date = timezone.localdate() + timedelta(days=30)
        elif state == DigitalGameUpcomingStatus.RELEASED:
            release_date = timezone.localdate() - timedelta(days=30)
        DigitalGameReleaseMetadata.objects.create(
            product=product,
            release_date=release_date,
            upcoming_status=state,
            preorder_enabled=state == DigitalGameUpcomingStatus.PREORDER_OPEN,
        )
        version = DeliveredVersion.objects.create(
            product=product,
            native_console=NativeConsole.PS5,
            is_active=True,
        )
        if with_offer:
            pool = InventoryPool.objects.create(
                sellable_quantity=3,
                status=InventoryPoolStatus.ENABLED,
            )
            DigitalOffer.objects.create(
                delivered_version=version,
                customer_console=NativeConsole.PS5,
                capacity="capacity_2",
                price=5_600_000,
                inventory_pool=pool,
                sale_state=DigitalOfferSaleState.ACTIVE,
            )
        return product

    def _prepare_bundle(self, product):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        bundle = Path(temporary.name) / "bundle"
        output = StringIO()
        call_command(
            "promote_game_to_production",
            product.pk,
            dry_run=True,
            bundle_dir=str(bundle),
            stdout=output,
        )
        return bundle, json.loads(output.getvalue())

    def _remove_source_graph(self, product):
        pool_ids = list(
            DigitalOffer.objects.filter(delivered_version__product=product)
            .values_list("inventory_pool_id", flat=True)
        )
        DigitalOffer.objects.filter(delivered_version__product=product).delete()
        DeliveredVersion.objects.filter(product=product).delete()
        DigitalGameReleaseMetadata.objects.filter(product=product).delete()
        ProductSlugHistory.objects.filter(product=product).delete()
        product.delete()
        InventoryPool.objects.filter(pk__in=pool_ids).delete()

    def test_released_upcoming_and_preorder_state_contracts(self):
        released = self._game()
        upcoming = self._game(
            title="Announced Game",
            slug="announced-game",
            state=DigitalGameUpcomingStatus.ANNOUNCED,
        )
        preorder = self._game(
            title="Preorder Game",
            slug="preorder-game",
            state=DigitalGameUpcomingStatus.PREORDER_OPEN,
        )
        for product in (released, upcoming, preorder):
            result = validate_game_for_production(product.pk)
            self.assertEqual(result["classification"], PRODUCTION_READY)
            self.assertTrue(result["ready"])

        self.assertFalse(
            DigitalOffer.objects.filter(delivered_version__product=upcoming).exists()
        )

    def test_owner_review_and_staging_test_are_distinct(self):
        missing_seo = self._game(slug="owner-review", seo_title="")
        test_game = self._game(title="FC26 staging test", slug="fc26-stage-test")
        review = validate_game_for_production(missing_seo.pk)
        excluded = validate_game_for_production(test_game.pk)
        self.assertEqual(review["classification"], OWNER_REVIEW)
        self.assertIn("SEO_TITLE_PRESENT", review["blockers"])
        self.assertEqual(excluded["classification"], EXCLUDE_STAGING_TEST)

    def test_unsupported_public_state_escalates_platform_capability(self):
        cancelled = self._game(
            title="Cancelled Game",
            slug="cancelled-game",
            state=DigitalGameUpcomingStatus.CANCELLED,
        )
        result = validate_game_for_production(cancelled.pk)
        self.assertEqual(result["classification"], OWNER_REVIEW)
        self.assertEqual(result["escalation"], PLATFORM_CAPABILITY_REQUIRED)
        self.assertIn("PUBLIC_STATE_SUPPORTED", result["blockers"])

    def test_dry_run_is_read_only_and_emits_checksum_bound_bundle(self):
        game = self._game()
        before = {
            "products": Product.objects.count(),
            "offers": DigitalOffer.objects.count(),
            "pools": InventoryPool.objects.count(),
        }
        bundle, result = self._prepare_bundle(game)
        after = {
            "products": Product.objects.count(),
            "offers": DigitalOffer.objects.count(),
            "pools": InventoryPool.objects.count(),
        }
        self.assertEqual(before, after)
        self.assertFalse(result["database_mutated"])
        manifest = json.loads((bundle / "manifest.json").read_text())
        self.assertEqual(manifest["products"][0]["slug"], game.slug)
        self.assertEqual(len(manifest["media"]), 2)
        self.assertTrue(all(item["sha256"] for item in manifest["media"]))
        second_bundle, second_result = self._prepare_bundle(game)
        self.assertNotEqual(bundle, second_bundle)
        self.assertEqual(
            result["bundle"]["manifest_sha256"],
            second_result["bundle"]["manifest_sha256"],
        )

    def test_apply_and_replay_are_idempotent_and_isolate_unrelated_data(self):
        unrelated = self._game(title="Existing Game", slug="existing-game")
        game = self._game()
        bundle, prepared = self._prepare_bundle(game)
        unrelated_snapshot = (unrelated.title, unrelated.slug, unrelated.updated_at)
        self._remove_source_graph(game)

        first = StringIO()
        call_command(
            "promote_game_to_production",
            apply=True,
            bundle_dir=str(bundle),
            manifest_sha256=prepared["bundle"]["manifest_sha256"],
            stdout=first,
        )
        first_counts = (
            Product.objects.count(),
            DeliveredVersion.objects.count(),
            DigitalOffer.objects.count(),
            InventoryPool.objects.count(),
        )
        second = StringIO()
        call_command(
            "promote_game_to_production",
            apply=True,
            bundle_dir=str(bundle),
            manifest_sha256=prepared["bundle"]["manifest_sha256"],
            stdout=second,
        )
        self.assertEqual(
            first_counts,
            (
                Product.objects.count(),
                DeliveredVersion.objects.count(),
                DigitalOffer.objects.count(),
                InventoryPool.objects.count(),
            ),
        )
        unrelated.refresh_from_db()
        self.assertEqual(
            (unrelated.title, unrelated.slug, unrelated.updated_at),
            unrelated_snapshot,
        )
        self.assertEqual(Product.objects.filter(slug="workflow-game").count(), 1)

    def test_media_checksum_conflict_fails_before_database_apply(self):
        game = self._game()
        bundle, prepared = self._prepare_bundle(game)
        manifest = json.loads((bundle / "manifest.json").read_text())
        target = bundle / "media" / manifest["media"][0]["bundle_name"]
        target.write_bytes(b"tampered")
        self._remove_source_graph(game)
        with self.assertRaises(CommandError):
            call_command(
                "promote_game_to_production",
                apply=True,
                bundle_dir=str(bundle),
                manifest_sha256=prepared["bundle"]["manifest_sha256"],
            )
        self.assertFalse(Product.objects.filter(slug="workflow-game").exists())

    def test_apply_migrates_slug_and_preserves_one_product_with_history(self):
        game = self._game(slug="new-game-slug")
        ProductSlugHistory.objects.create(product=game, slug="legacy-game-slug")
        bundle, prepared = self._prepare_bundle(game)
        self._remove_source_graph(game)
        old = self._game(slug="legacy-game-slug")
        old.title = "Workflow Game"
        old.seo_title = "خرید Workflow Game برای PS5"
        old.meta_description = "توضیح دقیق و مفید برای تصمیم خرید مشتری."
        old.save(update_fields=["title", "seo_title", "meta_description", "updated_at"])
        # Reuse the promoted media keys so the existing Product graph is otherwise exact.
        record = json.loads((bundle / "manifest.json").read_text())["products"][0]
        old.main_image = record["main_image_storage_key"]
        old.description = record["description_storage_key"]
        old.save(update_fields=["main_image", "description", "updated_at"])
        old.digital_release_metadata.delete()
        DigitalOffer.objects.filter(delivered_version__product=old).delete()
        DeliveredVersion.objects.filter(product=old).delete()
        DigitalGameReleaseMetadata.objects.create(
            product=old,
            release_date=timezone.localdate() - timedelta(days=30),
            upcoming_status=DigitalGameUpcomingStatus.RELEASED,
            preorder_enabled=False,
        )
        version_record = record["versions"][0]
        version = DeliveredVersion.objects.create(
            product=old,
            native_console=version_record["native_console"],
            is_active=True,
        )
        offer_record = version_record["offers"][0]
        pool = InventoryPool.objects.create(
            sellable_quantity=offer_record["inventory"]["initial_quantity"],
            status=offer_record["inventory"]["status"],
        )
        DigitalOffer.objects.create(
            delivered_version=version,
            customer_console=offer_record["customer_console"],
            capacity=offer_record["capacity"],
            price=offer_record["price_irr"],
            inventory_pool=pool,
            sale_state=offer_record["sale_state"],
        )

        call_command(
            "promote_game_to_production",
            apply=True,
            bundle_dir=str(bundle),
            manifest_sha256=prepared["bundle"]["manifest_sha256"],
        )
        self.assertEqual(Product.objects.filter(title="Workflow Game").count(), 1)
        promoted = Product.objects.get(title="Workflow Game")
        self.assertEqual(promoted.slug, "new-game-slug")
        self.assertTrue(
            ProductSlugHistory.objects.filter(
                product=promoted, slug="legacy-game-slug"
            ).exists()
        )
