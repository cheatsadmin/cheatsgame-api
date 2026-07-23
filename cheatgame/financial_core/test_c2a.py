from datetime import timedelta
from decimal import Decimal
from threading import Barrier, Thread
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError, close_old_connections, connection, transaction
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from cheatgame.digital_products.models import (
    DigitalCartFulfillmentMethod,
    DigitalInventoryReservation,
    DigitalInventoryReservationState,
    DigitalOffer,
    DigitalOfferCapacity,
    DigitalOfferSaleState,
    InventoryPool,
    InventoryPoolStatus,
)
from cheatgame.digital_products.services.cart import add_digital_offer_to_cart
from cheatgame.digital_products.services.checkout_preparation import prepare_digital_checkout
from cheatgame.financial_core.models import (
    FinancialOutboxMessage,
    MerchantAccountVersion,
    MoneyUnit,
    Payment,
    PaymentAttempt,
    PaymentAttemptStatus,
    PaymentCollectionStatus,
    PaymentObligationSource,
    PaymentTenderType,
    PaymentTransaction,
    PaymentTransactionOperation,
    PaymentTransactionStatus,
    ProviderCapabilityVersion,
    ProviderDefinition,
    ProviderRequestOutcome,
    ReviewCase,
    ReviewCaseReason,
    ReviewCaseSeverity,
)
from cheatgame.financial_core.services.adapters import (
    ADAPTER_CONTRACT_VERSION,
    NormalizedProviderResult,
    ProviderAdapterRegistry,
    assert_adapter_conformance,
    execute_adapter_outside_transaction,
)
from cheatgame.financial_core.services.boundaries import ExternalIOInsideTransaction
from cheatgame.financial_core.services.idempotency import IdempotencyConflict
from cheatgame.financial_core.services.money import (
    CANONICAL_IRR_BRIDGE_VERSION,
    LEGACY_IRT_BRIDGE_VERSION,
    normalize_obligation_money,
    represent_provider_money,
)
from cheatgame.financial_core.services.placement import (
    LegacyAdoptionRejected,
    PlacementNotEligible,
    ZeroValueOrderRequired,
    adopt_legacy_order_payment_obligation,
    place_order_and_create_payment_obligation,
)
from cheatgame.financial_core.services.provider_requests import (
    CollectionBlocked,
    StaleRequestClaim,
    apply_provider_request_result,
    claim_provider_request,
    create_or_replay_payment_attempt,
    create_or_replay_request_transaction,
)
from cheatgame.financial_core.services.reviews import open_review_case
from cheatgame.product.models import (
    DeliveredVersion,
    DeliveryOption,
    NativeConsole,
    Product,
    ProductCommerceAuthority,
    ProductType,
)
from cheatgame.shop.models import (
    Cart,
    CartItem,
    CartState,
    CheckoutShippingSnapshot,
    CheckoutStatus,
    DeliverySide,
    DeliveryType,
    Order,
    OrderStatus,
    PaymentTransaction as LegacyPaymentTransaction,
    PaymentTransactionStatus as LegacyPaymentTransactionStatus,
    StockReservation,
    StockReservationState,
)
from cheatgame.shop.services.checkout import create_or_reuse_checkout
from cheatgame.users.models import Address, BaseUser, UserTypes


class SyntheticAdapter:
    adapter_key = "synthetic"
    contract_version = ADAPTER_CONTRACT_VERSION

    def execute_operation(self, envelope):
        return NormalizedProviderResult(
            outcome=ProviderRequestOutcome.ACCEPTED_PENDING,
            evidence_hash="a" * 64,
        )

    def authenticate_callback(self, *, headers, body):
        return None

    def normalize_callback(self, authenticated_callback):
        return None

    def verify_operation(self, envelope):
        return None

    def query_operation(self, envelope):
        return None

    def read_reconciliation_records(self, *, period_start, period_end):
        return ()


