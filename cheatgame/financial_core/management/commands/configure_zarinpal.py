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
from cheatgame.financial_core.services.zarinpal import (
    ZARINPAL_ADAPTER_KEY,
    ZARINPAL_CONVERSION_POLICY_VERSION,
    ZARINPAL_CREDENTIAL_REFERENCE,
    ZARINPAL_PROVIDER_KEY,
    ZarinpalAdapter,
)


class Command(BaseCommand):
    help = "Inspect or provision the immutable Financial Core Zarinpal launch configuration."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Create missing configuration and enable new requests.",
        )

    def handle(self, *args, **options):
        del args
        if not getattr(settings, "FINANCIAL_ZARINPAL_ENABLED", False):
            raise CommandError("FINANCIAL_ZARINPAL_ENABLED must be true.")
        ZarinpalAdapter.from_settings()
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
                f"Zarinpal configured: provider={provider.key}, "
                f"capability={capability.version}, account={account.account_key}:{account.version}"
            )
        )

    def _inspect(self):
        provider = ProviderDefinition.objects.filter(key=ZARINPAL_PROVIDER_KEY).first()
        capability = (
            ProviderCapabilityVersion.objects.filter(provider=provider, version=1).first()
            if provider
            else None
        )
        account = (
            MerchantAccountVersion.objects.filter(
                provider=provider,
                account_key=settings.FINANCIAL_ZARINPAL_ACCOUNT_KEY,
                version=1,
            ).first()
            if provider
            else None
        )
        self.stdout.write(
            "Zarinpal configuration: "
            f"provider={'present' if provider else 'missing'}, "
            f"capability={'present' if capability else 'missing'}, "
            f"account={'present' if account else 'missing'}, "
            f"enabled={bool(provider and provider.is_enabled and account and account.is_enabled)}"
        )

    @staticmethod
    def _ensure_exact(instance, expected):
        mismatches = [
            name
            for name, value in expected.items()
            if getattr(instance, name) != value
        ]
        if mismatches:
            raise CommandError(
                "Existing Zarinpal configuration conflicts in: " + ", ".join(sorted(mismatches))
            )
        return instance

    def _provider(self):
        provider, _ = ProviderDefinition.objects.get_or_create(
            key=ZARINPAL_PROVIDER_KEY,
            defaults={
                "display_name": "Zarinpal",
                "is_enabled": False,
                "new_requests_enabled": False,
            },
        )
        return self._ensure_exact(provider, {"display_name": "Zarinpal"})

    def _capability(self, provider):
        expected = {
            "adapter_key": ZARINPAL_ADAPTER_KEY,
            "adapter_contract_version": ADAPTER_CONTRACT_VERSION,
            "provider_unit": MoneyUnit.IRR,
            "conversion_policy_version": ZARINPAL_CONVERSION_POLICY_VERSION,
            "supported_operations": [PaymentTransactionOperation.SALE],
            "supports_request_idempotency": False,
            "supports_lookup": True,
            "callback_authentication": CallbackAuthenticationStrength.NONE,
            "callback_authentication_method": "",
            "callback_authentication_version": "",
            "callback_verification_is_final": False,
            "verification_semantics": ProviderVerificationSemantics.REQUIRED,
            "finality_window_seconds": settings.FINANCIAL_ZARINPAL_FINALITY_WINDOW_SECONDS,
            "authority_expiry_seconds": settings.FINANCIAL_ZARINPAL_AUTHORITY_EXPIRY_SECONDS,
            "supports_refund": False,
            "supports_void": False,
            "not_found_is_final_unpaid": True,
        }
        capability, _ = ProviderCapabilityVersion.objects.get_or_create(
            provider=provider,
            version=1,
            defaults=expected,
        )
        return self._ensure_exact(capability, expected)

    def _account(self, provider, capability):
        expected = {
            "capability_version_id": capability.pk,
            "owner_key": settings.FINANCIAL_ZARINPAL_OWNER_KEY,
            "credential_reference": ZARINPAL_CREDENTIAL_REFERENCE,
            "callback_signing_key_reference_hash": "",
            "recovery_enabled": True,
        }
        account, _ = MerchantAccountVersion.objects.get_or_create(
            provider=provider,
            account_key=settings.FINANCIAL_ZARINPAL_ACCOUNT_KEY,
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
