from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from cheatgame.financial_core.models import (
    CommercialAccountingPolicyVersion,
    FinancialAccount,
    FinancialAccountStatus,
    FinancialAccountType,
    MerchantAccountVersion,
    MoneyUnit,
    ProviderDefinition,
    ReceiptAccountingPolicyVersion,
)
from cheatgame.financial_core.services.provider_constants import (
    ZARINPAL_ADAPTER_KEY,
    ZARINPAL_PROVIDER_KEY,
)


RECEIPT_POLICY_KEY = "production-zarinpal-receipt-v1"
COMMERCIAL_POLICY_KEY = "production-digital-commercial-v1"
POLICY_VERSION = 1


class Command(BaseCommand):
    help = (
        "Inspect or provision the immutable Zarinpal receipt and Digital Products "
        "commercial accounting policies. The default mode is read-only."
    )

    def add_arguments(self, parser):
        parser.add_argument("--merchant-account-key", required=True)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        del args
        if not options["apply"]:
            account = self._zarinpal_account(options["merchant_account_key"], lock=False)
            self._inspect(account)
            return
        with transaction.atomic():
            account = self._zarinpal_account(options["merchant_account_key"], lock=True)
            receipt, commercial = self._provision(account)
        self.stdout.write(
            self.style.SUCCESS(
                "Production accounting policy provisioning complete: "
                f"provider={ZARINPAL_PROVIDER_KEY}, "
                f"merchant_account={account.account_key}:{account.version}, "
                f"receipt_policy={receipt.policy_key}:{receipt.version}, "
                f"commercial_policy={commercial.policy_key}:{commercial.version}"
            )
        )

    def _zarinpal_account(self, account_key, *, lock):
        provider = ProviderDefinition.objects.filter(key=ZARINPAL_PROVIDER_KEY).first()
        if not provider:
            raise CommandError("Zarinpal provider configuration is missing; run configure_zarinpal first.")
        queryset = MerchantAccountVersion.objects.select_related("capability_version")
        if lock:
            queryset = queryset.select_for_update()
        accounts = list(queryset.filter(provider=provider, account_key=account_key))
        if len(accounts) != 1:
            raise CommandError("Exactly one matching Zarinpal merchant account version is required.")
        account = accounts[0]
        if account.capability_version.adapter_key != ZARINPAL_ADAPTER_KEY:
            raise CommandError("Merchant account is not bound to the official Zarinpal adapter.")
        if not provider.is_enabled or not account.is_enabled:
            raise CommandError("Zarinpal provider and merchant account must be enabled first.")
        return account

    def _inspect(self, account):
        active_receipts = ReceiptAccountingPolicyVersion.objects.filter(
            merchant_account_version=account,
            active_for_new_applications=True,
        )
        active_commercial = CommercialAccountingPolicyVersion.objects.filter(
            commerce_authority="digital_products",
            active_for_new_finalizations=True,
        )
        receipt = active_receipts.filter(
            policy_key=RECEIPT_POLICY_KEY, version=POLICY_VERSION
        ).first()
        commercial = active_commercial.filter(
            policy_key=COMMERCIAL_POLICY_KEY, version=POLICY_VERSION
        ).first()
        state = "ready" if receipt and commercial and active_receipts.count() == 1 and active_commercial.count() == 1 else "not_ready"
        self.stdout.write(
            "Production accounting policy inspection: "
            f"state={state}, provider={ZARINPAL_PROVIDER_KEY}, "
            f"merchant_account={account.account_key}:{account.version}, "
            f"active_zarinpal_receipt_policies={active_receipts.count()}, "
            f"active_digital_commercial_policies={active_commercial.count()}"
        )

    @staticmethod
    def _ensure_exact(instance, expected, *, label):
        mismatches = [name for name, value in expected.items() if getattr(instance, name) != value]
        if mismatches:
            raise CommandError(f"Existing {label} conflicts in: " + ", ".join(sorted(mismatches)))
        return instance

    def _financial_account(self, *, key, name, account_type):
        expected = {
            "name": name,
            "account_type": account_type,
            "currency": MoneyUnit.IRR,
            "status": FinancialAccountStatus.ACTIVE,
        }
        account, _ = FinancialAccount.objects.get_or_create(key=key, defaults=expected)
        return self._ensure_exact(account, expected, label=f"financial account {key}")

    def _provision(self, merchant_account):
        clearing = self._financial_account(
            key="zarinpal-provider-clearing",
            name="Zarinpal provider clearing",
            account_type=FinancialAccountType.ASSET,
        )
        liability = self._financial_account(
            key="customer-unapplied-funds",
            name="Customer unapplied funds",
            account_type=FinancialAccountType.LIABILITY,
        )
        merchandise = self._financial_account(
            key="digital-merchandise-revenue",
            name="Digital merchandise revenue",
            account_type=FinancialAccountType.REVENUE,
        )
        shipping = self._financial_account(
            key="digital-shipping-revenue",
            name="Digital shipping revenue",
            account_type=FinancialAccountType.REVENUE,
        )

        active_receipts = ReceiptAccountingPolicyVersion.objects.select_for_update().filter(
            merchant_account_version=merchant_account,
            active_for_new_applications=True,
        )
        conflicting_receipt = active_receipts.exclude(
            policy_key=RECEIPT_POLICY_KEY, version=POLICY_VERSION
        ).first()
        if conflicting_receipt:
            raise CommandError("A conflicting active Zarinpal receipt policy already exists.")
        receipt_expected = {
            "provider_clearing_account_id": clearing.pk,
            "customer_unapplied_funds_account_id": liability.pk,
            "currency": MoneyUnit.IRR,
        }
        receipt, _ = ReceiptAccountingPolicyVersion.objects.get_or_create(
            merchant_account_version=merchant_account,
            policy_key=RECEIPT_POLICY_KEY,
            version=POLICY_VERSION,
            defaults={
                "provider_clearing_account": clearing,
                "customer_unapplied_funds_account": liability,
                "currency": MoneyUnit.IRR,
                "active_for_new_applications": True,
            },
        )
        self._ensure_exact(receipt, receipt_expected, label="Zarinpal receipt policy")
        if not receipt.active_for_new_applications:
            if active_receipts.exists():
                raise CommandError("A conflicting active Zarinpal receipt policy already exists.")
            receipt.active_for_new_applications = True
            receipt.save(update_fields=("active_for_new_applications", "updated_at"))

        active_commercial = CommercialAccountingPolicyVersion.objects.select_for_update().filter(
            commerce_authority="digital_products",
            active_for_new_finalizations=True,
        )
        conflicting_commercial = active_commercial.exclude(
            policy_key=COMMERCIAL_POLICY_KEY, version=POLICY_VERSION
        ).first()
        if conflicting_commercial:
            raise CommandError("A conflicting active Digital Products commercial policy already exists.")
        commercial_expected = {
            "customer_unapplied_funds_account_id": liability.pk,
            "merchandise_revenue_account_id": merchandise.pk,
            "shipping_revenue_account_id": shipping.pk,
            "currency": MoneyUnit.IRR,
        }
        commercial, _ = CommercialAccountingPolicyVersion.objects.get_or_create(
            policy_key=COMMERCIAL_POLICY_KEY,
            version=POLICY_VERSION,
            commerce_authority="digital_products",
            defaults={
                "customer_unapplied_funds_account": liability,
                "merchandise_revenue_account": merchandise,
                "shipping_revenue_account": shipping,
                "currency": MoneyUnit.IRR,
                "active_for_new_finalizations": True,
            },
        )
        self._ensure_exact(commercial, commercial_expected, label="Digital Products commercial policy")
        if not commercial.active_for_new_finalizations:
            if active_commercial.exists():
                raise CommandError("A conflicting active Digital Products commercial policy already exists.")
            commercial.active_for_new_finalizations = True
            commercial.save(update_fields=("active_for_new_finalizations", "updated_at"))
        return receipt, commercial
