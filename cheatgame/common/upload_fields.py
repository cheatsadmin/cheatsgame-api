from io import BytesIO
from pathlib import Path

import bleach
from django.core.files.base import ContentFile
from PIL import Image, UnidentifiedImageError
from rest_framework import serializers


MAX_IMAGE_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_HTML_UPLOAD_BYTES = 2 * 1024 * 1024

ALLOWED_IMAGE_FORMATS = {"JPEG", "PNG", "WEBP", "AVIF"}
ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".avif"}
ALLOWED_IMAGE_CONTENT_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/avif",
}
IMAGE_FORMAT_POLICY = {
    "JPEG": ({".jpg", ".jpeg"}, {"image/jpeg"}),
    "PNG": ({".png"}, {"image/png"}),
    "WEBP": ({".webp"}, {"image/webp"}),
    "AVIF": ({".avif"}, {"image/avif"}),
}

ALLOWED_HTML_TAGS = {
    "a", "blockquote", "br", "code", "div", "em", "figcaption", "figure",
    "h1", "h2", "h3", "h4", "h5", "h6", "hr", "img", "li", "ol", "p",
    "pre", "span", "strong", "table", "tbody", "td", "th", "thead", "tr", "ul",
}
ALLOWED_HTML_ATTRIBUTES = {
    "*": ["class", "dir", "lang"],
    "a": ["href", "rel", "target", "title"],
    "img": ["alt", "height", "loading", "src", "title", "width"],
    "td": ["colspan", "rowspan"],
    "th": ["colspan", "rowspan", "scope"],
}


def _safe_upload_name(name: str, *, fallback: str) -> str:
    basename = Path(name or "").name
    if not basename or basename in {".", ".."}:
        return fallback
    return basename[:180]


class SecureImageUploadField(serializers.FileField):
    default_error_messages = {
        "too_large": "حجم تصویر نباید بیشتر از ۸ مگابایت باشد.",
        "invalid_image": "فایل باید یک تصویر معتبر JPG، PNG، WebP یا AVIF باشد.",
    }

    def to_internal_value(self, data):
        upload = super().to_internal_value(data)
        if upload.size > MAX_IMAGE_UPLOAD_BYTES:
            self.fail("too_large")

        extension = Path(upload.name or "").suffix.lower()
        content_type = (getattr(upload, "content_type", "") or "").lower()
        if extension not in ALLOWED_IMAGE_EXTENSIONS or content_type not in ALLOWED_IMAGE_CONTENT_TYPES:
            self.fail("invalid_image")

        try:
            upload.seek(0)
            payload = upload.read(MAX_IMAGE_UPLOAD_BYTES + 1)
            with Image.open(BytesIO(payload)) as image:
                image.verify()
                image_format = (image.format or "").upper()
                if image_format not in ALLOWED_IMAGE_FORMATS:
                    self.fail("invalid_image")
                valid_extensions, valid_content_types = IMAGE_FORMAT_POLICY[image_format]
                if extension not in valid_extensions or content_type not in valid_content_types:
                    self.fail("invalid_image")
        except (OSError, UnidentifiedImageError, ValueError):
            self.fail("invalid_image")
        finally:
            upload.seek(0)
        upload.name = _safe_upload_name(upload.name, fallback="image")
        return upload


class SecureHtmlUploadField(serializers.FileField):
    default_error_messages = {
        "too_large": "حجم محتوای متنی نباید بیشتر از ۲ مگابایت باشد.",
        "invalid_html": "فایل محتوا باید HTML معتبر با کدگذاری UTF-8 باشد.",
    }

    def to_internal_value(self, data):
        upload = super().to_internal_value(data)
        if upload.size > MAX_HTML_UPLOAD_BYTES:
            self.fail("too_large")
        if Path(upload.name or "").suffix.lower() not in {".html", ".htm"}:
            self.fail("invalid_html")
        content_type = (getattr(upload, "content_type", "") or "").lower().split(";", 1)[0].strip()
        # The approved Admin historically sends generated *.html descriptions as
        # text/plain. The extension plus mandatory UTF-8 decode and sanitization
        # remain authoritative; accepting that non-executable MIME preserves the
        # deployed contract without accepting arbitrary binary content.
        if content_type not in {"text/html", "application/xhtml+xml", "text/plain"}:
            self.fail("invalid_html")
        try:
            upload.seek(0)
            source = upload.read(MAX_HTML_UPLOAD_BYTES + 1).decode("utf-8")
        except (UnicodeDecodeError, OSError):
            self.fail("invalid_html")
        sanitized = bleach.clean(
            source,
            tags=ALLOWED_HTML_TAGS,
            attributes=ALLOWED_HTML_ATTRIBUTES,
            protocols={"https", "mailto"},
            strip=True,
            strip_comments=True,
        )
        return ContentFile(
            sanitized.encode("utf-8"),
            name=_safe_upload_name(upload.name, fallback="content.html"),
        )
