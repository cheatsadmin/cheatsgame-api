from io import StringIO
from unittest.mock import patch

from django.core.files.storage import default_storage
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from cheatgame.common.management.commands.verify_public_media_delivery import (
    HTML_PROBE,
    PNG_PROBE,
)


TEST_STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.InMemoryStorage",
        "OPTIONS": {"base_url": "https://cdn.cheatsg.ir/"},
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}


@override_settings(
    STORAGES=TEST_STORAGES,
    AWS_S3_ENDPOINT_URL="https://storage.example.test",
    AWS_STORAGE_BUCKET_NAME="production-media",
)
class VerifyPublicMediaDeliveryTests(SimpleTestCase):
    def test_command_verifies_html_and_image_and_cleans_up(self):
        def fetch(url):
            if url.endswith(".html"):
                return 200, "text/html", HTML_PROBE
            return 200, "image/png", PNG_PROBE

        stdout = StringIO()
        with patch(
            "cheatgame.common.management.commands.verify_public_media_delivery.fetch_public_url",
            side_effect=fetch,
        ):
            call_command("verify_public_media_delivery", stdout=stdout)

        self.assertIn('"content_type": "text/html"', stdout.getvalue())
        self.assertIn('"content_type": "image/png"', stdout.getvalue())
        self.assertEqual(default_storage.listdir("production/healthchecks"), ([], []))

    def test_command_fails_on_public_404_and_cleans_up(self):
        with patch(
            "cheatgame.common.management.commands.verify_public_media_delivery.fetch_public_url",
            return_value=(404, "application/xml", b"missing"),
        ):
            with self.assertRaisesMessage(CommandError, "HTTP 404"):
                call_command("verify_public_media_delivery")

        self.assertEqual(default_storage.listdir("production/healthchecks"), ([], []))
