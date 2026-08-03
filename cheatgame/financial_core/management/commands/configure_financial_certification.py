from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from cheatgame.financial_core.models import (
    CallbackAuthenticationStrength,
    MerchantAccountVersion,
    MoneyUnit,
    PaymentTransactionOperation,
    ProviderCapabilityVersion,
    ProviderDefinition,
    ProviderVerificationSemantics,
)
from cheatgame.financial_core.services.adapters import ADAPTER_CONTRACT_VERSION
from cheatgame.financial_core.services.financial_certification import (
    FINANCIAL_CERTIFICATION_ADAPTER_KEY,
    FINANCIAL_CERTIFICATION_CONVERSION_POLICY_VERSION,
    FINANCIAL_CERTIFICATION_CREDENTIAL_REFERENCE,
    FINANCIAL_CERTIFICATION_PROVIDER_KEY,
    FinancialCertificationAdapter,
)


class Command(BaseCommand):
    help = "Inspect or provision the staging-only Financial Certification provider."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        del args
        FinancialCertificationAdapter.from_settings()
        if not options["apply"]:
            self._inspect()
            return
        with transaction.atomic():
            provider = self._provider()
            capability = self._capability(provider)
            account = self._account(provider, capability)
            for item in (provider, account):
                changed = []
                if not item.is_enabled:
                    item.is_enabled = True
                    changed.append("is_enabled")
                if not item.new_requests_enabled:
                    item.new_requests_enabled = True
                    changed.append("new_requests_enabled")
                if changed:
                    item.save(update_fields=(*changed, "updated_at"))
        self.stdout.write(
            self.style.SUCCESS(
                "Financial Certification configured for staging: "
                f"provider={provider.key}, capability={capability.version}, "
                f"account={account.account_key}:{account.version}"
            )
        )

    def _inspect(self):
        provider = ProviderDefinition.objects.filter(key=FINANCIAL_CERTIFICATION_PROVIDER_KEY).first()
        capability = ProviderCapabilityVersion.objects.filter(provider=provider, version=1).first() if provider else None
        account = MerchantAccountVersion.objects.filter(
            provider=provider,
            account_key=settings.FINANCIAL_CERTIFICATION_ACCOUNT_KEY,
            version=1,
        ).first() if provider else None
        self.stdout.write(
            "Financial Certification configuration: "
            f"provider={'present' if provider else 'missing'}, "
            f"capability={'present' if capability else 'missing'}, "
            f"account={'present' if account else 'missing'}, "
            f"enabled={bool(provider and provider.is_enabled and account and account.is_enabled)}"
        )

    @staticmethod
    def _ensure_exact(instance, expected):
        mismatches = [name for name, value in expected.items() if getattr(instance, name) != value]
        if mismatches:
            raise CommandError(
                "Existing Financial Certification configuration conflicts in: "
                + ", ".join(sorted(mismatches))
            )
        return instance

    def _provider(self):
        provider, _ = ProviderDefinition.objects.get_or_create(
            key=FINANCIAL_CERTIFICATION_PROVIDER_KEY,
            defaults={
                "display_name": "Financial Certification (staging only)",
                "is_enabled": False,
                "new_requests_enabled": False,
            },
        )
        return self._ensure_exact(
            provider, {"display_name": "Financial Certification (staging only)"}
        )

    def _capability(self, provider):
        expected = {
            "adapter_key": FINANCIAL_CERTIFICATION_ADAPTER_KEY,
            "adapter_contract_version": ADAPTER_CONTRACT_VERSION,
            "provider_unit": MoneyUnit.IRR,
            "conversion_policy_version": FINANCIAL_CERTIFICATION_CONVERSION_POLICY_VERSION,
            "supported_operations": [PaymentTransactionOperation.SALE],
            "supports_request_idempotency": True,
            "supports_lookup": True,
            "callback_authentication": CallbackAuthenticationStrength.NONE,
            "callback_authentication_method": "",
            "callback_authentication_version": "",
            "callback_verification_is_final": False,
            "verification_semantics": ProviderVerificationSemantics.REQUIRED,
            "finality_window_seconds": 86400,
            "authority_expiry_seconds": 1800,
            "supports_refund": False,
            "supports_void": False,
            "not_found_is_final_unpaid": False,
        }
        capability, _ = ProviderCapabilityVersion.objects.get_or_create(
            provider=provider, version=1, defaults=expected
        )
        return self._ensure_exact(capability, expected)

    def _account(self, provider, capability):
        expected = {
            "capability_version_id": capability.pk,
            "owner_key": settings.FINANCIAL_CERTIFICATION_OWNER_KEY,
            "credential_reference": FINANCIAL_CERTIFICATION_CREDENTIAL_REFERENCE,
            "callback_signing_key_reference_hash": "",
            "recovery_enabled": True,
        }
        account, _ = MerchantAccountVersion.objects.get_or_create(
            provider=provider,
            account_key=settings.FINANCIAL_CERTIFICATION_ACCOUNT_KEY,
            version=1,
            defaults={
                "capability_version": capability,
                "owner_key": expected["owner_key"],
                "credential_reference": expected["credential_reference"],
                "callback_signing_key_reference_hash": "",
                "is_enabled": False,
                "new_requests_enabled": False,
                "recovery_enabled": True,
            },
        )
        return self._ensure_exact(account, expected)
