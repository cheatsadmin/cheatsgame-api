import logging
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.http import Http404
from django.shortcuts import get_object_or_404

from rest_framework import serializers


logger = logging.getLogger(__name__)


def make_mock_object(**kwargs):
    return type("", (object, ), kwargs)


def get_object(model_or_queryset, **kwargs):
    """
    Reuse get_object_or_404 since the implementation supports both Model && queryset.
    Catch Http404 & return None
    """
    try:
        return get_object_or_404(model_or_queryset, **kwargs)
    except Http404:
        return None


def create_serializer_class(name, fields):
    return type(name, (serializers.Serializer, ), fields)


def inline_serializer(*, fields, data=None, **kwargs):
    serializer_class = create_serializer_class(name='', fields=fields)

    if data is not None:
        return serializer_class(data=data, **kwargs)

    return serializer_class(**kwargs)


def assert_settings(required_settings, error_message_prefix=""):
    """
    Checks if each item from `required_settings` is present in Django settings
    """
    not_present = []
    values = {}

    for required_setting in required_settings:
        if not hasattr(settings, required_setting):
            not_present.append(required_setting)
            continue

        values[required_setting] = getattr(settings, required_setting)

    if not_present:
        if not error_message_prefix:
            error_message_prefix = "Required settings not found."

        stringified_not_present = ", ".join(not_present)

        raise ImproperlyConfigured(f"{error_message_prefix} Could not find: {stringified_not_present}")

    return values

def reformat_url(*, url:str) -> str:
    index = url.find("?")
    return url[:index] if index != -1 else url


def safe_file_url(*, file, fallback: str = "") -> str:
    if not file:
        return fallback

    try:
        return reformat_url(url=file.url)
    except Exception:
        name = str(getattr(file, "name", "") or "").lstrip("/")
        logger.warning("Could not build storage URL for file %s", name, exc_info=True)

    if not name:
        return fallback

    endpoint_url = getattr(settings, "AWS_S3_ENDPOINT_URL", "") or ""
    bucket_name = getattr(settings, "AWS_STORAGE_BUCKET_NAME", "") or ""
    parsed_endpoint = urlsplit(endpoint_url)
    if parsed_endpoint.scheme in ("http", "https") and parsed_endpoint.netloc:
        base_url = endpoint_url.rstrip("/")
        bucket_path = bucket_name.strip("/")
        return f"{base_url}/{bucket_path}/{name}" if bucket_path else f"{base_url}/{name}"

    media_url = getattr(settings, "MEDIA_URL", "") or ""
    if media_url and media_url != "/":
        return f"{media_url.rstrip('/')}/{name}"

    return name
