import os

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from cheatgame.users.models import BaseUser, UserTypes
from cheatgame.users.validators import normalize_iranian_phone_number


class Command(BaseCommand):
    help = (
        "Inspect or create the one explicitly configured Production bootstrap "
        "Admin without importing a staging identity."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        del args
        phone = normalize_iranian_phone_number(
            os.environ.get("CHEATSGAME_BOOTSTRAP_ADMIN_PHONE", "")
        )
        first_name = os.environ.get("CHEATSGAME_BOOTSTRAP_ADMIN_FIRST_NAME", "").strip()
        last_name = os.environ.get("CHEATSGAME_BOOTSTRAP_ADMIN_LAST_NAME", "").strip()
        password = os.environ.get("CHEATSGAME_BOOTSTRAP_ADMIN_PASSWORD", "")
        if not phone or not first_name or not last_name:
            raise CommandError(
                "Bootstrap Admin identity variables must be explicitly configured."
            )

        existing = BaseUser.objects.filter(phone_number=phone).first()
        if existing:
            self._require_exact_admin(existing)
            self.stdout.write("admin_bootstrap=ready existing=true")
            return

        if not password:
            raise CommandError(
                "CHEATSGAME_BOOTSTRAP_ADMIN_PASSWORD is required to create the Admin."
            )
        candidate = BaseUser(
            phone_number=phone,
            firstname=first_name,
            lastname=last_name,
            is_active=True,
            is_admin=True,
            is_superuser=True,
            user_type=UserTypes.ADMIN,
            phone_verified=True,
        )
        try:
            validate_password(password, user=candidate)
        except ValidationError as exc:
            raise CommandError("Bootstrap Admin password does not meet policy.") from exc

        if not options["apply"]:
            self.stdout.write("admin_bootstrap=planned existing=false dry_run=true")
            return

        with transaction.atomic():
            if BaseUser.objects.select_for_update().filter(phone_number=phone).exists():
                raise CommandError(
                    "Bootstrap Admin identity appeared concurrently; inspect before retrying."
                )
            candidate.set_password(password)
            candidate.full_clean()
            candidate.save()
        self.stdout.write(self.style.SUCCESS("admin_bootstrap=created existing=false"))

    @staticmethod
    def _require_exact_admin(user):
        if not (
            user.is_active
            and user.is_admin
            and user.is_superuser
            and user.phone_verified
            and user.user_type == UserTypes.ADMIN
            and user.has_usable_password()
        ):
            raise CommandError(
                "Existing bootstrap identity conflicts with the required Admin authority."
            )
