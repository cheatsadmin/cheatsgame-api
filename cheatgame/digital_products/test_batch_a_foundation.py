from threading import Barrier, Thread
from unittest import skipUnless
from uuid import uuid4

from django.contrib import admin
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase, TransactionTestCase

from cheatgame.digital_products.models import (
    DigitalOffer,
    DigitalOfferCapacity,
    DigitalOfferSaleState,
    InventoryPool,
    InventoryPoolStatus,
    PoolStockAdjustment,
    PoolStockAdjustmentReason,
)
from cheatgame.digital_products.services import (
    DigitalProductsConflictError,
    DigitalProductsValidationError,
    InsufficientStockError,
    OfferTransitionError,
    StockIdempotencyConflictError,
)
from cheatgame.digital_products.services.catalog_admin import (
    activate_digital_product,
    create_delivered_version,
    deactivate_digital_product,
    evaluate_product_readiness,
)
from cheatgame.digital_products.services.inventory import adjust_pool_stock
from cheatgame.digital_products.services.offers import (
    create_digital_offer,
    link_offer_to_shared_pool,
    move_offer_to_new_independent_pool,
    transition_offer_sale_state,
    update_offer_price,
)
from cheatgame.product.models import (
    DeliveredVersion,
    NativeConsole,
    Product,
    ProductCommerceAuthority,
    ProductType,
)
from cheatgame.users.models import BaseUser, UserTypes


