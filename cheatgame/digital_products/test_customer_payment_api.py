from dataclasses import FrozenInstanceError
from decimal import Decimal
from threading import Barrier, Lock, Thread
from unittest.mock import patch
from uuid import uuid4

from django.db import close_old_connections, connection
from django.test import TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from drf_spectacular.generators import SchemaGenerator
from rest_framework.test import APIClient

from cheatgame.digital_products import customer_payment_apis
from cheatgame.digital_products.customer_payment_apis import CustomerDigitalPaymentRequestApi
from cheatgame.digital_products.models import (
    DigitalCartFulfillmentMethod,
    DigitalFulfillmentItem,
    DigitalInventoryReservation,
    DigitalInventoryReservationState,
    DigitalOffer,
    DigitalOfferCapacity,
    DigitalOfferSaleState,
    Entitlement,
    InventoryPool,
    InventoryPoolStatus,
)
from cheatgame.digital_products.services.cart import add_digital_offer_to_cart
from cheatgame.digital_products.services.checkout_preparation import prepare_digital_checkout
from cheatgame.digital_products.services.payment_adapter import request_digital_checkout_payment
from cheatgame.financial_core.models import (
    CommercialFinalization,
    DigitalFulfillmentObligation,
    FinancialAllocation,
    IdempotencyRecord,
    IdempotencyStatus,
    JournalEntry,
    MerchantAccountVersion,
    MoneyUnit,
    Payment,
    PaymentAttempt,
    PaymentAttemptStatus,
    PaymentTransaction,
    PaymentTransactionOperation,
    PaymentTransactionStatus,
    ProviderCapabilityVersion,
    ProviderDefinition,
    ProviderRequestClaim,
    ProviderRequestOutcome,
    ProviderRequestResult,
    ReviewCase,
)
from cheatgame.financial_core.services.adapters import (
    ADAPTER_CONTRACT_VERSION,
    NormalizedProviderResult,
    ProviderAdapterRegistry,
)
from cheatgame.product.models import (
    DeliveredVersion,
    NativeConsole,
    Product,
    ProductCommerceAuthority,
    ProductStatus,
    ProductType,
)
from cheatgame.shop.models import (
    Cart,
    CartItem,
    Checkout,
    CheckoutStatus,
    Order,
    OrderItem,
    PaymentTransaction as LegacyPayment,
)
from cheatgame.users.models import BaseUser, UserTypes


class SyntheticPaymentAdapter:
    adapter_key = "synthetic"
    contract_version = ADAPTER_CONTRACT_VERSION

    def __init__(self, result=None, error=None):
        self.result = result or NormalizedProviderResult(
            outcome=ProviderRequestOutcome.CUSTOMER_ACTION_REQUIRED,
            evidence_hash="a" * 64,
            reason_code="customer_action",
            safe_metadata={"result_code": "100"},
            customer_action_url="https://pay.test/authority-1",
        )
        self.error = error
        self.envelopes = []
        self.atomic_states = []

    def execute_operation(self, envelope):
        self.envelopes.append(envelope)
        self.atomic_states.append(connection.in_atomic_block)
        if self.error:
            raise self.error
        return self.result

    def authenticate_callback(self, *, headers, body):
        raise AssertionError("callback is outside API-04")

    def normalize_callback(self, authenticated_callback):
        raise AssertionError("callback is outside API-04")

    def verify_operation(self, envelope):
        raise AssertionError("verification is outside API-04")

    def query_operation(self, envelope):
        raise AssertionError("polling is outside API-04")

    def read_reconciliation_records(self, *, period_start, period_end):
        return ()


