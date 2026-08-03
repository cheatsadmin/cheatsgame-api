import os
import subprocess
import sys
from datetime import date, timedelta
from io import StringIO
from threading import Barrier, Thread
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import close_old_connections
from django.test import SimpleTestCase, TransactionTestCase, override_settings

from cheatgame.digital_products.models import (
    DigitalCartFulfillmentMethod,
    DigitalGameReleaseMetadata,
    DigitalGameUpcomingStatus,
    DigitalInventoryReservation,
    DigitalInventoryReservationState,
    DigitalFulfillmentItem,
    DigitalOffer,
    DigitalOfferCapacity,
    DigitalOfferSaleState,
    InventoryPool,
    InventoryPoolStatus,
    Entitlement,
)
from cheatgame.digital_products.services.cart import add_digital_offer_to_cart
from cheatgame.digital_products.services.checkout_preparation import prepare_digital_checkout
from cheatgame.digital_products.services.payment_adapter import request_digital_checkout_payment
from cheatgame.financial_core.models import (
    CommercialAccountingPolicyVersion,
    CommercialFinalization,
    DigitalFulfillmentObligation,
    FinancialAllocation,
    JournalEntry,
    MoneyUnit,
    PaymentAttempt,
    ProviderRequestOutcome,
    ReceiptAccountingPolicyVersion,
    Verification,
    VerificationWorkItem,
)
from cheatgame.financial_core.services.adapters import (
    ADAPTER_CONTRACT_VERSION,
    ImmutableProviderRequestEnvelope,
    ProviderAdapterRegistry,
    VerificationEnvelope,
)
from cheatgame.financial_core.services.financial_certification import (
    FINANCIAL_CERTIFICATION_ADAPTER_KEY,
    FINANCIAL_CERTIFICATION_CREDENTIAL_REFERENCE,
    FINANCIAL_CERTIFICATION_PROVIDER_KEY,
    FinancialCertificationAdapter,
)
from cheatgame.financial_core.services.runtime import run_runtime_batch
from cheatgame.financial_core.test_commercial_finalizer_phase1 import CommercialFinalizerFixture
from cheatgame.product.models import DeliveredVersion, NativeConsole, ProductCommerceAuthority
from cheatgame.shop.models import Cart
from cheatgame.users.models import UserTypes


CERTIFICATION_SETTINGS = {
    "FINANCIAL_CERTIFICATION_PROVIDER_ENABLED": True,
    "FINANCIAL_CERTIFICATION_SECRET": "x" * 48,
    "FINANCIAL_CERTIFICATION_ALLOWED_HOSTS": ["testserver"],
    "CHEATSGAME_RUNTIME_ENVIRONMENT": "test",
    "ALLOWED_HOSTS": ["testserver"],
    "FINANCIAL_CERTIFICATION_ACCOUNT_KEY": "test-certification",
    "FINANCIAL_CERTIFICATION_OWNER_KEY": "test-owner",
}


def request_envelope(**overrides):
    values = {
        "transaction_public_id": str(uuid4()),
        "operation_type": "sale",
        "provider_key": FINANCIAL_CERTIFICATION_PROVIDER_KEY,
        "adapter_key": FINANCIAL_CERTIFICATION_ADAPTER_KEY,
        "adapter_contract_version": ADAPTER_CONTRACT_VERSION,
        "provider_capability_version": 1,
        "merchant_account_key": "test-certification",
        "merchant_account_version": 1,
        "credential_reference": FINANCIAL_CERTIFICATION_CREDENTIAL_REFERENCE,
        "merchant_reference": "cg-certification-test",
        "provider_reference": "",
        "canonical_amount": "90000",
        "canonical_currency": MoneyUnit.IRR,
        "provider_amount": "90000",
        "provider_unit": MoneyUnit.IRR,
        "provider_idempotency_reference": "",
        "request_fingerprint": "f" * 64,
        "claim_token": str(uuid4()),
        "callback_identity": "",
        "correlation_id": str(uuid4()),
    }
    values.update(overrides)
    return ImmutableProviderRequestEnvelope(**values)


