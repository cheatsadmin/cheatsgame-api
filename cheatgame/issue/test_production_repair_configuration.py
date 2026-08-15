import json
from pathlib import Path
from tempfile import NamedTemporaryFile

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from cheatgame.issue.models import Issue, IssueTag, Tag
from cheatgame.shop.models import DeliveryType


class ProductionRepairConfigurationCommandTests(TestCase):
    def setUp(self):
        self.manifest = {
            "schema": "cheatsg.repair-configuration-promotion.v1",
            "issues": [{
                "classification": "PRODUCTION_READY",
                "title": "مشکل کنترل‌شده",
                "picture_storage_key": "repair/problem.webp",
                "description_storage_key": "repair/problem.html",
                "min_price": "0",
                "max_price": "0",
                "is_active": True,
                "sort_order": 1,
                "tags": [{"title": "Controller", "issue_type": 2}],
            }, {
                "classification": "EXCLUDE_STAGING_TEST",
                "title": "Staging seed",
            }],
            "delivery_types": [{
                "classification": "PRODUCTION_READY",
                "name": "تحویل حضوری",
                "delivery_type": 1,
                "side": 1,
            }],
        }

    def write_manifest(self, payload=None):
        handle = NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(payload or self.manifest, handle, ensure_ascii=False)
        handle.close()
        self.addCleanup(Path(handle.name).unlink, missing_ok=True)
        return handle.name

    def test_dry_run_is_read_only(self):
        call_command("import_production_repair_configuration", self.write_manifest())
        self.assertEqual(Issue.objects.count(), 0)
        self.assertEqual(DeliveryType.objects.count(), 0)

    def test_apply_is_idempotent_and_excludes_non_ready_records(self):
        path = self.write_manifest()
        call_command("import_production_repair_configuration", path, apply=True)
        call_command("import_production_repair_configuration", path, apply=True)
        self.assertEqual(Issue.objects.count(), 1)
        self.assertEqual(Tag.objects.count(), 1)
        self.assertEqual(IssueTag.objects.count(), 1)
        self.assertEqual(DeliveryType.objects.count(), 1)
        self.assertFalse(Issue.objects.filter(title="Staging seed").exists())

    def test_conflicting_replay_fails_closed(self):
        path = self.write_manifest()
        call_command("import_production_repair_configuration", path, apply=True)
        issue = Issue.objects.get()
        issue.title = "متفاوت"
        issue.save(update_fields=["title"])
        with self.assertRaises(CommandError):
            call_command("import_production_repair_configuration", path, apply=True)