@override_settings(FINANCIAL_PROVIDER_CUSTOMER_ACTION_HOSTS={"synthetic-pay": ["pay.test"]})
class CustomerDigitalPaymentApiTests(TransactionTestCase):
    def setUp(self):
        self.client = APIClient()
        self.customer = self.make_user("09127770001")
        self.client.force_authenticate(self.customer)
        self.product = Product.objects.create(
            product_type=ProductType.GAME,
            commerce_authority=ProductCommerceAuthority.DIGITAL_PRODUCTS,
            status=ProductStatus.PUBLISHED,
            title="Payment Adapter Game",
            slug="payment-adapter-game",
            main_image="tests/payment.png",
            description="tests/payment.html",
            price=Decimal("999999"),
            off_price=Decimal("999999"),
            quantity=999,
            order_limit=5,
        )
        self.version = DeliveredVersion.objects.create(product=self.product, native_console=NativeConsole.PS4)
        self.pool = InventoryPool.objects.create(sellable_quantity=4, status=InventoryPoolStatus.ENABLED)
        self.offer = DigitalOffer.objects.create(
            delivered_version=self.version,
            customer_console=NativeConsole.PS5,
            capacity=DigitalOfferCapacity.CAPACITY_2,
            price=Decimal("450000"),
            inventory_pool=self.pool,
            sale_state=DigitalOfferSaleState.ACTIVE,
        )
        self.cart = Cart.objects.create(user=self.customer)
        add_digital_offer_to_cart(
            cart=self.cart,
            offer=self.offer,
            fulfillment_method=DigitalCartFulfillmentMethod.REMOTE,
            actor=self.customer,
        )
        self.checkout = prepare_digital_checkout(actor=self.customer, client_checkout_uuid=uuid4())[0]
        self.provider = ProviderDefinition.objects.create(
            key="synthetic-pay",
            display_name="Synthetic Payment",
            is_enabled=True,
            new_requests_enabled=True,
        )
        self.capability = ProviderCapabilityVersion.objects.create(
            provider=self.provider,
            version=1,
            adapter_key="synthetic",
            adapter_contract_version=ADAPTER_CONTRACT_VERSION,
            provider_unit=MoneyUnit.IRR,
            conversion_policy_version="irr-exact-v1",
            supported_operations=[PaymentTransactionOperation.SALE],
            supports_request_idempotency=True,
            supports_lookup=True,
            finality_window_seconds=3600,
            authority_expiry_seconds=900,
        )
        self.account = MerchantAccountVersion.objects.create(
            provider=self.provider,
            capability_version=self.capability,
            account_key="primary",
            version=1,
            owner_key="cheats-game",
            credential_reference="env://SYNTHETIC_PAYMENT_CREDENTIAL",
            is_enabled=True,
            new_requests_enabled=True,
        )
        self.adapter = SyntheticPaymentAdapter()
        self.registry = ProviderAdapterRegistry({("synthetic", ADAPTER_CONTRACT_VERSION): self.adapter})
        self.registry_patch = patch.object(CustomerDigitalPaymentRequestApi, "adapter_registry", self.registry)
        self.registry_patch.start()
        self.addCleanup(self.registry_patch.stop)

    def make_user(self, phone, *, user_type=UserTypes.CUSTOMER, verified=True):
        user = BaseUser.objects.create_user(
            phone_number=phone,
            firstname="Payment",
            lastname="Customer",
            password="test-only",
            user_type=user_type,
        )
        user.phone_verified = verified
        user.save(update_fields=("phone_verified", "updated_at"))
        return user

    def make_checkout_for(self, customer):
        cart = Cart.objects.create(user=customer)
        add_digital_offer_to_cart(
            cart=cart,
            offer=self.offer,
            fulfillment_method=DigitalCartFulfillmentMethod.REMOTE,
            actor=customer,
        )
        return prepare_digital_checkout(actor=customer, client_checkout_uuid=uuid4())[0]

    def request_for(self, *, customer, checkout, key, provider="synthetic-pay"):
        client = APIClient()
        client.force_authenticate(customer)
        return client.post(
            f"/api/digital-products/customer/checkout/{checkout.public_id}/payment/request/",
            {"provider": provider},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(key),
        )

    @property
    def request_url(self):
        return f"/api/digital-products/customer/checkout/{self.checkout.public_id}/payment/request/"

    @property
    def status_url(self):
        return f"/api/digital-products/customer/checkout/{self.checkout.public_id}/payment/"

    def request(self, key=None, **payload):
        return self.client.post(
            self.request_url,
            {"provider": "synthetic-pay", **payload},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(key or uuid4()),
        )

    def root_identity(self, key):
        root = IdempotencyRecord.objects.get(
            scope=f"digital_api:payment_request:{self.checkout.pk}",
            key=str(key),
        )
        return root.safe_response["request_identity"]

    def test_request_places_checkout_and_persists_customer_action(self):
        response = self.request()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["customer_action_url"], "https://pay.test/authority-1")
        self.assertEqual((Order.objects.count(), OrderItem.objects.count()), (1, 1))
        self.assertEqual((Payment.objects.count(), PaymentAttempt.objects.count()), (1, 1))
        self.assertEqual((PaymentTransaction.objects.count(), ProviderRequestResult.objects.count()), (1, 1))
        attempt = PaymentAttempt.objects.get()
        transaction_obj = PaymentTransaction.objects.get()
        self.assertEqual(attempt.status, PaymentAttemptStatus.REQUIRES_CUSTOMER_ACTION)
        self.assertEqual(transaction_obj.status, PaymentTransactionStatus.PENDING_CUSTOMER)
        self.assertEqual(self.adapter.atomic_states, [False])
        self.assertEqual(self.adapter.envelopes[0].canonical_amount, "4500000")
        self.assertEqual(self.adapter.envelopes[0].canonical_currency, "IRR")
        self.assertEqual(self.adapter.envelopes[0].provider_capability_version, 1)
        with self.assertRaises(FrozenInstanceError):
            self.adapter.envelopes[0].canonical_amount = "1"

    def test_identical_root_replay_does_not_repeat_provider_io(self):
        key = uuid4()
        first = self.request(key)
        second = self.request(key)
        self.assertEqual((first.status_code, second.status_code), (201, 200))
        self.assertTrue(second.data["replayed"])
        self.assertEqual(second.data["customer_action_url"], first.data["customer_action_url"])
        self.assertEqual(len(self.adapter.envelopes), 1)
        self.assertEqual((Order.objects.count(), PaymentAttempt.objects.count(), PaymentTransaction.objects.count()), (1, 1, 1))

    def test_root_key_conflicting_payload_is_rejected(self):
        key = uuid4()
        self.assertEqual(self.request(key).status_code, 201)
        conflict = self.request(key, provider="different-provider")
        self.assertEqual((conflict.status_code, conflict.data["code"]), (409, "payment_request_conflict"))
        self.assertEqual(len(self.adapter.envelopes), 1)

    def test_competing_key_is_blocked_by_one_blocking_attempt(self):
        self.assertEqual(self.request(uuid4()).status_code, 201)
        blocked = self.request(uuid4())
        self.assertEqual((blocked.status_code, blocked.data["code"]), (409, "payment_request_conflict"))
        self.assertEqual((PaymentAttempt.objects.count(), PaymentTransaction.objects.count()), (1, 1))

    def test_concurrent_duplicate_click_has_one_blocking_attempt_and_one_provider_call(self):
        request_barrier = Barrier(2)
        insert_barrier = Barrier(2)
        insert_lock = Lock()
        insert_calls = 0
        synchronized_scopes = []
        result_lock = Lock()
        results = []
        captured_errors = []
        key = uuid4()
        original_domain_error = customer_payment_apis._domain_error
        original_root_create = IdempotencyRecord.objects.create

        def synchronized_root_create(*args, **kwargs):
            nonlocal insert_calls
            with insert_lock:
                insert_calls += 1
                synchronize = insert_calls <= 2
                if synchronize:
                    synchronized_scopes.append(kwargs.get("scope"))
            if synchronize:
                insert_barrier.wait(timeout=10)
            return original_root_create(*args, **kwargs)

        def capture_domain_error(exc):
            with result_lock:
                captured_errors.append((type(exc).__name__, str(exc)))
            return original_domain_error(exc)

        def invoke():
            close_old_connections()
            client = APIClient()
            client.force_authenticate(self.customer)
            request_barrier.wait(timeout=10)
            response = client.post(
                self.request_url,
                {"provider": "synthetic-pay"},
                format="json",
                HTTP_IDEMPOTENCY_KEY=str(key),
            )
            with result_lock:
                results.append((response.status_code, response.data.get("code"), response.data))
            close_old_connections()

        with patch.object(
            IdempotencyRecord.objects,
            "create",
            side_effect=synchronized_root_create,
        ), patch.object(customer_payment_apis, "_domain_error", side_effect=capture_domain_error):
            threads = [Thread(target=invoke), Thread(target=invoke)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=20)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(results), 2)
        expected_scope = f"digital_api:payment_request:{self.checkout.pk}"
        self.assertGreaterEqual(insert_calls, 2)
        self.assertEqual(synchronized_scopes, [expected_scope, expected_scope])
        self.assertEqual(sum(status_code == 201 for status_code, _, _ in results), 1, results)
        losing = next(result for result in results if result[0] != 201)
        self.assertIn(losing[0], (200, 409), (results, captured_errors))
        if losing[0] == 200:
            self.assertTrue(losing[2]["replayed"])
        else:
            self.assertIn(losing[1], ("payment_request_in_progress", "payment_request_conflict"))
        self.assertNotIn(400, [status_code for status_code, _, _ in results])
        self.assertEqual(
            (
                Order.objects.count(),
                Payment.objects.count(),
                PaymentAttempt.objects.count(),
                PaymentTransaction.objects.count(),
                ProviderRequestClaim.objects.count(),
            ),
            (1, 1, 1, 1, 1),
        )
        self.assertEqual(
            IdempotencyRecord.objects.filter(
                scope=f"digital_api:payment_request:{self.checkout.pk}", key=str(key)
            ).count(),
            1,
        )
        self.assertEqual(len(self.adapter.envelopes), 1)

    def test_same_client_uuid_is_domain_separated_across_customers(self):
        other = self.make_user("09127770005")
        other_checkout = self.make_checkout_for(other)
        key = uuid4()

        first = self.request_for(customer=self.customer, checkout=self.checkout, key=key)
        second = self.request_for(customer=other, checkout=other_checkout, key=key)

        self.assertEqual((first.status_code, second.status_code), (201, 201))
        self.assertEqual((Order.objects.count(), PaymentAttempt.objects.count()), (2, 2))
        self.assertEqual(len(set(PaymentAttempt.objects.values_list("idempotency_key", flat=True))), 2)
        self.assertEqual(len(self.adapter.envelopes), 2)

    def test_same_client_uuid_is_domain_separated_across_checkouts(self):
        other = self.make_user("09127770006")
        other_checkout = self.make_checkout_for(other)
        key = uuid4()

        self.assertNotEqual(self.checkout.public_id, other_checkout.public_id)
        self.assertEqual(self.request_for(customer=self.customer, checkout=self.checkout, key=key).status_code, 201)
        self.assertEqual(self.request_for(customer=other, checkout=other_checkout, key=key).status_code, 201)
        roots = IdempotencyRecord.objects.filter(scope__startswith="digital_api:payment_request:")
        self.assertEqual(roots.count(), 2)
        self.assertEqual(len({root.request_hash for root in roots}), 2)

    def test_completed_replay_precedes_later_checkout_state_validation(self):
        key = uuid4()
        self.assertEqual(self.request(key).status_code, 201)
        Checkout.objects.filter(pk=self.checkout.pk).update(status=CheckoutStatus.PAID)

        replay = self.request(key)

        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertTrue(replay.data["replayed"])
        self.assertEqual(len(self.adapter.envelopes), 1)

    def test_crash_after_placement_resumes_attempt_and_provider_request(self):
        key = uuid4()
        with patch(
            "cheatgame.digital_products.services.payment_adapter.create_or_replay_payment_attempt",
            side_effect=RuntimeError("crash after placement"),
        ):
            with self.assertRaises(RuntimeError):
                request_digital_checkout_payment(
                    checkout_public_id=self.checkout.public_id,
                    actor=self.customer,
                    provider="synthetic-pay",
                    idempotency_key=key,
                    adapter_registry=self.registry,
                )
        self.assertEqual((Order.objects.count(), PaymentAttempt.objects.count()), (1, 0))

        resumed = self.request(key)

        self.assertEqual(resumed.status_code, 201, resumed.data)
        self.assertEqual((Order.objects.count(), PaymentAttempt.objects.count()), (1, 1))
        self.assertEqual(len(self.adapter.envelopes), 1)

    def test_crash_after_attempt_resumes_transaction_and_provider_request(self):
        key = uuid4()
        with patch(
            "cheatgame.digital_products.services.payment_adapter.create_or_replay_request_transaction",
            side_effect=RuntimeError("crash after attempt"),
        ):
            with self.assertRaises(RuntimeError):
                request_digital_checkout_payment(
                    checkout_public_id=self.checkout.public_id,
                    actor=self.customer,
                    provider="synthetic-pay",
                    idempotency_key=key,
                    adapter_registry=self.registry,
                )
        self.assertEqual((PaymentAttempt.objects.count(), PaymentTransaction.objects.count()), (1, 0))

        resumed = self.request(key)

        self.assertEqual(resumed.status_code, 201, resumed.data)
        self.assertEqual((PaymentAttempt.objects.count(), PaymentTransaction.objects.count()), (1, 1))
        self.assertEqual(len(self.adapter.envelopes), 1)

    def test_crash_after_transaction_resumes_claim_and_provider_request(self):
        key = uuid4()
        with patch(
            "cheatgame.digital_products.services.payment_adapter.claim_provider_request",
            side_effect=RuntimeError("crash after transaction"),
        ):
            with self.assertRaises(RuntimeError):
                request_digital_checkout_payment(
                    checkout_public_id=self.checkout.public_id,
                    actor=self.customer,
                    provider="synthetic-pay",
                    idempotency_key=key,
                    adapter_registry=self.registry,
                )
        self.assertEqual(PaymentTransaction.objects.get().status, PaymentTransactionStatus.CREATED)

        resumed = self.request(key)

        self.assertEqual(resumed.status_code, 201, resumed.data)
        self.assertEqual(PaymentTransaction.objects.count(), 1)
        self.assertEqual(len(self.adapter.envelopes), 1)

    def test_crash_after_claim_never_reissues_provider_request(self):
        key = uuid4()
        with patch(
            "cheatgame.digital_products.services.payment_adapter._execute_provider",
            side_effect=RuntimeError("crash after claim"),
        ):
            with self.assertRaises(RuntimeError):
                request_digital_checkout_payment(
                    checkout_public_id=self.checkout.public_id,
                    actor=self.customer,
                    provider="synthetic-pay",
                    idempotency_key=key,
                    adapter_registry=self.registry,
                )
        self.assertEqual(PaymentTransaction.objects.get().status, PaymentTransactionStatus.REQUESTING)

        replay = self.request(key)
        status_response = self.client.get(self.status_url)

        self.assertEqual((replay.status_code, replay.data["code"]), (409, "payment_request_in_progress"))
        self.assertTrue(status_response.data["do_not_pay_again"])
        self.assertEqual(len(self.adapter.envelopes), 0)

    def test_concurrent_different_keys_have_one_provider_owner(self):
        barrier = Barrier(2)
        result_lock = Lock()
        results = []

        def invoke(key):
            close_old_connections()
            client = APIClient()
            client.force_authenticate(self.customer)
            barrier.wait()
            response = client.post(
                self.request_url,
                {"provider": "synthetic-pay"},
                format="json",
                HTTP_IDEMPOTENCY_KEY=str(key),
            )
            with result_lock:
                results.append((response.status_code, response.data.get("code")))
            close_old_connections()

        threads = [Thread(target=invoke, args=(uuid4(),)), Thread(target=invoke, args=(uuid4(),))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(sorted(status_code for status_code, _ in results), [201, 409])
        self.assertEqual((Order.objects.count(), PaymentAttempt.objects.count(), PaymentTransaction.objects.count()), (1, 1, 1))
        self.assertEqual(len(self.adapter.envelopes), 1)

    def test_provider_policy_identity_change_fails_closed(self):
        key = uuid4()
        with patch(
            "cheatgame.digital_products.services.payment_adapter.create_or_replay_payment_attempt",
            side_effect=RuntimeError("pause after frozen root"),
        ):
            with self.assertRaises(RuntimeError):
                request_digital_checkout_payment(
                    checkout_public_id=self.checkout.public_id,
                    actor=self.customer,
                    provider="synthetic-pay",
                    idempotency_key=key,
                    adapter_registry=self.registry,
                )
        self.account.is_enabled = False
        self.account.new_requests_enabled = False
        self.account.save(update_fields=("is_enabled", "new_requests_enabled", "updated_at"))
        MerchantAccountVersion.objects.create(
            provider=self.provider,
            capability_version=self.capability,
            account_key="replacement",
            version=2,
            owner_key="cheats-game",
            credential_reference="env://SYNTHETIC_PAYMENT_CREDENTIAL_V2",
            is_enabled=True,
            new_requests_enabled=True,
        )

        retry = self.request(key)

        self.assertEqual((retry.status_code, retry.data["code"]), (409, "payment_request_conflict"))
        self.assertEqual(PaymentAttempt.objects.count(), 0)
        self.assertEqual(len(self.adapter.envelopes), 0)

    def test_definitive_failure_allows_new_attempt(self):
        self.adapter.result = NormalizedProviderResult(
            outcome=ProviderRequestOutcome.CONFIRMED_DECLINE,
            evidence_hash="b" * 64,
            reason_code="declined",
        )
        first = self.request(uuid4())
        self.assertEqual(first.status_code, 201)
        self.assertEqual(PaymentAttempt.objects.get().status, PaymentAttemptStatus.DEFINITIVE_FAILED)
        self.adapter.result = NormalizedProviderResult(
            outcome=ProviderRequestOutcome.CUSTOMER_ACTION_REQUIRED,
            evidence_hash="c" * 64,
            customer_action_url="https://pay.test/authority-2",
        )
        second = self.request(uuid4())
        self.assertEqual(second.status_code, 201, second.data)
        self.assertEqual(PaymentAttempt.objects.count(), 2)

    def test_retry_root_preserves_exact_preplacement_checkout_version(self):
        Checkout.objects.filter(pk=self.checkout.pk).update(version=3)
        self.checkout.refresh_from_db()
        first_key = uuid4()
        self.adapter.result = NormalizedProviderResult(
            outcome=ProviderRequestOutcome.CONFIRMED_DECLINE,
            evidence_hash="1" * 64,
            reason_code="declined",
        )
        self.assertEqual(self.request(first_key).status_code, 201)
        self.checkout.refresh_from_db()
        self.assertEqual(self.checkout.version, 4)

        retry_key = uuid4()
        self.adapter.result = NormalizedProviderResult(
            outcome=ProviderRequestOutcome.CUSTOMER_ACTION_REQUIRED,
            evidence_hash="2" * 64,
            customer_action_url="https://pay.test/version-preserved",
        )
        self.assertEqual(self.request(retry_key).status_code, 201)
        self.assertEqual(self.root_identity(first_key)["placement_checkout_version"], 3)
        self.assertEqual(self.root_identity(retry_key)["placement_checkout_version"], 3)

    def test_definitive_failure_retry_root_reuses_original_placement_identity(self):
        first_key = uuid4()
        self.adapter.result = NormalizedProviderResult(
            outcome=ProviderRequestOutcome.CONFIRMED_DECLINE,
            evidence_hash="3" * 64,
            reason_code="declined",
        )
        self.assertEqual(self.request(first_key).status_code, 201)
        original = self.root_identity(first_key)

        retry_key = uuid4()
        self.adapter.result = NormalizedProviderResult(
            outcome=ProviderRequestOutcome.CUSTOMER_ACTION_REQUIRED,
            evidence_hash="4" * 64,
            customer_action_url="https://pay.test/definitive-retry",
        )
        self.assertEqual(self.request(retry_key).status_code, 201)
        retry = self.root_identity(retry_key)
        self.assertEqual(retry["placement_checkout_version"], original["placement_checkout_version"])
        self.assertEqual(retry["commercial_revision"], original["commercial_revision"])
        self.assertEqual(retry["checkout_id"], original["checkout_id"])
        self.assertEqual(retry["checkout_public_id"], original["checkout_public_id"])

    def test_no_effect_retry_root_reuses_original_placement_identity(self):
        first_key = uuid4()
        self.adapter.result = NormalizedProviderResult(
            outcome=ProviderRequestOutcome.NO_EFFECT_RETRYABLE,
            evidence_hash="5" * 64,
        )
        self.assertEqual(self.request(first_key).status_code, 201)
        original = self.root_identity(first_key)

        retry_key = uuid4()
        self.adapter.result = NormalizedProviderResult(
            outcome=ProviderRequestOutcome.CUSTOMER_ACTION_REQUIRED,
            evidence_hash="6" * 64,
            customer_action_url="https://pay.test/no-effect-retry",
        )
        self.assertEqual(self.request(retry_key).status_code, 201)
        retry = self.root_identity(retry_key)
        self.assertEqual(retry["placement_checkout_version"], original["placement_checkout_version"])
        self.assertEqual(retry["commercial_revision"], original["commercial_revision"])
        self.assertEqual(retry["checkout_id"], original["checkout_id"])

    def test_retry_after_later_checkout_version_increments_keeps_original_version(self):
        first_key = uuid4()
        self.adapter.result = NormalizedProviderResult(
            outcome=ProviderRequestOutcome.CONFIRMED_DECLINE,
            evidence_hash="7" * 64,
            reason_code="declined",
        )
        self.assertEqual(self.request(first_key).status_code, 201)
        original_version = self.root_identity(first_key)["placement_checkout_version"]
        Checkout.objects.filter(pk=self.checkout.pk).update(version=original_version + 7)

        retry_key = uuid4()
        self.adapter.result = NormalizedProviderResult(
            outcome=ProviderRequestOutcome.CUSTOMER_ACTION_REQUIRED,
            evidence_hash="8" * 64,
            customer_action_url="https://pay.test/later-version-retry",
        )
        self.assertEqual(self.request(retry_key).status_code, 201)
        self.assertEqual(
            self.root_identity(retry_key)["placement_checkout_version"],
            original_version,
        )

    def test_no_effect_retry_reuses_attempt_and_transaction(self):
        self.adapter.result = NormalizedProviderResult(
            outcome=ProviderRequestOutcome.NO_EFFECT_RETRYABLE,
            evidence_hash="d" * 64,
        )
        self.assertEqual(self.request(uuid4()).status_code, 201)
        self.adapter.result = NormalizedProviderResult(
            outcome=ProviderRequestOutcome.CUSTOMER_ACTION_REQUIRED,
            evidence_hash="e" * 64,
            customer_action_url="https://pay.test/retry",
        )
        retry = self.request(uuid4())
        self.assertEqual(retry.status_code, 201, retry.data)
        self.assertEqual((PaymentAttempt.objects.count(), PaymentTransaction.objects.count()), (1, 1))
        self.assertEqual(ProviderRequestResult.objects.count(), 2)

    def test_timeout_and_malformed_results_fail_closed_to_review(self):
        self.adapter.error = TimeoutError("sensitive upstream detail")
        timeout = self.request(uuid4())
        self.assertEqual(timeout.status_code, 201)
        self.assertTrue(timeout.data["do_not_pay_again"])
        self.assertEqual(timeout.data["attempt_status"], PaymentAttemptStatus.OUTCOME_UNKNOWN)
        self.assertEqual(ReviewCase.objects.count(), 1)
        self.assertNotIn("sensitive", str(timeout.data))

    def test_result_and_root_handoff_persistence_roll_back_together(self):
        with patch(
            "cheatgame.digital_products.services.payment_adapter._complete_root",
            side_effect=RuntimeError("synthetic persistence failure"),
        ):
            with self.assertRaises(RuntimeError):
                request_digital_checkout_payment(
                    checkout_public_id=self.checkout.public_id,
                    actor=self.customer,
                    provider="synthetic-pay",
                    idempotency_key=uuid4(),
                    adapter_registry=self.registry,
                )
        self.assertEqual(ProviderRequestResult.objects.count(), 0)
        root = IdempotencyRecord.objects.get(scope__startswith="digital_api:payment_request:")
        self.assertEqual(root.status, IdempotencyStatus.IN_PROGRESS)
        self.assertNotIn("customer_action_url", root.safe_response)
        transaction_obj = PaymentTransaction.objects.get()
        self.assertEqual(transaction_obj.status, PaymentTransactionStatus.REQUESTING)
        self.assertEqual(PaymentAttempt.objects.get().status, PaymentAttemptStatus.PROCESSING)

    def test_unsafe_redirect_becomes_protocol_review(self):
        self.adapter.result = NormalizedProviderResult(
            outcome=ProviderRequestOutcome.CUSTOMER_ACTION_REQUIRED,
            evidence_hash="f" * 64,
            customer_action_url="https://evil.test/authority",
        )
        response = self.request(uuid4())
        self.assertEqual(response.status_code, 201)
        self.assertIsNone(response.data["customer_action_url"])
        self.assertEqual(response.data["attempt_status"], PaymentAttemptStatus.REVIEW)

    def test_malformed_provider_response_becomes_protocol_review(self):
        class MalformedResult:
            outcome = ProviderRequestOutcome.CUSTOMER_ACTION_REQUIRED

        self.adapter.result = MalformedResult()
        response = self.request(uuid4())
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["attempt_status"], PaymentAttemptStatus.REVIEW)
        self.assertIsNone(response.data["customer_action_url"])

    def test_strict_input_permissions_ownership_and_methods(self):
        invalid = self.request(amount=1)
        self.assertEqual((invalid.status_code, invalid.data["code"]), (400, "invalid_request"))
        self.assertIn("amount", invalid.data["fields"])
        self.client.force_authenticate(None)
        self.assertEqual(self.request().status_code, 401)
        unverified = self.make_user("09127770003", verified=False)
        self.client.force_authenticate(unverified)
        self.assertEqual(self.request().status_code, 403)
        admin = self.make_user("09127770004", user_type=UserTypes.ADMIN)
        self.client.force_authenticate(admin)
        self.assertEqual(self.request().status_code, 403)
        other = self.make_user("09127770002")
        self.client.force_authenticate(other)
        self.assertEqual(self.request().status_code, 404)
        self.client.force_authenticate(self.customer)
        self.assertEqual(self.client.get(self.request_url).status_code, 405)

    def test_status_is_read_only_owned_and_query_bounded(self):
        self.assertEqual(self.request().status_code, 201)
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(self.status_url)
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(len(captured), 12)
        before = (Order.objects.count(), PaymentAttempt.objects.count(), ProviderRequestResult.objects.count())
        self.client.get(self.status_url)
        self.assertEqual(before, (Order.objects.count(), PaymentAttempt.objects.count(), ProviderRequestResult.objects.count()))
        self.assertNotIn("credential", str(response.data).lower())
        self.assertNotIn("merchant", str(response.data).lower())

    def test_api04_does_not_mutate_downstream_or_inventory_truth(self):
        initial_pool = self.pool.sellable_quantity
        initial_product = self.product.quantity
        self.assertEqual(self.request().status_code, 201)
        self.pool.refresh_from_db()
        self.product.refresh_from_db()
        reservation = DigitalInventoryReservation.objects.get()
        self.assertEqual(reservation.state, DigitalInventoryReservationState.PAYMENT_HOLD)
        self.assertEqual(self.pool.sellable_quantity, initial_pool)
        self.assertEqual(self.product.quantity, initial_product)
        self.assertEqual(FinancialAllocation.objects.count(), 0)
        self.assertEqual(JournalEntry.objects.count(), 0)
        self.assertEqual(CommercialFinalization.objects.count(), 0)
        self.assertEqual(DigitalFulfillmentObligation.objects.count(), 0)
        self.assertEqual(DigitalFulfillmentItem.objects.count(), 0)
        self.assertEqual(Entitlement.objects.count(), 0)
        self.assertEqual(LegacyPayment.objects.count(), 0)

    def test_provider_unavailable_does_not_place_checkout(self):
        CustomerDigitalPaymentRequestApi.adapter_registry = ProviderAdapterRegistry()
        response = self.request()
        self.assertEqual((response.status_code, response.data["code"]), (503, "payment_provider_unavailable"))
        self.assertEqual((Order.objects.count(), Payment.objects.count()), (0, 0))

    def test_openapi_has_explicit_payment_operations(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        paths = schema["paths"]
        self.assertIn("/api/digital-products/customer/checkout/{checkout_id}/payment/request/", paths)
        self.assertIn("/api/digital-products/customer/checkout/{checkout_id}/payment/", paths)
        self.assertIn("post", paths["/api/digital-products/customer/checkout/{checkout_id}/payment/request/"])
        self.assertIn("get", paths["/api/digital-products/customer/checkout/{checkout_id}/payment/"])