@override_settings(**CERTIFICATION_SETTINGS)
class FinancialCertificationAdapterTests(SimpleTestCase):
    def test_enabled_registry_can_import_certification_command_in_fresh_process(self):
        environment = os.environ.copy()
        environment.update(
            {
                "DJANGO_SETTINGS_MODULE": "config.django.base",
                "ALLOWED_HOSTS": "testserver",
                "CHEATSGAME_RUNTIME_ENVIRONMENT": "test",
                "FINANCIAL_CERTIFICATION_PROVIDER_ENABLED": "True",
                "FINANCIAL_CERTIFICATION_SECRET": "x" * 48,
                "FINANCIAL_CERTIFICATION_ALLOWED_HOSTS": "testserver",
            }
        )
        result = subprocess.run(
            [sys.executable, "manage.py", "help", "certify_staging_payment"],
            cwd=settings.BASE_DIR,
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_request_stays_pending_and_verification_uses_immutable_irr(self):
        adapter = FinancialCertificationAdapter.from_settings()
        request = request_envelope()
        pending = adapter.execute_operation(request)
        self.assertEqual(pending.outcome, ProviderRequestOutcome.ACCEPTED_PENDING)
        self.assertFalse(pending.customer_action_url)
        verification = VerificationEnvelope(
            transaction_public_id=request.transaction_public_id,
            operation_type=request.operation_type,
            provider_key=request.provider_key,
            adapter_key=request.adapter_key,
            adapter_contract_version=request.adapter_contract_version,
            merchant_account_key=request.merchant_account_key,
            merchant_account_version=request.merchant_account_version,
            credential_reference=request.credential_reference,
            merchant_reference=request.merchant_reference,
            provider_authority=pending.provider_authority,
            provider_reference="",
            requested_provider_amount=request.provider_amount,
            requested_provider_unit=request.provider_unit,
            canonical_amount=request.canonical_amount,
            canonical_currency=request.canonical_currency,
            claim_token=str(uuid4()),
            correlation_id=request.correlation_id,
        )
        result = adapter.query_operation(verification)
        self.assertEqual(result.outcome, "confirmed_success")
        self.assertEqual(result.financial_effect, "paid")
        self.assertEqual(result.observed_provider_amount, 90000)
        self.assertEqual(result.observed_provider_unit, MoneyUnit.IRR)
        self.assertTrue(result.provider_reference.startswith("fcert_r_"))

    def test_amount_currency_and_authority_mismatch_fail_closed(self):
        adapter = FinancialCertificationAdapter.from_settings()
        with self.assertRaises(ValidationError):
            adapter.execute_operation(request_envelope(provider_amount="9000"))
        with self.assertRaises(ValidationError):
            adapter.execute_operation(request_envelope(canonical_currency=MoneyUnit.IRT))
        request = request_envelope()
        pending = adapter.execute_operation(request)
        verification = VerificationEnvelope(
            transaction_public_id=request.transaction_public_id,
            operation_type=request.operation_type,
            provider_key=request.provider_key,
            adapter_key=request.adapter_key,
            adapter_contract_version=request.adapter_contract_version,
            merchant_account_key=request.merchant_account_key,
            merchant_account_version=request.merchant_account_version,
            credential_reference=request.credential_reference,
            merchant_reference=request.merchant_reference,
            provider_authority=pending.provider_authority + "x",
            provider_reference="",
            requested_provider_amount=request.provider_amount,
            requested_provider_unit=request.provider_unit,
            canonical_amount=request.canonical_amount,
            canonical_currency=request.canonical_currency,
            claim_token=str(uuid4()),
            correlation_id=request.correlation_id,
        )
        self.assertEqual(adapter.query_operation(verification).outcome, "security_failure")

    @override_settings(CHEATSGAME_RUNTIME_ENVIRONMENT="production")
    def test_runtime_guard_rejects_production(self):
        with self.assertRaises(ImproperlyConfigured):
            FinancialCertificationAdapter.from_settings()

    @override_settings(FINANCIAL_CERTIFICATION_PROVIDER_ENABLED=False)
    def test_runtime_guard_rejects_disabled_provider(self):
        with self.assertRaises(ImproperlyConfigured):
            FinancialCertificationAdapter.from_settings()

    @override_settings(FINANCIAL_CERTIFICATION_SECRET="")
    def test_runtime_guard_rejects_missing_secret(self):
        with self.assertRaises(ImproperlyConfigured):
            FinancialCertificationAdapter.from_settings()


@override_settings(**CERTIFICATION_SETTINGS)
class FinancialCertificationLifecycleTests(CommercialFinalizerFixture, TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        super().setUp()
        call_command("configure_financial_certification", "--apply", stdout=StringIO())
        self.adapter = FinancialCertificationAdapter.from_settings()
        self.registry = ProviderAdapterRegistry(
            {(self.adapter.adapter_key, self.adapter.contract_version): self.adapter}
        )

    def _graph(self):
        customer = self.make_user()
        product = self.make_product(authority=ProductCommerceAuthority.DIGITAL_PRODUCTS, price=9000)
        DigitalGameReleaseMetadata.objects.create(
            product=product,
            release_date=date.today() + timedelta(days=30),
            upcoming_status=DigitalGameUpcomingStatus.PREORDER_OPEN,
            preorder_enabled=True,
        )
        version = DeliveredVersion.objects.create(product=product, native_console=NativeConsole.PS5)
        pool = InventoryPool.objects.create(sellable_quantity=2, status=InventoryPoolStatus.ENABLED)
        offer = DigitalOffer.objects.create(
            delivered_version=version,
            customer_console=NativeConsole.PS5,
            capacity=DigitalOfferCapacity.CAPACITY_1,
            price=9000,
            inventory_pool=pool,
            sale_state=DigitalOfferSaleState.ACTIVE,
        )
        cart = Cart.objects.create(user=customer)
        add_digital_offer_to_cart(
            cart=cart,
            offer=offer,
            fulfillment_method=DigitalCartFulfillmentMethod.IN_STORE,
            actor=customer,
        )
        checkout, _ = prepare_digital_checkout(actor=customer, client_checkout_uuid=uuid4())
        request_digital_checkout_payment(
            checkout_public_id=checkout.public_id,
            actor=customer,
            provider=FINANCIAL_CERTIFICATION_PROVIDER_KEY,
            idempotency_key=uuid4(),
            adapter_registry=self.registry,
        )
        attempt = PaymentAttempt.objects.get(payment__order__checkout=checkout)
        admin = self.make_user()
        admin.user_type = UserTypes.ADMIN
        admin.phone_verified = True
        admin.save(update_fields=("user_type", "phone_verified", "updated_at"))
        return customer, checkout, attempt, admin, pool

    def test_admin_certification_uses_full_runtime_and_defers_preorder_fulfillment(self):
        _, _, attempt, admin, pool = self._graph()
        self.assertEqual(
            ReceiptAccountingPolicyVersion.objects.filter(
                merchant_account_version=attempt.merchant_account_version,
                active_for_new_applications=True,
            ).count(),
            1,
        )
        stdout = StringIO()
        call_command(
            "certify_staging_payment",
            "--payment-attempt",
            str(attempt.public_id),
            "--actor-id",
            str(admin.pk),
            "--confirm",
            stdout=stdout,
        )
        self.assertEqual(VerificationWorkItem.objects.count(), 1)
        before = pool.sellable_quantity
        result = run_runtime_batch(limit=3, adapter_registry=self.registry)
        self.assertEqual(
            [item.stage for item in result.results],
            ["verification", "recognition", "finalization"],
            msg=(result.results, list(Verification.objects.values("normalized_outcome", "error_classification"))),
        )
        attempt.refresh_from_db()
        pool.refresh_from_db()
        self.assertEqual(Verification.objects.count(), 1)
        self.assertEqual(FinancialAllocation.objects.count(), 1)
        self.assertEqual(CommercialFinalization.objects.count(), 1)
        self.assertEqual(pool.sellable_quantity, before - 1)
        self.assertEqual(
            DigitalInventoryReservation.objects.get(order=attempt.payment.order).state,
            DigitalInventoryReservationState.CONSUMED,
        )
        self.assertEqual(DigitalFulfillmentObligation.objects.count(), 0)
        self.assertEqual(DigitalFulfillmentItem.objects.count(), 0)
        self.assertEqual(Entitlement.objects.count(), 0)
        self.assertEqual(
            JournalEntry.objects.filter(source_type="provider_receipt").count(), 1
        )
        receipt_entry = JournalEntry.objects.get(source_type="provider_receipt")
        receipt_postings = list(receipt_entry.postings.all())
        self.assertEqual(
            sum(posting.amount for posting in receipt_postings if posting.direction == "debit"),
            attempt.payment.amount_due,
        )
        self.assertEqual(
            sum(posting.amount for posting in receipt_postings if posting.direction == "credit"),
            attempt.payment.amount_due,
        )
        call_command(
            "certify_staging_payment",
            "--payment-attempt",
            str(attempt.public_id),
            "--actor-id",
            str(admin.pk),
            "--confirm",
            stdout=StringIO(),
        )
        run_runtime_batch(limit=3, adapter_registry=self.registry)
        self.assertEqual(FinancialAllocation.objects.count(), 1)
        self.assertEqual(CommercialFinalization.objects.count(), 1)
        self.assertEqual(pool.sellable_quantity, before - 1)

    def test_manager_and_customer_cannot_certify(self):
        customer, _, attempt, _, _ = self._graph()
        with self.assertRaises(CommandError):
            call_command(
                "certify_staging_payment",
                "--payment-attempt",
                str(attempt.public_id),
                "--actor-id",
                str(customer.pk),
                "--confirm",
                stdout=StringIO(),
            )
        manager = self.make_user()
        manager.user_type = UserTypes.MANAGER
        manager.is_admin = True
        manager.save(update_fields=("user_type", "is_admin", "updated_at"))
        with self.assertRaises(CommandError):
            call_command(
                "certify_staging_payment",
                "--payment-attempt",
                str(attempt.public_id),
                "--actor-id",
                str(manager.pk),
                "--confirm",
                stdout=StringIO(),
            )

    def test_expired_checkout_cannot_be_certified(self):
        _, checkout, attempt, admin, _ = self._graph()
        checkout.expires_at = checkout.expires_at - timedelta(days=2)
        checkout.save(update_fields=("expires_at", "updated_at"))
        with self.assertRaises(CommandError):
            call_command(
                "certify_staging_payment",
                "--payment-attempt",
                str(attempt.public_id),
                "--actor-id",
                str(admin.pk),
                "--confirm",
                stdout=StringIO(),
            )

    def test_wrong_provider_attempt_cannot_be_certified(self):
        CommercialAccountingPolicyVersion.objects.filter(
            commerce_authority="digital_products",
            active_for_new_finalizations=True,
        ).update(active_for_new_finalizations=False)
        placement, _ = self.ready_digital(preorder=True)
        attempt = placement.payment.attempts.get()
        admin = self.make_user()
        admin.user_type = UserTypes.ADMIN
        admin.save(update_fields=("user_type", "updated_at"))
        with self.assertRaises(CommandError):
            call_command(
                "certify_staging_payment",
                "--payment-attempt",
                str(attempt.public_id),
                "--actor-id",
                str(admin.pk),
                "--confirm",
                stdout=StringIO(),
            )

    def test_concurrent_duplicate_certification_converges(self):
        _, _, attempt, admin, _ = self._graph()
        barrier = Barrier(2)
        outcomes = []

        def runner():
            close_old_connections()
            try:
                barrier.wait()
                call_command(
                    "certify_staging_payment",
                    "--payment-attempt",
                    str(attempt.public_id),
                    "--actor-id",
                    str(admin.pk),
                    "--confirm",
                    stdout=StringIO(),
                )
                outcomes.append("ok")
            except Exception as exc:
                outcomes.append(type(exc).__name__)
            finally:
                close_old_connections()

        threads = [Thread(target=runner) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(outcomes, ["ok", "ok"])
        self.assertEqual(VerificationWorkItem.objects.count(), 1)