class BatchAFoundationTests(TestCase):
    def setUp(self):
        self.manager = self.user("09121110001", UserTypes.MANAGER)
        self.admin_user = self.user("09121110002", UserTypes.ADMIN)
        self.customer = self.user("09121110003", UserTypes.CUSTOMER)
        self.game = self.product("Batch A Game", ProductType.GAME)
        self.version = DeliveredVersion.objects.create(product=self.game, native_console=NativeConsole.PS4)

    def user(self, phone, user_type):
        return BaseUser.objects.create_user(
            phone_number=phone,
            firstname="Batch",
            lastname="A",
            password="Test-only-password-123",
            user_type=user_type,
        )

    def product(self, title, product_type=ProductType.PHYSCIAL):
        return Product.objects.create(
            product_type=product_type,
            title=title,
            main_image="product/main_images/test.jpg",
            price=50000,
            off_price=45000,
            quantity=9,
            description="product/descriptions/test.html",
        )

    def offer(self, **overrides):
        values = {
            "delivered_version_id": self.version.id,
            "customer_console": NativeConsole.PS4,
            "capacity": DigitalOfferCapacity.CAPACITY_1,
            "price": "100000",
            "actor": self.manager,
        }
        values.update(overrides)
        return create_digital_offer(**values)

    def test_standard_defaults_and_pricing_quantity_are_unchanged(self):
        standard = self.product("Standard Product")
        self.assertEqual(standard.commerce_authority, ProductCommerceAuthority.STANDARD_COMMERCE)
        self.assertEqual((standard.price, standard.off_price, standard.quantity), (50000, 45000, 9))

    def test_non_game_cannot_have_delivered_version_or_digital_authority(self):
        standard = self.product("Not a Game")
        with self.assertRaises(ValidationError):
            DeliveredVersion.objects.create(product=standard, native_console=NativeConsole.PS4)
        standard.commerce_authority = ProductCommerceAuthority.DIGITAL_PRODUCTS
        with self.assertRaises(ValidationError):
            standard.full_clean()

    def test_delivered_version_is_unique_active_and_protects_product(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            DeliveredVersion.objects.create(product=self.game, native_console=NativeConsole.PS4)
        with self.assertRaises(ProtectedError):
            self.game.delete()

    def test_manager_service_creates_version_but_customer_cannot(self):
        other = self.product("Other Game", ProductType.GAME)
        created = create_delivered_version(product_id=other.id, native_console=NativeConsole.PS5, actor=self.manager)
        self.assertEqual(created.product, other)
        with self.assertRaises(PermissionDenied):
            create_delivered_version(product_id=other.id, native_console=NativeConsole.PS4, actor=self.customer)

    def test_offer_uses_decimal_price_and_independent_paused_pool(self):
        offer, pool = self.offer(initial_stock=3)
        self.assertEqual(str(offer.price), "100000")
        self.assertEqual(pool.status, InventoryPoolStatus.PAUSED)
        self.assertEqual(pool.sellable_quantity, 3)
        self.assertEqual(PoolStockAdjustment.objects.filter(inventory_pool=pool).count(), 1)
        self.game.refresh_from_db()
        self.assertEqual((self.game.price, self.game.quantity), (50000, 9))

    def test_fractional_or_negative_offer_values_are_rejected(self):
        for price in ("1.5", -1):
            with self.subTest(price=price), self.assertRaises(DigitalProductsValidationError):
                self.offer(price=price)

    def test_standard_authority_offer_cannot_activate(self):
        offer, _ = self.offer()
        with self.assertRaises(OfferTransitionError):
            transition_offer_sale_state(offer_id=offer.id, target_state=DigitalOfferSaleState.ACTIVE, actor=self.manager)
        self.assertEqual(self.game.commerce_authority, ProductCommerceAuthority.STANDARD_COMMERCE)

    def test_explicit_activation_requires_readiness_then_offer_activation(self):
        empty_game = self.product("Unready Game", ProductType.GAME)
        with self.assertRaises(DigitalProductsConflictError):
            activate_digital_product(product_id=empty_game.id, actor=self.admin_user)
        offer, _ = self.offer()
        self.assertTrue(evaluate_product_readiness(self.game)["ready"])
        activate_digital_product(product_id=self.game.id, actor=self.admin_user)
        active = transition_offer_sale_state(
            offer_id=offer.id,
            target_state=DigitalOfferSaleState.ACTIVE,
            actor=self.manager,
        )
        self.assertEqual(active.sale_state, DigitalOfferSaleState.ACTIVE)

    def test_deactivation_preserves_history_and_requires_no_active_offer(self):
        offer, _ = self.offer()
        activate_digital_product(product_id=self.game.id, actor=self.admin_user)
        transition_offer_sale_state(offer_id=offer.id, target_state=DigitalOfferSaleState.ACTIVE, actor=self.manager)
        with self.assertRaises(DigitalProductsConflictError):
            deactivate_digital_product(product_id=self.game.id, actor=self.admin_user)
        transition_offer_sale_state(offer_id=offer.id, target_state=DigitalOfferSaleState.PAUSED, actor=self.manager)
        deactivate_digital_product(product_id=self.game.id, actor=self.admin_user)
        self.assertTrue(DigitalOffer.objects.filter(pk=offer.pk).exists())
        self.assertEqual(self.game.delivered_versions.count(), 1)

    def test_price_update_does_not_mutate_product_price(self):
        offer, _ = self.offer()
        update_offer_price(offer_id=offer.id, price=120000, actor=self.manager)
        self.game.refresh_from_db()
        self.assertEqual(self.game.price, 50000)

    def test_adjustment_is_idempotent_append_only_and_never_negative(self):
        _, pool = self.offer()
        key = uuid4()
        first, available = adjust_pool_stock(
            pool_id=pool.id,
            delta=2,
            reason=PoolStockAdjustmentReason.INVENTORY_RECEIVED,
            actor=self.manager,
            idempotency_key=key,
        )
        second, retry_available = adjust_pool_stock(
            pool_id=pool.id,
            delta=2,
            reason=PoolStockAdjustmentReason.INVENTORY_RECEIVED,
            actor=self.manager,
            idempotency_key=key,
        )
        self.assertEqual((first.id, available), (second.id, retry_available))
        with self.assertRaises(StockIdempotencyConflictError):
            adjust_pool_stock(
                pool_id=pool.id,
                delta=3,
                reason=PoolStockAdjustmentReason.INVENTORY_RECEIVED,
                actor=self.manager,
                idempotency_key=key,
            )
        with self.assertRaises(InsufficientStockError):
            adjust_pool_stock(
                pool_id=pool.id,
                delta=-3,
                reason=PoolStockAdjustmentReason.MARK_UNAVAILABLE,
                actor=self.manager,
                idempotency_key=uuid4(),
            )
        with self.assertRaises(ValidationError):
            first.delete()

    def test_reconciliation_is_admin_only_and_actor_is_protected(self):
        _, pool = self.offer()
        with self.assertRaises(PermissionDenied):
            adjust_pool_stock(
                pool_id=pool.id,
                delta=1,
                reason=PoolStockAdjustmentReason.RECONCILIATION,
                actor=self.manager,
                idempotency_key=uuid4(),
            )
        adjustment, _ = adjust_pool_stock(
            pool_id=pool.id,
            delta=1,
            reason=PoolStockAdjustmentReason.RECONCILIATION,
            actor=self.admin_user,
            idempotency_key=uuid4(),
        )
        with self.assertRaises(ProtectedError):
            self.admin_user.delete()
        self.assertEqual(adjustment.actor_id, self.admin_user.id)

    def test_shared_pool_requires_compatible_version_and_capacity_and_never_merges_balance(self):
        first, target = self.offer(initial_stock=2)
        second, source = self.offer(customer_console=NativeConsole.PS5, initial_stock=5)
        linked = link_offer_to_shared_pool(offer_id=second.id, target_pool_id=target.id, actor=self.admin_user)
        target.refresh_from_db()
        source.refresh_from_db()
        self.assertEqual(linked.inventory_pool_id, first.inventory_pool_id)
        self.assertEqual((target.sellable_quantity, source.sellable_quantity), (2, 5))
        other_version = DeliveredVersion.objects.create(
            product=self.product("Different Game", ProductType.GAME),
            native_console=NativeConsole.PS4,
        )
        incompatible, _ = self.offer(
            delivered_version_id=other_version.id,
            customer_console=NativeConsole.PS5,
        )
        with self.assertRaises(DigitalProductsValidationError):
            link_offer_to_shared_pool(offer_id=incompatible.id, target_pool_id=target.id, actor=self.admin_user)

    def test_move_to_independent_pool_starts_zero_without_moving_balance(self):
        offer, old_pool = self.offer(initial_stock=6)
        moved, new_pool = move_offer_to_new_independent_pool(offer_id=offer.id, actor=self.admin_user)
        old_pool.refresh_from_db()
        self.assertEqual(moved.inventory_pool, new_pool)
        self.assertEqual((old_pool.sellable_quantity, new_pool.sellable_quantity), (6, 0))

    def test_no_generic_admin_or_customer_api_surface(self):
        self.assertNotIn(InventoryPool, admin.site._registry)
        self.assertNotIn(DigitalOffer, admin.site._registry)
        self.assertNotIn(PoolStockAdjustment, admin.site._registry)


@skipUnless(connection.vendor == "postgresql", "PostgreSQL concurrency validation only")
class BatchAPostgreSQLConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.manager = BaseUser.objects.create_user(
            phone_number="09121110011",
            firstname="Concurrent",
            lastname="Manager",
            password="Test-only-password-123",
            user_type=UserTypes.MANAGER,
        )
        self.pool = InventoryPool.objects.create()

    def test_concurrent_adjustments_serialize_on_pool_row(self):
        barrier = Barrier(2)
        outcomes = []

        def worker():
            close_old_connections()
            try:
                barrier.wait(timeout=5)
                adjust_pool_stock(
                    pool_id=self.pool.id,
                    delta=1,
                    reason=PoolStockAdjustmentReason.INVENTORY_RECEIVED,
                    actor=BaseUser.objects.get(pk=self.manager.pk),
                    idempotency_key=uuid4(),
                )
                outcomes.append("adjusted")
            except Exception as exc:
                outcomes.append(type(exc).__name__)
            finally:
                close_old_connections()

        threads = [Thread(target=worker), Thread(target=worker)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(any(thread.is_alive() for thread in threads), outcomes)
        self.assertCountEqual(outcomes, ["adjusted", "adjusted"])
        self.pool.refresh_from_db()
        self.assertEqual(self.pool.sellable_quantity, 2)
