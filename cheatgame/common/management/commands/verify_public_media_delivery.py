import base64
import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand, CommandError

from cheatgame.common.utils import storage_origin_file_url


HTML_PROBE = b"<p>Cheats Game public media delivery check</p>"
PNG_PROBE = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def fetch_public_url(url: str):
    request = Request(url, headers={"User-Agent": "CheatsGameMediaVerifier/1.0"})
    try:
        with urlopen(request, timeout=20) as response:
            return (
                response.status,
                response.headers.get_content_type(),
                response.read(),
            )
    except HTTPError as exc:
        return exc.code, exc.headers.get_content_type(), exc.read()
    except (OSError, URLError) as exc:
        raise CommandError(f"Public media verification could not reach {url}.") from exc


class Command(BaseCommand):
    help = (
        "Write temporary HTML and image objects through Django storage, verify "
        "their public URLs byte-for-byte, and remove the probes."
    )

    def handle(self, *args, **options):
        del args, options
        probe_id = uuid4().hex
        probes = (
            (
                f"production/healthchecks/media-delivery-{probe_id}.html",
                HTML_PROBE,
                "text/html",
            ),
            (
                f"production/healthchecks/media-delivery-{probe_id}.png",
                PNG_PROBE,
                "image/png",
            ),
        )
        saved_keys = []
        results = []

        try:
            for key, payload, expected_content_type in probes:
                saved_key = default_storage.save(key, ContentFile(payload))
                saved_keys.append(saved_key)
                if saved_key != key:
                    raise CommandError(
                        f"Storage changed the deterministic probe key from {key} to {saved_key}."
                    )
                if not default_storage.exists(saved_key):
                    raise CommandError(f"Stored probe does not exist: {saved_key}.")
                with default_storage.open(saved_key, "rb") as stored:
                    if stored.read() != payload:
                        raise CommandError(f"Stored probe bytes differ for {saved_key}.")

                public_url = storage_origin_file_url(file=saved_key).split("?", 1)[0]
                status, content_type, delivered = fetch_public_url(public_url)
                if status != 200:
                    raise CommandError(
                        f"Public probe retrieval returned HTTP {status} for {saved_key}."
                    )
                if delivered != payload:
                    raise CommandError(f"Public probe bytes differ for {saved_key}.")
                if content_type != expected_content_type:
                    raise CommandError(
                        f"Public probe content type {content_type!r} does not match "
                        f"{expected_content_type!r} for {saved_key}."
                    )
                results.append(
                    {
                        "key": saved_key,
                        "status": status,
                        "content_type": content_type,
                        "byte_size": len(payload),
                    }
                )
        finally:
            for key in saved_keys:
                try:
                    if default_storage.exists(key):
                        default_storage.delete(key)
                except Exception:
                    self.stderr.write(f"Could not remove temporary media probe {key}.")

        self.stdout.write(json.dumps({"verified": results}, sort_keys=True))
