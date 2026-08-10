from io import StringIO
import os
from pathlib import Path
import subprocess
import sys

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings

from cheatgame.financial_core.models import (
    CommercialAccountingPolicyVersion,
    ReceiptAccountingPolicyVersion,
)


ZARINPAL_TEST_SETTINGS = {
    "FINANCIAL_ZARINPAL_ENABLED": True,
    "FINANCIAL_ZARINPAL_ACCOUNT_KEY": "production-terminal",
    "FINANCIAL_ZARINPAL_OWNER_KEY": "cheatsg-production",
    "FINANCIAL_ZARINPAL_FINALITY_WINDOW_SECONDS": 86400,
    "FINANCIAL_ZARINPAL_AUTHORITY_EXPIRY_SECONDS": 1800,
    "ZARINPAL_MERCHANT_ID": "a" * 36,
    "ZARINPAL_SANDBOX": False,
    "ZARINPAL_REQUEST_URL": "https://payment.zarinpal.com/pg/v4/payment/request.json",
    "ZARINPAL_VERIFY_URL": "https://payment.zarinpal.com/pg/v4/payment/verify.json",
    "ZARINPAL_STARTPAY_URL": "https://payment.zarinpal.com/pg/StartPay/{authority}",
    "FINANCIAL_PROVIDER_CALLBACK_BASE_URL": "https://api.cheatsg.ir/api/financial/callback/",
}


class ProductionAccountingCommandImportTests(SimpleTestCase):
    def test_command_loads_in_a_clean_management_process(self):
        project_root = Path(__file__).resolve().parents[2]
        environment = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "config.django.test",
            "CHEATSGAME_RUNTIME_ENVIRONMENT": "test",
        }
        result = subprocess.run(
            [
                sys.executable,
                str(project_root / "manage.py"),
                "help",
                "configure_production_accounting",
            ],
            cwd=project_root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--merchant-account-key", result.stdout)


@override_settings(**ZARINPAL_TEST_SETTINGS)
class ProductionAccountingConfigurationTests(TestCase):
    def setUp(self):
        call_command("configure_zarinpal", "--apply", stdout=StringIO())

    def run_accounting(self, *args):
        output = StringIO()
        call_command(
            "configure_production_accounting",
            "--merchant-account-key",
            "production-terminal",
            *args,
            stdout=output,
        )
        return output.getvalue()

    def test_default_mode_is_read_only_and_reports_not_ready(self):
        output = self.run_accounting()
        self.assertIn("state=not_ready", output)
        self.assertEqual(ReceiptAccountingPolicyVersion.objects.count(), 0)
        self.assertEqual(CommercialAccountingPolicyVersion.objects.count(), 0)

    def test_apply_is_idempotent_and_provisions_exactly_one_active_policy(self):
        self.run_accounting("--apply")
        self.run_accounting("--apply")
        self.assertEqual(
            ReceiptAccountingPolicyVersion.objects.filter(
                active_for_new_applications=True
            ).count(),
            1,
        )
        self.assertEqual(
            CommercialAccountingPolicyVersion.objects.filter(
                commerce_authority="digital_products",
                active_for_new_finalizations=True,
            ).count(),
            1,
        )
        self.assertIn("state=ready", self.run_accounting())

    def test_conflicting_active_commercial_policy_is_rejected(self):
        self.run_accounting("--apply")
        policy = CommercialAccountingPolicyVersion.objects.get()
        policy.active_for_new_finalizations = False
        policy.save(update_fields=("active_for_new_finalizations", "updated_at"))
        CommercialAccountingPolicyVersion.objects.create(
            policy_key="conflicting-v1",
            version=1,
            commerce_authority="digital_products",
            customer_unapplied_funds_account=policy.customer_unapplied_funds_account,
            merchandise_revenue_account=policy.merchandise_revenue_account,
            shipping_revenue_account=policy.shipping_revenue_account,
            active_for_new_finalizations=True,
        )
        with self.assertRaisesMessage(CommandError, "conflicting active Digital Products"):
            self.run_accounting("--apply")