class C2AFixture:
    phone_sequence = 0

    def make_user(self):
        type(self).phone_sequence += 1
        user = BaseUser.objects.create_user(
            phone_number=f"091299{type(self).phone_sequence:05d}",
            firstname="C2A",
            lastname="Synthetic",
            password="test-only",
            user_type=UserTypes.CUSTOMER,
        )
        user.phone_verified = True
        user.save(update_fields=("phone_verified", "updated_at"))
        return user

    def make_product(self, *, authority=ProductCommerceAuthority.STANDARD_COMMERCE, price=1000):
        return Product.objects.create(
            product_type=ProductType.GAME if authority == ProductCommerceAuthority.DIGITAL_PRODUCTS else ProductType.PHYSCIAL,
            commerce_authority=authority,
            title=f"C2A product {uuid4()}",
            main_image="tests/product.png",
            price=Decimal(price),
            off_price=Decimal(price),
            quantity=20,
            description="tests/product.html",
            order_limit=5,
        )

    def make_standard_checkout(self, *, price=1000, quantity=1, finalized=True):
        user = self.make_user()
        product = self.make_product(price=price)
        cart = Cart.objects.create(user=user)
        CartItem.objects.create(cart=cart, product=product, quantity=quantity, price=price * quantity)
        checkout = create_or_reuse_checkout(user=user, client_checkout_uuid=uuid4()).checkout
        address = Address.objects.create(
            user=user,
            province="Tehran",
            city="Tehran",
            postal_code="1234567890",
            address_detail="Synthetic test address",
        )
        method = DeliveryType.objects.create(
            name="Synthetic courier",
            delivery_type=DeliveryOption.MOTOR,
            side=DeliverySide.SENDTOUSER,
        )
        CheckoutShippingSnapshot.objects.create(
            checkout=checkout,
            address_id=address.id,
            recipient_name="Synthetic",
            recipient_phone="09120000000",
            province="Tehran",
            city="Tehran",
            full_address="Synthetic test address",
            postal_code="1234567890",
            delivery_method_id=method.id,
            delivery_method_name=method.name,
            delivery_cost=Decimal("0"),
            is_pricing_finalized=finalized,
        )
        return user, product, cart, checkout

    def place_standard(self, *, source_unit=MoneyUnit.IRR, price=1000):
        user, product, cart, checkout = self.make_standard_checkout(price=price)
        result = place_order_and_create_payment_obligation(
            checkout_id=checkout.id,
            expected_user_id=user.id,
            expected_checkout_version=checkout.version,
            source_unit=source_unit,
            idempotency_key=uuid4(),
            actor_id=user.id,
        )
        return user, product, cart, checkout, result

    def make_account(self, *, unit=MoneyUnit.IRR, enabled=True, version=1):
        provider = ProviderDefinition.objects.create(
            key=f"synthetic-{uuid4()}",
            display_name="Synthetic Provider",
            is_enabled=enabled,
            new_requests_enabled=enabled,
        )
        capability = ProviderCapabilityVersion.objects.create(
            provider=provider,
            version=version,
            adapter_key="synthetic",
            adapter_contract_version=ADAPTER_CONTRACT_VERSION,
            provider_unit=unit,
            conversion_policy_version=f"{unit.lower()}-exact-v1",
            supported_operations=[PaymentTransactionOperation.SALE],
            supports_request_idempotency=True,
            supports_lookup=True,
            finality_window_seconds=3600,
            authority_expiry_seconds=900,
        )
        account = MerchantAccountVersion.objects.create(
            provider=provider,
            capability_version=capability,
            account_key="platform-primary",
            version=version,
            owner_key="cheats-game",
            credential_reference="env://SYNTHETIC_PROVIDER_CREDENTIAL",
            is_enabled=enabled,
            new_requests_enabled=enabled,
        )
        return provider, capability, account

    def make_request_graph(self, *, unit=MoneyUnit.IRR, price=1000):
        _, _, _, _, placement = self.place_standard(price=price)
        _, _, account = self.make_account(unit=unit)
        attempt = create_or_replay_payment_attempt(
            payment_id=placement.payment.id,
            merchant_account_version_id=account.id,
            tender_type=PaymentTenderType.EXTERNAL_PROVIDER,
            requested_amount=placement.payment.amount_due,
            idempotency_key=uuid4(),
        ).attempt
        transaction_obj = create_or_replay_request_transaction(
            attempt_id=attempt.id,
            operation_type=PaymentTransactionOperation.SALE,
            idempotency_key=uuid4(),
        ).transaction
        return placement, account, attempt, transaction_obj


