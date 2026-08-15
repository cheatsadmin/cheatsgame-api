import json

from django.conf import settings
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand, CommandError

from cheatgame.general.models import Banner, Blog, BlogStatus, Slider, Story
from cheatgame.product.models import Product, ProductStatus


DEFAULT_CACHE_CONTROL = "public, max-age=31536000, immutable"


def public_image_keys():
    keys = set()

    def add(field):
        name = getattr(field, "name", "")
        if name:
            keys.add(name)

    products = Product.objects.filter(status=ProductStatus.PUBLISHED).prefetch_related("images")
    for product in products:
        add(product.main_image)
        for image in product.images.all():
            add(image.file)
    for story in Story.objects.filter(is_active=True):
        add(story.picture)
        add(story.content_picture)
    for slider in Slider.objects.filter(is_active=True):
        add(slider.laptop_picture)
        add(slider.middle_picture)
        add(slider.mobile_picture)
        add(slider.hero_artwork_image)
    for banner in Banner.objects.filter(is_active=True):
        add(banner.picture)
    for blog in Blog.objects.filter(status=BlogStatus.PUBLISHED):
        add(blog.picture)
    return sorted(keys)


def _replacement_metadata(head):
    values = {"Metadata": head.get("Metadata", {}), "MetadataDirective": "REPLACE"}
    for key in (
        "ContentDisposition",
        "ContentEncoding",
        "ContentLanguage",
        "ContentType",
        "Expires",
        "WebsiteRedirectLocation",
    ):
        if head.get(key) is not None:
            values[key] = head[key]
    return values


class Command(BaseCommand):
    help = "Inspect or apply immutable cache metadata to public Production image objects."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--cache-control", default=DEFAULT_CACHE_CONTROL)

    def handle(self, *args, **options):
        del args
        if getattr(settings, "AWS_STORAGE_ENVIRONMENT", "") != "production":
            raise CommandError("Public image cache metadata may be changed only in Production.")
        bucket = getattr(settings, "AWS_STORAGE_BUCKET_NAME", "")
        if not bucket:
            raise CommandError("AWS_STORAGE_BUCKET_NAME is required.")
        try:
            client = default_storage.connection.meta.client
        except AttributeError as exc:
            raise CommandError("The configured storage backend is not S3-compatible.") from exc

        policy = options["cache_control"]
        inspected = changed = unchanged = 0
        failures = []
        for key in public_image_keys():
            try:
                before = client.head_object(Bucket=bucket, Key=key)
                inspected += 1
                if before.get("CacheControl") == policy:
                    unchanged += 1
                    continue
                if not options["apply"]:
                    changed += 1
                    continue
                client.copy_object(
                    Bucket=bucket,
                    Key=key,
                    CopySource={"Bucket": bucket, "Key": key},
                    CacheControl=policy,
                    **_replacement_metadata(before),
                )
                after = client.head_object(Bucket=bucket, Key=key)
                if (
                    after.get("CacheControl") != policy
                    or after.get("ContentLength") != before.get("ContentLength")
                    or after.get("ETag") != before.get("ETag")
                ):
                    raise CommandError("Object verification failed after metadata replacement.")
                changed += 1
            except Exception as exc:
                failures.append({"key": key, "error": type(exc).__name__})

        result = {
            "mode": "apply" if options["apply"] else "inspect",
            "cache_control": policy,
            "inspected": inspected,
            "changed_or_pending": changed,
            "unchanged": unchanged,
            "failures": failures,
        }
        self.stdout.write(json.dumps(result, sort_keys=True))
        if failures:
            raise CommandError("One or more public image objects could not be verified.")
