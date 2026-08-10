from io import BytesIO

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase
from PIL import Image

from cheatgame.common.upload_fields import SecureHtmlUploadField, SecureImageUploadField


def _image_upload(*, name="cover.webp", content_type="image/webp"):
    payload = BytesIO()
    Image.new("RGB", (8, 10), color=(20, 40, 60)).save(payload, format="WEBP")
    return SimpleUploadedFile(name, payload.getvalue(), content_type=content_type)


class SecureUploadFieldTests(SimpleTestCase):
    def test_valid_webp_image_is_accepted_and_filename_is_flattened(self):
        upload = SecureImageUploadField().run_validation(
            _image_upload(name="../../unsafe/cover.webp")
        )
        self.assertEqual(upload.name, "cover.webp")

    def test_svg_and_mime_extension_mismatch_are_rejected(self):
        svg = SimpleUploadedFile(
            "cover.svg",
            b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
            content_type="image/svg+xml",
        )
        with self.assertRaisesMessage(Exception, "JPG"):
            SecureImageUploadField().run_validation(svg)

        with self.assertRaisesMessage(Exception, "JPG"):
            SecureImageUploadField().run_validation(
                _image_upload(name="cover.jpg", content_type="image/png")
            )

    def test_html_is_sanitized_before_service_receives_it(self):
        upload = SimpleUploadedFile(
            "description.html",
            (
                b'<p class="lead">safe</p><script>alert(1)</script>'
                b'<a href="javascript:alert(2)" onclick="alert(3)">link</a>'
            ),
            content_type="text/html",
        )
        sanitized = SecureHtmlUploadField().run_validation(upload).read().decode("utf-8")
        self.assertIn('<p class="lead">safe</p>', sanitized)
        self.assertNotIn("<script", sanitized)
        self.assertNotIn("javascript:", sanitized)
        self.assertNotIn("onclick", sanitized)

    def test_admin_generated_html_with_text_plain_mime_is_sanitized(self):
        upload = SimpleUploadedFile(
            "product-description.html",
            b'<p>safe</p><script>alert(1)</script>',
            content_type="text/plain;charset=utf-8",
        )
        sanitized = SecureHtmlUploadField().run_validation(upload).read().decode("utf-8")
        self.assertEqual(sanitized, "<p>safe</p>alert(1)")

    def test_non_html_content_upload_is_rejected(self):
        upload = SimpleUploadedFile(
            "description.txt", b"plain", content_type="text/plain"
        )
        with self.assertRaisesMessage(Exception, "HTML"):
            SecureHtmlUploadField().run_validation(upload)