class C2APlacementAndMoneyTests(C2AFixture, TestCase):
    def test_standard_order_and_payment_are_atomic_and_reservation_is_not_consumed(self):
        _, product, cart, checkout, result = self.place_standard()
        checkout.refresh_from_db()
        cart.refresh_from_db()
        reservation = StockReservation.objects.get(checkout=checkout)
        product.refresh_from_db()
        self.assertEqual(checkout.status, CheckoutStatus.PENDING_PAYMENT)
        self.assertEqual(cart.lock_reason, "payment_in_progress")
        self.assertEqual(reservation.state, StockReservationState.PAYMENT_HOLD)
        self.assertEqual(reservation.order_id, result.order.id)
        self.assertEqual(product.quantity, 20)
        self.assertEqual(result.payment.order_id, result.order.id)
        self.assertEqual(result.payment.amount_due, Decimal("1000"))

    def test_legacy_customer_order_mutation_rejects_financial_core_owner_cleanly(self):
        user, _, _, _, result = self.place_standard()
        client = APIClient()
        client.force_authenticate(user)
        response = client.put(f"/api/shop/order-detail/{result.order.pk}/", {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["code"], "FINANCIAL_CORE_ORDER_IMMUTABLE")

    def test_duplicate_placement_replays_and_payload_mismatch_conflicts(self):
        user, _, _, checkout = self.make_standard_checkout()
        key = uuid4()
        first = place_order_and_create_payment_obligation(
            checkout_id=checkout.id,
            expected_user_id=user.id,
            expected_checkout_version=checkout.version,
            source_unit=MoneyUnit.IRR,
            idempotency_key=key,
        )
        replay = place_order_and_create_payment_obligation(
            checkout_id=checkout.id,
            expected_user_id=user.id,
            expected_checkout_version=checkout.version,
            source_unit=MoneyUnit.IRR,
            idempotency_key=key,
        )
        self.assertTrue(replay.replayed)
        self.assertEqual((Order.objects.count(), Payment.objects.count()), (1, 1))
        with self.assertRaises(IdempotencyConflict):
            place_order_and_create_payment_obligation(
                checkout_id=checkout.id,
                expected_user_id=user.id,
                expected_checkout_version=checkout.version,
                source_unit=MoneyUnit.IRT,
                idempotency_key=key,
            )

    def test_failed_placement_rolls_back_everything(self):
        user, _, _, checkout = self.make_standard_checkout()
        StockReservation.objects.filter(checkout=checkout).update(state=StockReservationState.RELEASED)
        with self.assertRaises(PlacementNotEligible):
            place_order_and_create_payment_obligation(
                checkout_id=checkout.id,
                expected_user_id=user.id,
                expected_checkout_version=checkout.version,
                source_unit=MoneyUnit.IRR,
                idempotency_key=uuid4(),
            )
        self.assertFalse(Order.objects.exists())
        self.assertFalse(Payment.objects.exists())
        checkout.refresh_from_db()
        self.assertEqual(checkout.status, CheckoutStatus.CHECKOUT_DRAFT)

    def test_unpriced_shipping_is_rejected(self):
        user, _, _, checkout = self.make_standard_checkout(finalized=False)
        with self.assertRaises(PlacementNotEligible):
            place_order_and_create_payment_obligation(
                checkout_id=checkout.id,
                expected_user_id=user.id,
                expected_checkout_version=checkout.version,
                source_unit=MoneyUnit.IRR,
                idempotency_key=uuid4(),
            )

    def test_irt_bridge_converts_exactly_once_and_preserves_evidence(self):
        _, _, _, _, result = self.place_standard(source_unit=MoneyUnit.IRT, price=1000)
        source = result.payment.obligation_source
        self.assertEqual(result.payment.amount_due, Decimal("10000"))
        self.assertEqual((source.source_amount, source.source_unit), (Decimal("1000"), MoneyUnit.IRT))
        self.assertEqual(source.bridge_version, LEGACY_IRT_BRIDGE_VERSION)
        self.assertEqual(source.canonical_amount, Decimal("10000"))
        self.assertEqual(len(source.evidence_fingerprint), 64)

    def test_irr_is_not_multiplied_and_float_or_missing_unit_is_rejected(self):
        normalized = normalize_obligation_money(
            source_amount=Decimal("1230"),
            source_unit=MoneyUnit.IRR,
            source_model="test.Source",
            source_object_id="1",
            source_field="amount",
        )
        self.assertEqual(normalized.canonical_amount, Decimal("1230"))
        self.assertEqual(normalized.bridge_version, CANONICAL_IRR_BRIDGE_VERSION)
        for value, unit in ((1.5, MoneyUnit.IRT), (1, "")):
            with self.assertRaises(ValidationError):
                normalize_obligation_money(
                    source_amount=value,
                    source_unit=unit,
                    source_model="test.Source",
                    source_object_id="2",
                    source_field="amount",
                )

    def test_zero_value_requires_separate_non_payment_boundary(self):
        user, _, _, checkout = self.make_standard_checkout(price=0)
        with self.assertRaises(ZeroValueOrderRequired):
            place_order_and_create_payment_obligation(
                checkout_id=checkout.id,
                expected_user_id=user.id,
                expected_checkout_version=checkout.version,
                source_unit=MoneyUnit.IRR,
                idempotency_key=uuid4(),
            )
        self.assertFalse(Payment.objects.exists())

    def test_mixed_authority_is_rejected(self):
        user, _, _, checkout = self.make_standard_checkout()
        checkout.lines.update(commerce_authority=ProductCommerceAuthority.DIGITAL_PRODUCTS)
        with self.assertRaises(PlacementNotEligible):
            place_order_and_create_payment_obligation(
                checkout_id=checkout.id,
                expected_user_id=user.id,
                expected_checkout_version=checkout.version,
                source_unit=MoneyUnit.IRR,
                idempotency_key=uuid4(),
            )

    def test_digital_order_and_payment_use_pool_hold_without_consumption(self):
        user = self.make_user()
        product = self.make_product(authority=ProductCommerceAuthority.DIGITAL_PRODUCTS, price=9000)
        version = DeliveredVersion.objects.create(product=product, native_console=NativeConsole.PS4)
        pool = InventoryPool.objects.create(sellable_quantity=2, status=InventoryPoolStatus.ENABLED)
        offer = DigitalOffer.objects.create(
            delivered_version=version,
            customer_console=NativeConsole.PS4,
            capacity=DigitalOfferCapacity.CAPACITY_1,
            price=Decimal("9000"),
            inventory_pool=pool,
            sale_state=DigitalOfferSaleState.ACTIVE,
        )
        cart = Cart.objects.create(user=user)
        add_digital_offer_to_cart(
            cart=cart,
            offer=offer,
            fulfillment_method=DigitalCartFulfillmentMethod.IN_STORE,
            actor=user,
        )
        checkout, _ = prepare_digital_checkout(actor=user, client_checkout_uuid=uuid4())
        result = place_order_and_create_payment_obligation(
            checkout_id=checkout.id,
            expected_user_id=user.id,
            expected_checkout_version=checkout.version,
            source_unit=MoneyUnit.IRR,
            idempotency_key=uuid4(),
        )
        reservation = DigitalInventoryReservation.objects.get(checkout=checkout)
        pool.refresh_from_db()
        self.assertEqual(reservation.state, DigitalInventoryReservationState.PAYMENT_HOLD)
        self.assertEqual(reservation.order_id, result.order.id)
        self.assertEqual(pool.sellable_quantity, 2)

    def test_legacy_adoption_is_explicit_idempotent_and_blocks_unsafe_evidence(self):
        user = self.make_user()
        order = Order.objects.create(
            user=user,
            payment_status=OrderStatus.PENDDING,
            total_price=Decimal("500"),
            total_price_discount=Decimal("500"),
        )
        key = uuid4()
        payment = adopt_legacy_order_payment_obligation(
            order_id=order.id,
            expected_user_id=user.id,
            source_unit=MoneyUnit.IRT,
            idempotency_key=key,
            legacy_owner_inactive=True,
            ownership_evidence_reference="release-gate-synthetic",
        )
        replay = adopt_legacy_order_payment_obligation(
            order_id=order.id,
            expected_user_id=user.id,
            source_unit=MoneyUnit.IRT,
            idempotency_key=key,
            legacy_owner_inactive=True,
            ownership_evidence_reference="release-gate-synthetic",
        )
        self.assertEqual((payment.pk, replay.pk, payment.amount_due), (replay.pk, payment.pk, Decimal("5000")))

        blocked_order = Order.objects.create(
            user=user,
            payment_status=OrderStatus.PENDDING,
            total_price=500,
            total_price_discount=500,
        )
        LegacyPaymentTransaction.objects.create(
            order=blocked_order,
            user=user,
            amount=500,
            status=LegacyPaymentTransactionStatus.PENDING,
            idempotency_key=f"legacy-{uuid4()}",
        )
        with self.assertRaises(LegacyAdoptionRejected):
            adopt_legacy_order_payment_obligation(
                order_id=blocked_order.id,
                expected_user_id=user.id,
                source_unit=MoneyUnit.IRT,
                idempotency_key=uuid4(),
                legacy_owner_inactive=True,
                ownership_evidence_reference="release-gate-synthetic",
            )
        self.assertFalse(Payment.objects.filter(order=blocked_order).exists())


class C2AProviderRequestTests(C2AFixture, TransactionTestCase):
    def test_provider_representation_is_exact_and_versioned(self):
        _, capability, _ = self.make_account(unit=MoneyUnit.IRT)
        represented = represent_provider_money(canonical_amount=Decimal("10000"), capability_version=capability)
        self.assertEqual((represented.provider_amount, represented.provider_unit), (Decimal("1000"), MoneyUnit.IRT))
        with self.assertRaises(ValidationError):
            represent_provider_money(canonical_amount=Decimal("10001"), capability_version=capability)

    def test_attempt_and_transaction_persist_versioned_provider_identity(self):
        _, account, attempt, transaction_obj = self.make_request_graph(unit=MoneyUnit.IRT, price=1000)
        self.assertEqual(attempt.merchant_account_version_id, account.id)
        self.assertEqual(transaction_obj.capability_version_id, account.capability_version_id)
        self.assertEqual(transaction_obj.provider_amount, Decimal("100"))
        self.assertEqual(transaction_obj.provider_unit, MoneyUnit.IRT)
        self.assertEqual(transaction_obj.provider_conversion_policy_version, "irt-exact-v1")
        self.assertIsNone(transaction_obj.provider_authority)
        self.assertEqual(transaction_obj.status, PaymentTransactionStatus.CREATED)

    def test_provider_configuration_change_does_not_reinterpret_transaction(self):
        _, account, _, transaction_obj = self.make_request_graph()
        account.new_requests_enabled = False
        account.save(update_fields=("new_requests_enabled", "updated_at"))
        transaction_obj.refresh_from_db()
        self.assertEqual(transaction_obj.merchant_account_version_id, account.id)
        self.assertEqual(transaction_obj.provider_amount, transaction_obj.amount)

    def test_kill_switch_blocks_attempt(self):
        _, _, _, _, placement = self.place_standard()
        _, _, account = self.make_account(enabled=False)
        with self.assertRaises(CollectionBlocked):
            create_or_replay_payment_attempt(
                payment_id=placement.payment.id,
                merchant_account_version_id=account.id,
                tender_type=PaymentTenderType.EXTERNAL_PROVIDER,
                requested_amount=placement.payment.amount_due,
                idempotency_key=uuid4(),
            )

    def test_attempt_and_transaction_replay_and_payload_conflict(self):
        _, _, _, _, placement = self.place_standard()
        _, _, account = self.make_account()
        key = uuid4()
        first = create_or_replay_payment_attempt(
            payment_id=placement.payment.id,
            merchant_account_version_id=account.id,
            tender_type=PaymentTenderType.EXTERNAL_PROVIDER,
            requested_amount=placement.payment.amount_due,
            idempotency_key=key,
        )
        replay = create_or_replay_payment_attempt(
            payment_id=placement.payment.id,
            merchant_account_version_id=account.id,
            tender_type=PaymentTenderType.EXTERNAL_PROVIDER,
            requested_amount=placement.payment.amount_due,
            idempotency_key=key,
        )
        self.assertTrue(replay.replayed)
        self.assertEqual(PaymentAttempt.objects.count(), 1)
        tx_key = uuid4()
        first_tx = create_or_replay_request_transaction(
            attempt_id=first.attempt.id,
            operation_type=PaymentTransactionOperation.SALE,
            idempotency_key=tx_key,
        )
        replay_tx = create_or_replay_request_transaction(
            attempt_id=first.attempt.id,
            operation_type=PaymentTransactionOperation.SALE,
            idempotency_key=tx_key,
        )
        self.assertTrue(replay_tx.replayed)
        self.assertEqual(first_tx.transaction.pk, replay_tx.transaction.pk)

    def test_claim_is_atomic_replayable_and_adapter_execution_is_outside_atomic(self):
        _, _, _, transaction_obj = self.make_request_graph()
        key = uuid4()
        claim = claim_provider_request(transaction_id=transaction_obj.id, claim_idempotency_key=key)
        replay = claim_provider_request(transaction_id=transaction_obj.id, claim_idempotency_key=key)
        self.assertTrue(replay.replayed)
        self.assertEqual(claim.claim.claim_token, replay.claim.claim_token)
        self.assertEqual(claim.envelope.transaction_public_id, str(transaction_obj.public_id))
        with self.assertRaises(IdempotencyConflict):
            claim_provider_request(
                transaction_id=transaction_obj.id,
                claim_idempotency_key=key,
                lease_seconds=120,
            )
        adapter = SyntheticAdapter()
        self.assertEqual(
            execute_adapter_outside_transaction(adapter=adapter, envelope=claim.envelope).outcome,
            ProviderRequestOutcome.ACCEPTED_PENDING,
        )
        with self.assertRaises(ExternalIOInsideTransaction), transaction.atomic():
            execute_adapter_outside_transaction(adapter=adapter, envelope=claim.envelope)

    def test_request_result_replay_rejects_payload_mismatch(self):
        _, _, _, transaction_obj = self.make_request_graph()
        claim = claim_provider_request(transaction_id=transaction_obj.id, claim_idempotency_key=uuid4())
        key = uuid4()
        first = apply_provider_request_result(
            transaction_id=transaction_obj.id,
            claim_token=claim.claim.claim_token,
            outcome=ProviderRequestOutcome.ACCEPTED_PENDING,
            evidence_hash="9" * 64,
            result_idempotency_key=key,
            reason_code="accepted",
        )
        replay = apply_provider_request_result(
            transaction_id=transaction_obj.id,
            claim_token=claim.claim.claim_token,
            outcome=ProviderRequestOutcome.ACCEPTED_PENDING,
            evidence_hash="9" * 64,
            result_idempotency_key=key,
            reason_code="accepted",
        )
        self.assertEqual(first.pk, replay.pk)
        with self.assertRaises(IdempotencyConflict):
            apply_provider_request_result(
                transaction_id=transaction_obj.id,
                claim_token=claim.claim.claim_token,
                outcome=ProviderRequestOutcome.ACCEPTED_PENDING,
                evidence_hash="9" * 64,
                result_idempotency_key=key,
                reason_code="different",
            )

    def test_customer_action_and_pending_results_never_mark_success(self):
        for outcome, tx_status, attempt_status in (
            (
                ProviderRequestOutcome.CUSTOMER_ACTION_REQUIRED,
                PaymentTransactionStatus.PENDING_CUSTOMER,
                PaymentAttemptStatus.REQUIRES_CUSTOMER_ACTION,
            ),
            (
                ProviderRequestOutcome.ACCEPTED_PENDING,
                PaymentTransactionStatus.PENDING_PROVIDER,
                PaymentAttemptStatus.PROCESSING,
            ),
        ):
            _, _, attempt, transaction_obj = self.make_request_graph()
            claim = claim_provider_request(transaction_id=transaction_obj.id, claim_idempotency_key=uuid4())
            apply_provider_request_result(
                transaction_id=transaction_obj.id,
                claim_token=claim.claim.claim_token,
                outcome=outcome,
                evidence_hash="b" * 64,
                result_idempotency_key=uuid4(),
            )
            transaction_obj.refresh_from_db()
            attempt.refresh_from_db()
            self.assertEqual((transaction_obj.status, attempt.status), (tx_status, attempt_status))

    def test_definitive_decline_reopens_payment_and_retry_gets_new_sequence(self):
        placement, account, attempt, transaction_obj = self.make_request_graph()
        claim = claim_provider_request(transaction_id=transaction_obj.id, claim_idempotency_key=uuid4())
        apply_provider_request_result(
            transaction_id=transaction_obj.id,
            claim_token=claim.claim.claim_token,
            outcome=ProviderRequestOutcome.CONFIRMED_DECLINE,
            evidence_hash="c" * 64,
            result_idempotency_key=uuid4(),
        )
        attempt.refresh_from_db()
        placement.payment.refresh_from_db()
        self.assertEqual(attempt.status, PaymentAttemptStatus.DEFINITIVE_FAILED)
        self.assertEqual(placement.payment.collection_status, PaymentCollectionStatus.OPEN)
        retry = create_or_replay_payment_attempt(
            payment_id=placement.payment.id,
            merchant_account_version_id=account.id,
            tender_type=PaymentTenderType.EXTERNAL_PROVIDER,
            requested_amount=placement.payment.amount_due,
            idempotency_key=uuid4(),
        ).attempt
        self.assertEqual(retry.sequence, 2)

    def test_no_effect_retryable_releases_same_transaction_for_new_claim(self):
        _, _, attempt, transaction_obj = self.make_request_graph()
        first = claim_provider_request(transaction_id=transaction_obj.id, claim_idempotency_key=uuid4())
        apply_provider_request_result(
            transaction_id=transaction_obj.id,
            claim_token=first.claim.claim_token,
            outcome=ProviderRequestOutcome.NO_EFFECT_RETRYABLE,
            evidence_hash="d" * 64,
            result_idempotency_key=uuid4(),
        )
        transaction_obj.refresh_from_db()
        attempt.refresh_from_db()
        self.assertEqual(transaction_obj.status, PaymentTransactionStatus.CREATED)
        self.assertEqual(attempt.status, PaymentAttemptStatus.PROCESSING)
        second = claim_provider_request(transaction_id=transaction_obj.id, claim_idempotency_key=uuid4())
        self.assertNotEqual(first.claim.claim_token, second.claim.claim_token)

    def test_unknown_outcome_blocks_retry_and_opens_review(self):
        placement, account, attempt, transaction_obj = self.make_request_graph()
        claim = claim_provider_request(transaction_id=transaction_obj.id, claim_idempotency_key=uuid4())
        apply_provider_request_result(
            transaction_id=transaction_obj.id,
            claim_token=claim.claim.claim_token,
            outcome=ProviderRequestOutcome.OUTCOME_UNKNOWN,
            evidence_hash="e" * 64,
            result_idempotency_key=uuid4(),
            reason_code="possible_external_effect",
        )
        placement.payment.refresh_from_db()
        attempt.refresh_from_db()
        self.assertEqual(placement.payment.collection_status, PaymentCollectionStatus.REVIEW)
        self.assertEqual(attempt.status, PaymentAttemptStatus.OUTCOME_UNKNOWN)
        self.assertTrue(ReviewCase.objects.filter(transaction=transaction_obj).exists())
        with self.assertRaises(CollectionBlocked):
            create_or_replay_payment_attempt(
                payment_id=placement.payment.id,
                merchant_account_version_id=account.id,
                tender_type=PaymentTenderType.EXTERNAL_PROVIDER,
                requested_amount=placement.payment.amount_due,
                idempotency_key=uuid4(),
            )

    def test_stale_result_and_confirmed_success_are_rejected(self):
        _, _, _, transaction_obj = self.make_request_graph()
        claim = claim_provider_request(transaction_id=transaction_obj.id, claim_idempotency_key=uuid4())
        with self.assertRaises(StaleRequestClaim):
            apply_provider_request_result(
                transaction_id=transaction_obj.id,
                claim_token=uuid4(),
                outcome=ProviderRequestOutcome.ACCEPTED_PENDING,
                evidence_hash="f" * 64,
                result_idempotency_key=uuid4(),
            )
        with self.assertRaises(ValidationError):
            apply_provider_request_result(
                transaction_id=transaction_obj.id,
                claim_token=claim.claim.claim_token,
                outcome=ProviderRequestOutcome.CONFIRMED_SUCCESS,
                evidence_hash="f" * 64,
                result_idempotency_key=uuid4(),
            )

    def test_raw_sql_cannot_forge_success_or_mutate_provider_identity(self):
        _, _, attempt, transaction_obj = self.make_request_graph()
        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE financial_core_paymenttransaction SET status='succeeded', completed_at=NOW() WHERE id=%s",
                [transaction_obj.id],
            )
        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE financial_core_paymentattempt SET status='succeeded' WHERE id=%s",
                [attempt.id],
            )
        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE financial_core_paymenttransaction SET merchant_reference='forged' WHERE id=%s",
                [transaction_obj.id],
            )

    def test_database_rejects_versioned_rows_inserted_as_successful(self):
        _, _, _, _, placement = self.place_standard()
        _, capability, account = self.make_account()
        with self.assertRaises(DatabaseError), transaction.atomic():
            PaymentAttempt.objects.create(
                payment=placement.payment,
                sequence=1,
                requested_amount=placement.payment.amount_due,
                currency=MoneyUnit.IRR,
                tender_type=PaymentTenderType.EXTERNAL_PROVIDER,
                provider=account.provider.key,
                merchant_account_ref=account.account_key,
                capability_version=capability,
                merchant_account_version=account,
                status=PaymentAttemptStatus.SUCCEEDED,
                idempotency_key=uuid4(),
                request_hash="8" * 64,
            )

        attempt = create_or_replay_payment_attempt(
            payment_id=placement.payment.id,
            merchant_account_version_id=account.id,
            tender_type=PaymentTenderType.EXTERNAL_PROVIDER,
            requested_amount=placement.payment.amount_due,
            idempotency_key=uuid4(),
        ).attempt
        with self.assertRaises(DatabaseError), transaction.atomic():
            PaymentTransaction.objects.create(
                attempt=attempt,
                sequence=1,
                operation_type=PaymentTransactionOperation.SALE,
                provider=account.provider.key,
                merchant_account_ref=account.account_key,
                capability_version=capability,
                merchant_account_version=account,
                adapter_contract_version=capability.adapter_contract_version,
                merchant_reference=f"forged-success-{uuid4()}",
                amount=attempt.requested_amount,
                currency=MoneyUnit.IRR,
                provider_amount=attempt.requested_amount,
                provider_unit=MoneyUnit.IRR,
                provider_conversion_policy_version=capability.conversion_policy_version,
                provider_idempotency_reference=f"forged-{uuid4()}",
                request_fingerprint="7" * 64,
                status=PaymentTransactionStatus.SUCCEEDED,
                completed_at=timezone.now(),
                idempotency_key=uuid4(),
            )

    def test_adapter_registry_is_allowlisted_and_contract_conformant(self):
        adapter = SyntheticAdapter()
        self.assertTrue(assert_adapter_conformance(adapter))
        registry = ProviderAdapterRegistry({("synthetic", ADAPTER_CONTRACT_VERSION): adapter})
        self.assertIs(registry.resolve(adapter_key="synthetic", contract_version=ADAPTER_CONTRACT_VERSION), adapter)
        with self.assertRaises(ValidationError):
            ProviderAdapterRegistry().resolve(
                adapter_key="arbitrary.module.Adapter",
                contract_version=ADAPTER_CONTRACT_VERSION,
            )

    def test_outbox_is_dormant_safe_and_contains_no_credentials(self):
        _, _, _, transaction_obj = self.make_request_graph()
        claim_provider_request(transaction_id=transaction_obj.id, claim_idempotency_key=uuid4())
        messages = list(FinancialOutboxMessage.objects.values_list("safe_payload", flat=True))
        self.assertGreaterEqual(len(messages), 2)
        serialized = str(messages).lower()
        self.assertNotIn("credential", serialized)
        self.assertNotIn("token", serialized)
        self.assertNotIn("env://", serialized)


