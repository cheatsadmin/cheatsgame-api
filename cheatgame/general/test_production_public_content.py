import json
from pathlib import Path
from tempfile import TemporaryDirectory

from django.core.management import call_command
from django.test import TestCase

from cheatgame.general.models import Banner, CommonQuestion, Story


class ProductionPublicContentCommandTests(TestCase):
    def _manifest_path(self, payload):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "public-content.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_public_manifest_excludes_private_customer_and_message_data(self):
        Story.objects.create(
            picture="public/story.webp",
            content_picture="public/story-content.webp",
            link="https://cheatsg.ir/Game",
            title="Public story",
            alt_text="Story cover",
        )
        output = __import__("io").StringIO()
        call_command("public_content_promotion_manifest", stdout=output)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["schema"], "cheatsg.public-content-promotion.v1")
        self.assertEqual(payload["stories"][0]["picture"], "public/story.webp")
        self.assertNotIn("users", payload)
        self.assertNotIn("comments", payload)
        self.assertNotIn("contact_forms", payload)
        self.assertNotIn("messages", payload)

    def test_import_is_dry_run_by_default_and_apply_is_idempotent(self):
        payload = {
            "schema": "cheatsg.public-content-promotion.v1",
            "stories": [],
            "sliders": [],
            "blogs": [],
            "banners": [
                {
                    "picture": "public/banner.webp",
                    "link": "https://cheatsg.ir/Game",
                    "location": 1,
                    "is_active": True,
                    "sort_order": 0,
                    "alt_text": "Game banner",
                }
            ],
            "common_questions": [
                {"question_location": 1, "question": "Question?", "answer": "Answer."}
            ],
        }
        path = self._manifest_path(payload)
        call_command("import_production_public_content", str(path))
        self.assertFalse(Banner.objects.exists())
        call_command("import_production_public_content", str(path), apply=True)
        call_command("import_production_public_content", str(path), apply=True)
        self.assertEqual(Banner.objects.count(), 1)
        self.assertEqual(CommonQuestion.objects.count(), 1)
