from unittest.mock import PropertyMock, patch

from django.test import TestCase, override_settings
from rest_framework import status
from rest_framework.test import APIRequestFactory

from cheatgame.common.utils import safe_file_url
from cheatgame.general.apis import BannerListApi
from cheatgame.general.models import Banner


class SafeFileUrlTests(TestCase):
    class BrokenFile:
        name = "images/banner.png"

        @property
        def url(self):
            raise ValueError("Invalid endpoint")

    @override_settings(
        AWS_S3_ENDPOINT_URL="https://storage.iran.liara.space",
        AWS_STORAGE_BUCKET_NAME="cheatsgame-storage",
    )
    def test_safe_file_url_falls_back_to_s3_path(self):
        self.assertEqual(
            safe_file_url(file=self.BrokenFile()),
            "https://storage.iran.liara.space/cheatsgame-storage/images/banner.png",
        )

    @override_settings(AWS_S3_ENDPOINT_URL="<invalid>", AWS_STORAGE_BUCKET_NAME="cheatsgame-storage")
    def test_safe_file_url_falls_back_to_file_name_for_invalid_endpoint(self):
        self.assertEqual(safe_file_url(file=self.BrokenFile()), "images/banner.png")


class BannerListApiTests(TestCase):
    @override_settings(
        AWS_S3_ENDPOINT_URL="https://storage.iran.liara.space",
        AWS_STORAGE_BUCKET_NAME="cheatsgame-storage",
    )
    def test_banner_list_returns_success_when_storage_url_generation_fails(self):
        Banner.objects.create(
            picture="images/banner.png",
            link="https://example.com/products",
            location=1,
        )

        request = APIRequestFactory().get("/api/general/banner-list/")
        with patch(
            "django.db.models.fields.files.FieldFile.url",
            new_callable=PropertyMock,
            side_effect=ValueError("Invalid endpoint"),
        ):
            response = BannerListApi.as_view()(request)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data[0]["picture"],
            "https://storage.iran.liara.space/cheatsgame-storage/images/banner.png",
        )