class C2AConcurrencyTests(C2AFixture, TransactionTestCase):
    reset_sequences = True

    def _run_threads(self, functions):
        barrier = Barrier(len(functions))
        results = []

        def runner(index, function):
            close_old_connections()
            try:
                barrier.wait()
                results.append((index, "ok", function()))
            except Exception as exc:
                results.append((index, "error", exc))
            finally:
                close_old_connections()

        threads = [Thread(target=runner, args=(index, function)) for index, function in enumerate(functions)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        return results

    def test_concurrent_placement_creates_one_order_and_payment(self):
        user, _, _, checkout = self.make_standard_checkout()
        key = uuid4()

        def place():
            result = place_order_and_create_payment_obligation(
                checkout_id=checkout.id,
                expected_user_id=user.id,
                expected_checkout_version=checkout.version,
                source_unit=MoneyUnit.IRR,
                idempotency_key=key,
            )
            return result.payment.id

        results = self._run_threads([place, place])
        self.assertEqual(sum(status == "ok" for _, status, _ in results), 2)
        self.assertEqual((Order.objects.count(), Payment.objects.count()), (1, 1))

    def test_concurrent_attempt_creation_has_one_winner(self):
        _, _, _, _, placement = self.place_standard()
        _, _, account = self.make_account()

        def create_attempt():
            return create_or_replay_payment_attempt(
                payment_id=placement.payment.id,
                merchant_account_version_id=account.id,
                tender_type=PaymentTenderType.EXTERNAL_PROVIDER,
                requested_amount=placement.payment.amount_due,
                idempotency_key=uuid4(),
            ).attempt.id

        results = self._run_threads([create_attempt, create_attempt])
        self.assertEqual(sum(status == "ok" for _, status, _ in results), 1)
        self.assertEqual(PaymentAttempt.objects.count(), 1)

    def test_concurrent_request_claim_has_one_winner(self):
        _, _, _, transaction_obj = self.make_request_graph()

        def claim():
            return claim_provider_request(
                transaction_id=transaction_obj.id,
                claim_idempotency_key=uuid4(),
            ).claim.id

        results = self._run_threads([claim, claim])
        self.assertEqual(sum(status == "ok" for _, status, _ in results), 1)
        self.assertEqual(transaction_obj.request_claims.count(), 1)

    def test_request_claim_serializes_with_request_result(self):
        _, _, _, transaction_obj = self.make_request_graph()
        first = claim_provider_request(
            transaction_id=transaction_obj.id,
            claim_idempotency_key=uuid4(),
        )

        def apply_result():
            return apply_provider_request_result(
                transaction_id=transaction_obj.id,
                claim_token=first.claim.claim_token,
                outcome=ProviderRequestOutcome.NO_EFFECT_RETRYABLE,
                evidence_hash="6" * 64,
                result_idempotency_key=uuid4(),
            ).id

        def claim_again():
            return claim_provider_request(
                transaction_id=transaction_obj.id,
                claim_idempotency_key=uuid4(),
            ).claim.id

        results = self._run_threads([apply_result, claim_again])
        self.assertGreaterEqual(sum(status == "ok" for _, status, _ in results), 1)
        transaction_obj.refresh_from_db()
        self.assertIn(
            transaction_obj.status,
            (PaymentTransactionStatus.CREATED, PaymentTransactionStatus.REQUESTING),
        )
        self.assertEqual(transaction_obj.request_results.count(), 1)

    def test_unknown_transition_serializes_with_retry(self):
        placement, account, _, transaction_obj = self.make_request_graph()
        claim = claim_provider_request(
            transaction_id=transaction_obj.id,
            claim_idempotency_key=uuid4(),
        )

        def apply_unknown():
            return apply_provider_request_result(
                transaction_id=transaction_obj.id,
                claim_token=claim.claim.claim_token,
                outcome=ProviderRequestOutcome.OUTCOME_UNKNOWN,
                evidence_hash="5" * 64,
                result_idempotency_key=uuid4(),
            ).id

        def retry():
            return create_or_replay_payment_attempt(
                payment_id=placement.payment.id,
                merchant_account_version_id=account.id,
                tender_type=PaymentTenderType.EXTERNAL_PROVIDER,
                requested_amount=placement.payment.amount_due,
                idempotency_key=uuid4(),
            ).attempt.id

        results = self._run_threads([apply_unknown, retry])
        self.assertEqual(sum(status == "ok" for _, status, _ in results), 1)
        placement.payment.refresh_from_db()
        self.assertEqual(placement.payment.collection_status, PaymentCollectionStatus.REVIEW)
        self.assertEqual(PaymentAttempt.objects.filter(payment=placement.payment).count(), 1)
        self.assertTrue(ReviewCase.objects.filter(transaction=transaction_obj).exists())

    def test_attempt_creation_serializes_with_review_creation(self):
        _, _, _, _, placement = self.place_standard()
        _, _, account = self.make_account()

        def create_attempt():
            return create_or_replay_payment_attempt(
                payment_id=placement.payment.id,
                merchant_account_version_id=account.id,
                tender_type=PaymentTenderType.EXTERNAL_PROVIDER,
                requested_amount=placement.payment.amount_due,
                idempotency_key=uuid4(),
            ).attempt.id

        def create_review():
            return open_review_case(
                reason=ReviewCaseReason.PROVIDER_STATE_UNCLEAR,
                severity=ReviewCaseSeverity.HIGH,
                summary="Synthetic concurrency review",
                idempotency_key=uuid4(),
                command_key=f"concurrency-review:{uuid4()}",
                payment_id=placement.payment.id,
            ).id

        results = self._run_threads([create_attempt, create_review])
        self.assertEqual(ReviewCase.objects.count(), 1)
        self.assertLessEqual(PaymentAttempt.objects.count(), 1)
        if PaymentAttempt.objects.exists():
            with self.assertRaises(CollectionBlocked):
                create_or_replay_request_transaction(
                    attempt_id=PaymentAttempt.objects.get().id,
                    operation_type=PaymentTransactionOperation.SALE,
                    idempotency_key=uuid4(),
                )
        self.assertGreaterEqual(sum(status == "ok" for _, status, _ in results), 1)

    def test_legacy_adoption_serializes_with_legacy_payment_activity(self):
        user = self.make_user()
        order = Order.objects.create(
            user=user,
            payment_status=OrderStatus.PENDDING,
            total_price=500,
            total_price_discount=500,
        )

        def adopt():
            return adopt_legacy_order_payment_obligation(
                order_id=order.id,
                expected_user_id=user.id,
                source_unit=MoneyUnit.IRT,
                idempotency_key=uuid4(),
                legacy_owner_inactive=True,
                ownership_evidence_reference="concurrency-gate",
            ).id

        def legacy_activity():
            with transaction.atomic():
                locked = Order.objects.select_for_update().get(pk=order.id)
                if hasattr(locked, "financial_payment"):
                    return "financial-core-owned"
                return LegacyPaymentTransaction.objects.create(
                    order=locked,
                    user=user,
                    amount=500,
                    status=LegacyPaymentTransactionStatus.PENDING,
                    idempotency_key=f"legacy-race:{uuid4()}",
                ).id

        self._run_threads([adopt, legacy_activity])
        self.assertFalse(
            Payment.objects.filter(order=order).exists()
            and LegacyPaymentTransaction.objects.filter(order=order).exists()
        )
