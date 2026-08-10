import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from cheatgame.general.models import Banner, Blog, CommonQuestion, Slider, Story


class Command(BaseCommand):
    help = "Dry-run or import an owner-approved public CMS manifest idempotently."

    def add_arguments(self, parser):
        parser.add_argument("manifest")
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        del args
        payload = self._load(options["manifest"])
        counts = {
            key: len(payload[key])
            for key in ("stories", "sliders", "banners", "blogs", "common_questions")
        }
        self._validate(payload)
        if not options["apply"]:
            self.stdout.write("dry_run=true " + " ".join(f"{key}={value}" for key, value in counts.items()))
            return
        with transaction.atomic():
            self._apply(payload)
        self.stdout.write(self.style.SUCCESS("Production public content import complete: " + " ".join(f"{key}={value}" for key, value in counts.items())))

    @staticmethod
    def _load(filename):
        try:
            payload = json.loads(Path(filename).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CommandError("Public content manifest is unreadable or invalid JSON.") from exc
        if payload.get("schema") != "cheatsg.public-content-promotion.v1":
            raise CommandError("Unsupported public content manifest schema.")
        return payload

    @staticmethod
    def _validate(payload):
        for key in ("stories", "sliders", "banners", "blogs", "common_questions"):
            if not isinstance(payload.get(key), list):
                raise CommandError(f"Manifest field {key} must be a list.")
        slugs = [item.get("slug") for item in payload["blogs"]]
        if any(not slug for slug in slugs) or len(slugs) != len(set(slugs)):
            raise CommandError("Blog slugs must be non-empty and unique.")
        locations = [item.get("location") for item in payload["banners"]]
        if len(locations) != len(set(locations)):
            raise CommandError("Banner locations must be unique.")

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

    def _upsert_exact(self, model, lookup, expected, label):
        instance, _ = model.objects.get_or_create(**lookup, defaults=expected)
        self._ensure_exact(instance, expected, label)

    def _apply(self, payload):
        for item in payload["stories"]:
            expected = {key: item.get(key) for key in ("content_picture", "link", "title", "is_active", "sort_order", "alt_text")}
            self._upsert_exact(Story, {"picture": item["picture"]}, expected, "Story")
        for item in payload["sliders"]:
            expected = {key: item.get(key) for key in (
                "middle_picture", "mobile_picture", "link", "is_active", "sort_order", "alt_text",
                "hero_eyebrow", "hero_headline", "hero_highlight", "hero_subtitle",
                "hero_primary_label", "hero_primary_link", "hero_secondary_label",
                "hero_secondary_link", "hero_artwork_image",
            )}
            self._upsert_exact(Slider, {"laptop_picture": item["laptop_picture"]}, expected, "Slider")
        for item in payload["banners"]:
            expected = {key: item.get(key) for key in ("picture", "link", "is_active", "sort_order", "alt_text")}
            self._upsert_exact(Banner, {"location": item["location"]}, expected, "Banner")
        for item in payload["blogs"]:
            expected = {key: item.get(key) for key in ("title", "content", "picture", "status", "seo_title", "meta_description")}
            self._upsert_exact(Blog, {"slug": item["slug"]}, expected, f"Blog {item['slug']}")
        for item in payload["common_questions"]:
            expected = {"answer": item["answer"]}
            self._upsert_exact(
                CommonQuestion,
                {"question_location": item["question_location"], "question": item["question"]},
                expected,
                "CommonQuestion",
            )
