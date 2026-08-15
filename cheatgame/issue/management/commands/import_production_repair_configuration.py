import json
from decimal import Decimal
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from cheatgame.issue.models import Issue, IssueTag, Tag
from cheatgame.shop.models import DeliveryType


class Command(BaseCommand):
    help = "Dry-run or idempotently import reviewed Production repair configuration."

    def add_arguments(self, parser):
        parser.add_argument("manifest")
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        del args
        payload = self._load(options["manifest"])
        issues = [
            item for item in payload["issues"]
            if item.get("classification") == "PRODUCTION_READY"
        ]
        delivery_types = [
            item for item in payload["delivery_types"]
            if item.get("classification") == "PRODUCTION_READY"
        ]
        self._validate(issues, delivery_types)
        skipped = len(payload["issues"]) - len(issues)
        if not options["apply"]:
            self.stdout.write(
                "dry_run=true "
                f"issues={len(issues)} delivery_types={len(delivery_types)} skipped={skipped}"
            )
            return
        with transaction.atomic():
            self._apply_issues(issues)
            self._apply_delivery_types(delivery_types)
        self.stdout.write(
            self.style.SUCCESS(
                "Production repair configuration import complete: "
                f"issues={len(issues)} delivery_types={len(delivery_types)} skipped={skipped}"
            )
        )

    @staticmethod
    def _load(filename):
        try:
            payload = json.loads(Path(filename).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CommandError("Repair configuration manifest is unreadable or invalid JSON.") from exc
        if payload.get("schema") != "cheatsg.repair-configuration-promotion.v1":
            raise CommandError("Unsupported repair configuration manifest schema.")
        if not isinstance(payload.get("issues"), list) or not isinstance(
            payload.get("delivery_types"), list
        ):
            raise CommandError("Repair configuration lists are required.")
        return payload

    @staticmethod
    def _validate(issues, delivery_types):
        pictures = [str(item.get("picture_storage_key") or "") for item in issues]
        if any(not picture for picture in pictures) or len(pictures) != len(set(pictures)):
            raise CommandError("Production-ready repair pictures must be non-empty and unique.")
        for item in issues:
            for field in ("title", "description_storage_key", "tags"):
                if not item.get(field):
                    raise CommandError(f"Production-ready repair issue is missing {field}.")
            if item.get("is_active") is not True:
                raise CommandError("Only active repair issues may be Production-ready.")
            for tag in item["tags"]:
                if not tag.get("title") or tag.get("issue_type") not in (1, 2):
                    raise CommandError("Repair issue tags must have a title and valid issue type.")
        identities = [(item.get("name"), item.get("side")) for item in delivery_types]
        if any(not name for name, _ in identities) or len(identities) != len(set(identities)):
            raise CommandError("Delivery type name/side identities must be non-empty and unique.")
        if any(item.get("delivery_type") not in (1, 2, 3) or item.get("side") not in (1, 2) for item in delivery_types):
            raise CommandError("Delivery type option or side is invalid.")

    @staticmethod
    def _ensure_exact(instance, expected, label):
        mismatches = []
        for field, value in expected.items():
            actual = getattr(instance, field)
            if hasattr(actual, "name"):
                actual = actual.name
            if actual != value:
                mismatches.append(field)
        if mismatches:
            raise CommandError(f"Existing {label} conflicts in: " + ", ".join(sorted(mismatches)))

    def _apply_issues(self, issues):
        for item in issues:
            expected = {
                "title": item["title"],
                "description": item["description_storage_key"],
                "min_price": Decimal(str(item["min_price"])),
                "max_price": Decimal(str(item["max_price"])),
                "is_active": True,
                "sort_order": item["sort_order"],
            }
            issue, _ = Issue.objects.get_or_create(
                picture=item["picture_storage_key"], defaults=expected
            )
            self._ensure_exact(issue, expected, f"repair issue {item['title']}")
            expected_tag_ids = set()
            for tag_record in item["tags"]:
                tag, _ = Tag.objects.get_or_create(
                    title=tag_record["title"],
                    issue_type=tag_record["issue_type"],
                )
                expected_tag_ids.add(tag.id)
                IssueTag.objects.get_or_create(issue=issue, tag=tag)
            actual_tag_ids = set(issue.tags.values_list("tag_id", flat=True))
            if actual_tag_ids != expected_tag_ids:
                raise CommandError(f"Existing repair issue {item['title']} has conflicting tags.")

    def _apply_delivery_types(self, delivery_types):
        for item in delivery_types:
            expected = {"delivery_type": item["delivery_type"]}
            instance, _ = DeliveryType.objects.get_or_create(
                name=item["name"], side=item["side"], defaults=expected
            )
            self._ensure_exact(instance, expected, f"delivery type {item['name']}")
