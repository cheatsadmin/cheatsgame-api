from unittest.mock import PropertyMock, patch

import requests
from django.test import TestCase, override_settings
from rest_framework import status
from rest_framework.test import APIClient, APIRequestFactory

from cheatgame.common.utils import safe_file_url
from cheatgame.general.filters import BlogFilter
from cheatgame.general.apis import BannerListApi
from cheatgame.general.models import Banner, Blog, BlogCategory, BlogStatus
from cheatgame.general.services import create_blog, update_blog_category
from cheatgame.product.models import Category, CategoryType
from cheatgame.users.models import BaseUser, UserTypes


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


class BlogCmsStabilizationTests(TestCase):
    def test_blog_search_uses_sqlite_safe_fallback(self):
        matching_blog = Blog.objects.create(
            title="راهنمای تعمیر HDMI",
            slug="repair-hdmi-guide",
            content="blogs/repair-hdmi-guide.html",
            picture="blogs/repair-hdmi-guide.png",
        )
        Blog.objects.create(
            title="خبر فروشگاه",
            slug="shop-news",
            content="blogs/shop-news.html",
            picture="blogs/shop-news.png",
        )

        results = BlogFilter({"search": "HDMI"}, Blog.objects.all()).qs

        self.assertEqual(list(results), [matching_blog])

    def test_blog_search_finds_slug(self):
        matching_blog = Blog.objects.create(
            title="راهنمای تعمیر کنسول",
            slug="ps5-temperature-error",
            content="blogs/ps5-temperature-error.html",
            picture="blogs/ps5-temperature-error.png",
        )
        Blog.objects.create(
            title="خبر فروشگاه",
            slug="shop-news",
            content="blogs/shop-news.html",
            picture="blogs/shop-news.png",
        )

        results = BlogFilter({"search": "temperature"}, Blog.objects.all()).qs

        self.assertEqual(list(results), [matching_blog])

    def test_update_blog_category_updates_blog_relation(self):
        old_blog = Blog.objects.create(
            title="بلاگ قدیمی",
            slug="old-blog",
            content="blogs/old-blog.html",
            picture="blogs/old-blog.png",
        )
        new_blog = Blog.objects.create(
            title="بلاگ جدید",
            slug="new-blog",
            content="blogs/new-blog.html",
            picture="blogs/new-blog.png",
        )
        category = Category.objects.create(
            name="راهنما",
            slug="blog-guide",
            category_type=CategoryType.BLOG,
        )
        blog_category = BlogCategory.objects.create(blog=old_blog, category=category)

        update_blog_category(
            blog_category_id=blog_category.id,
            blog=new_blog,
            category=category,
        )

        blog_category.refresh_from_db()
        self.assertEqual(blog_category.blog, new_blog)


