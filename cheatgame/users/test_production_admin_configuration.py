from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from cheatgame.users.models import BaseUser, UserTypes


BOOTSTRAP_ENV = {
    "CHEATSGAME_BOOTSTRAP_ADMIN_PHONE": "09120000001",
    "CHEATSGAME_BOOTSTRAP_ADMIN_FIRST_NAME": "Production",
    "CHEATSGAME_BOOTSTRAP_ADMIN_LAST_NAME": "Owner",
    "CHEATSGAME_BOOTSTRAP_ADMIN_PASSWORD": "OnlyForDisposableDb!2468",
}


class ProductionAdminConfigurationTests(TestCase):
    def run_command(self, *, apply=False, overrides=None):
        values = {**BOOTSTRAP_ENV, **(overrides or {})}
        with patch.dict("os.environ", values, clear=False):
            call_command("configure_production_admin", apply=apply)

    def test_dry_run_does_not_create_identity(self):
        self.run_command()

        self.assertFalse(BaseUser.objects.exists())

    def test_apply_creates_exact_admin_and_replay_is_idempotent(self):
        self.run_command(apply=True)
        self.run_command(apply=True)

        self.assertEqual(BaseUser.objects.count(), 1)
        user = BaseUser.objects.get()
        self.assertEqual(user.user_type, UserTypes.ADMIN)
        self.assertTrue(user.is_active)
        self.assertTrue(user.is_admin)
        self.assertTrue(user.is_superuser)
        self.assertTrue(user.phone_verified)
        self.assertTrue(user.check_password(BOOTSTRAP_ENV["CHEATSGAME_BOOTSTRAP_ADMIN_PASSWORD"]))

    def test_missing_password_fails_closed_for_new_identity(self):
        with self.assertRaises(CommandError):
            self.run_command(
                apply=True,
                overrides={"CHEATSGAME_BOOTSTRAP_ADMIN_PASSWORD": ""},
            )

    def test_conflicting_existing_identity_is_rejected(self):
        BaseUser.objects.create_user(
            phone_number=BOOTSTRAP_ENV["CHEATSGAME_BOOTSTRAP_ADMIN_PHONE"],
            firstname="Customer",
            lastname="Conflict",
            password="AnotherDisposable!2468",
        )

        with self.assertRaises(CommandError):
            self.run_command(apply=True)