class BlogFoundationV2Tests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = BaseUser.objects.create_superuser(
            phone_number="09170009991",
            firstname="Admin",
            lastname="User",
            password="AdminPass123!",
            user_type=UserTypes.MANAGER,
        )

    def make_blog(self, *, slug: str, title: str = "مقاله تست", status_value: str = BlogStatus.DRAFT):
        return Blog.objects.create(
            title=title,
            slug=slug,
            status=status_value,
            content=f"blogs/{slug}.html",
            picture=f"blogs/{slug}.png",
            seo_title="",
            meta_description="",
        )

    def test_new_blog_defaults_to_draft(self):
        blog = create_blog(
            title="مقاله پیش نویس",
            content="blogs/draft.html",
            picture="blogs/draft.png",
        )

        self.assertEqual(blog.status, BlogStatus.DRAFT)

    def test_public_blog_list_and_detail_only_show_published_articles(self):
        draft = self.make_blog(slug="draft-post", title="پیش نویس", status_value=BlogStatus.DRAFT)
        published = self.make_blog(slug="published-post", title="منتشر شده", status_value=BlogStatus.PUBLISHED)

        list_response = self.client.get("/api/general/blog-list/")
        self.assertEqual(list_response.status_code, status.HTTP_200_OK)
        slugs = [item["slug"] for item in list_response.data["results"]]
        self.assertIn(published.slug, slugs)
        self.assertNotIn(draft.slug, slugs)

        draft_detail = self.client.get(f"/api/general/blog-detail/{draft.slug}/")
        self.assertEqual(draft_detail.status_code, status.HTTP_404_NOT_FOUND)

        published_detail = self.client.get(f"/api/general/blog-detail/{published.slug}/")
        self.assertEqual(published_detail.status_code, status.HTTP_200_OK)

    def test_admin_can_filter_drafts(self):
        draft = self.make_blog(slug="admin-draft", title="پیش نویس", status_value=BlogStatus.DRAFT)
        self.make_blog(slug="admin-published", title="منتشر شده", status_value=BlogStatus.PUBLISHED)

        self.client.force_authenticate(user=self.admin)
        response = self.client.get("/api/general/blog-list/", {"status": BlogStatus.DRAFT})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([item["slug"] for item in response.data["results"]], [draft.slug])

    def test_admin_blog_list_is_newest_first(self):
        older = self.make_blog(slug="older-admin-blog", title="قدیمی", status_value=BlogStatus.DRAFT)
        newer = self.make_blog(slug="newer-admin-blog", title="جدید", status_value=BlogStatus.DRAFT)
        self.client.force_authenticate(user=self.admin)

        response = self.client.get("/api/general/blog-list/", {"limit": 2, "offset": 0})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["results"][0]["slug"], newer.slug)
        self.assertEqual(response.data["results"][1]["slug"], older.slug)

    def test_title_edit_preserves_slug_and_updates_seo_fields(self):
        blog = self.make_blog(slug="stable-blog-slug", status_value=BlogStatus.DRAFT)
        self.client.force_authenticate(user=self.admin)

        response = self.client.put(
            f"/api/general/blog-detail/{blog.id}/",
            {
                "title": "عنوان جدید بدون تغییر اسلاگ",
                "status": BlogStatus.PUBLISHED,
                "seo_title": "عنوان سئو",
                "meta_description": "توضیحات متای مقاله",
            },
            format="multipart",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        blog.refresh_from_db()
        self.assertEqual(blog.slug, "stable-blog-slug")
        self.assertEqual(blog.status, BlogStatus.PUBLISHED)
        self.assertEqual(blog.seo_title, "عنوان سئو")
        self.assertEqual(blog.meta_description, "توضیحات متای مقاله")

    def test_duplicate_manual_slug_is_rejected(self):
        existing = self.make_blog(slug="duplicate-blog-slug", status_value=BlogStatus.PUBLISHED)
        target = self.make_blog(slug="target-blog-slug", status_value=BlogStatus.DRAFT)
        self.client.force_authenticate(user=self.admin)

        response = self.client.put(
            f"/api/general/blog-detail/{target.id}/",
            {
                "title": target.title,
                "slug": existing.slug,
                "status": BlogStatus.DRAFT,
            },
            format="multipart",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("slug", response.data["detail"])


class BlogAiDraftEndpointTests(TestCase):
    endpoint = "/api/general/admin/blog-ai-draft/"

    def setUp(self):
        self.client = APIClient()
        self.admin = BaseUser.objects.create_user(
            phone_number="09170009992",
            firstname="Admin",
            lastname="Writer",
            password="AdminPass123!",
            user_type=UserTypes.MANAGER,
        )
        self.customer = BaseUser.objects.create_user(
            phone_number="09170009993",
            firstname="Customer",
            lastname="User",
            password="CustomerPass123!",
            user_type=UserTypes.CUSTOMER,
        )
        self.payload = {
            "topic": "تعمیر دریفت دسته PS5",
            "primary_keyword": "تعمیر دریفت PS5",
            "secondary_keywords": ["دریفت آنالوگ PS5", "تعمیر دسته PS5"],
            "article_goal": "جذب مشتری برای ثبت تعمیر",
            "tone": "حرفه‌ای، ساده، قابل اعتماد",
            "target_audience": "گیمرهایی که دسته‌شان مشکل دریفت دارد",
        }

    def test_blog_ai_draft_rejects_unauthenticated_request(self):
        response = self.client.post(self.endpoint, self.payload, format="json")

        self.assertIn(response.status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))

    def test_blog_ai_draft_rejects_non_admin_request(self):
        self.client.force_authenticate(user=self.customer)

        response = self.client.post(self.endpoint, self.payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @override_settings(BLOG_AI_PROVIDER="openai_compatible", BLOG_AI_API_KEY="", BLOG_AI_MOCK_ENABLED=False)
    def test_blog_ai_draft_missing_api_key_returns_persian_error(self):
        self.client.force_authenticate(user=self.admin)

        response = self.client.post(self.endpoint, self.payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("تنظیمات دستیار هوشمند کامل نیست", response.data["error"])

    @override_settings(BLOG_AI_PROVIDER="mock", BLOG_AI_MOCK_ENABLED=True)
    def test_blog_ai_draft_mock_mode_returns_valid_structured_draft(self):
        self.client.force_authenticate(user=self.admin)

        response = self.client.post(self.endpoint, self.payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["version"], "blog_ai_draft_v1")
        self.assertEqual(response.data["meta"]["title"], "راهنمای تعمیر دریفت دسته PS5")
        self.assertTrue(response.data["blocks"])
        self.assertNotIn("<script", str(response.data).lower())

    @override_settings(BLOG_AI_PROVIDER="mock", BLOG_AI_MOCK_ENABLED=True)
    def test_blog_ai_draft_mock_topics_pass_quality_gate(self):
        topics = [
            "تعمیر دریفت دسته PS5",
            "ارور دما PS5",
            "مشکل تصویر ندادن PS5",
            "خرابی پورت HDMI PS5",
            "تعمیر باتری دسته PS5",
        ]
        forbidden_claims = ["صد درصد", "قیمت قطعی", "تضمینی", "حتماً درست"]
        self.client.force_authenticate(user=self.admin)

        for topic in topics:
            with self.subTest(topic=topic):
                response = self.client.post(
                    self.endpoint,
                    {
                        **self.payload,
                        "topic": topic,
                        "primary_keyword": topic,
                    },
                    format="json",
                )

                self.assertEqual(response.status_code, status.HTTP_200_OK)
                draft = response.data
                self.assertIn(topic, draft["meta"]["title"])
                self.assertIn(topic, draft["meta"]["seo_title"])
                self.assertLessEqual(len(draft["meta"]["meta_description"]), 320)
                self.assertRegex(draft["meta"]["slug_suggestion"], r"^[a-z0-9_-]+$")
                blocks = draft["blocks"]
                block_types = {block["type"] for block in blocks}
                h2_count = sum(1 for block in blocks if block["type"] == "heading" and block["level"] == 2)
                cta_count = sum(1 for block in blocks if block["type"] == "cta")
                faq_count = sum(len(block["items"]) for block in blocks if block["type"] == "faq")
                word_count = sum(
                    len(str(block.get("text") or block.get("body") or "").split())
                    for block in blocks
                    if block["type"] in {"paragraph", "quote", "callout"}
                )
                self.assertIn("faq", block_types)
                self.assertIn("cta", block_types)
                self.assertGreaterEqual(h2_count, 6)
                self.assertGreaterEqual(cta_count, 2)
                self.assertGreaterEqual(faq_count, 5)
                self.assertGreaterEqual(word_count, 450)
                serialized = str(draft)
                for forbidden in forbidden_claims:
                    self.assertNotIn(forbidden, serialized)
                self.assertNotIn("<script", serialized.lower())
                self.assertNotIn("<iframe", serialized.lower())

    @override_settings(BLOG_AI_PROVIDER="openai_compatible", BLOG_AI_API_KEY="test-key", BLOG_AI_MOCK_ENABLED=False)
    @patch("cheatgame.general.blog_ai.OpenAICompatibleBlogAiProvider.generate")
    def test_blog_ai_draft_invalid_provider_output_is_rejected_without_raw_output(self, mock_generate):
        mock_generate.return_value = {
            "version": "blog_ai_draft_v1",
            "meta": {
                "title": "عنوان تست",
                "seo_title": "عنوان تست",
                "meta_description": "توضیح تست",
                "slug_suggestion": "test-slug",
            },
            "outline": [{"level": 2, "title": "ساختار تست"}],
            "blocks": [{"type": "paragraph", "text": "<script>alert(1)</script>"}],
            "image_prompts": [],
            "internal_link_suggestions": [],
        }
        self.client.force_authenticate(user=self.admin)

        response = self.client.post(self.endpoint, self.payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["error"], "خروجی دستیار هوشمند معتبر نبود و برای امنیت نمایش داده نشد.")
        self.assertNotIn("<script", str(response.data).lower())

    @override_settings(BLOG_AI_PROVIDER="openai_compatible", BLOG_AI_API_KEY="test-key", BLOG_AI_MOCK_ENABLED=False)
    @patch("cheatgame.general.blog_ai.OpenAICompatibleBlogAiProvider.generate")
    def test_blog_ai_draft_unsupported_block_type_is_rejected(self, mock_generate):
        mock_generate.return_value = {
            "version": "blog_ai_draft_v1",
            "meta": {
                "title": "عنوان تست",
                "seo_title": "عنوان تست",
                "meta_description": "توضیح تست",
                "slug_suggestion": "test-slug",
            },
            "outline": [{"level": 2, "title": "ساختار تست"}],
            "blocks": [{"type": "raw_html", "html": "<p>unsafe</p>"}],
            "image_prompts": [],
            "internal_link_suggestions": [],
        }
        self.client.force_authenticate(user=self.admin)

        response = self.client.post(self.endpoint, self.payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["error"], "خروجی دستیار هوشمند معتبر نبود و برای امنیت نمایش داده نشد.")
        self.assertNotIn("raw_html", str(response.data).lower())

    @override_settings(BLOG_AI_PROVIDER="openai_compatible", BLOG_AI_API_KEY="test-key", BLOG_AI_MOCK_ENABLED=False)
    @patch("cheatgame.general.blog_ai.OpenAICompatibleBlogAiProvider.generate")
    def test_blog_ai_draft_too_long_fields_are_rejected(self, mock_generate):
        mock_generate.return_value = {
            "version": "blog_ai_draft_v1",
            "meta": {
                "title": "الف" * 220,
                "seo_title": "عنوان تست",
                "meta_description": "توضیح تست",
                "slug_suggestion": "test-slug",
            },
            "outline": [{"level": 2, "title": "ساختار تست"}],
            "blocks": [{"type": "paragraph", "text": "متن امن"}],
            "image_prompts": [],
            "internal_link_suggestions": [],
        }
        self.client.force_authenticate(user=self.admin)

        response = self.client.post(self.endpoint, self.payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["error"], "خروجی دستیار هوشمند معتبر نبود و برای امنیت نمایش داده نشد.")

    @override_settings(BLOG_AI_PROVIDER="openai_compatible", BLOG_AI_API_KEY="test-key", BLOG_AI_MOCK_ENABLED=False)
    @patch("cheatgame.general.blog_ai.requests.post")
    def test_blog_ai_draft_invalid_json_from_provider_returns_safe_error(self, mock_post):
        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"choices": [{"message": {"content": "not-json"}}]}

        mock_post.return_value = FakeResponse()
        self.client.force_authenticate(user=self.admin)

        response = self.client.post(self.endpoint, self.payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)
        self.assertEqual(response.data["error"], "ارتباط با سرویس هوش مصنوعی برقرار نشد. لطفاً بعداً دوباره تلاش کنید.")
        self.assertNotIn("not-json", str(response.data))

    @override_settings(BLOG_AI_PROVIDER="openai_compatible", BLOG_AI_API_KEY="test-key", BLOG_AI_MOCK_ENABLED=False)
    @patch("cheatgame.general.blog_ai.requests.post", side_effect=requests.Timeout("timeout"))
    def test_blog_ai_draft_provider_timeout_returns_safe_error(self, mock_post):
        self.client.force_authenticate(user=self.admin)

        response = self.client.post(self.endpoint, self.payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)
        self.assertEqual(response.data["error"], "ارتباط با سرویس هوش مصنوعی برقرار نشد. لطفاً بعداً دوباره تلاش کنید.")
