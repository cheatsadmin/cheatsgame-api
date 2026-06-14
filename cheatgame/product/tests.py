import shutil
import tempfile

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework.throttling import ScopedRateThrottle

from cheatgame.product.models import (
    Category,
    CategoryType,
    Attachment,
    AttachmentType,
    Product,
    ProductCategory,
    ProductStatus,
    ProductType,
    Reviews,
    ReviewStatus,
)
from cheatgame.product.selectors.product import product_list
from cheatgame.shop.models import Order, OrderItem, OrderStatus
from cheatgame.users.models import BaseUser, UserTypes


class ProductReviewApiTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.user = BaseUser.objects.create_user(
            phone_number="09174441001",
            firstname="Review",
            lastname="Customer",
            password="StrongPass123!",
        )
        self.user.phone_verified = True
        self.user.save(update_fields=["phone_verified"])

        self.admin = BaseUser.objects.create_user(
            phone_number="09174441002",
            firstname="Review",
            lastname="Admin",
            password="StrongPass123!",
            user_type=UserTypes.ADMIN,
        )
        self.admin.phone_verified = True
        self.admin.save(update_fields=["phone_verified"])

        self.product = Product.objects.create(
            product_type=ProductType.PHYSCIAL,
            title="Review Test Product",
            main_image="product/main_images/review-test.jpg",
            price=100000,
            off_price=90000,
            quantity=5,
            description="product/descriptions/review-test.html",
        )
        self.order = Order.objects.create(
            user=self.user,
            payment_status=OrderStatus.PAID,
            total_price=90000,
            total_price_discount=90000,
        )
        OrderItem.objects.create(
            order=self.order,
            product=self.product,
            quantity=1,
            price=90000,
        )

    def tearDown(self):
        if hasattr(self, "original_throttle_rates"):
            ScopedRateThrottle.THROTTLE_RATES = self.original_throttle_rates
        cache.clear()

    def set_throttle_rate(self, scope, rate):
        self.original_throttle_rates = ScopedRateThrottle.THROTTLE_RATES.copy()
        ScopedRateThrottle.THROTTLE_RATES = self.original_throttle_rates.copy()
        ScopedRateThrottle.THROTTLE_RATES[scope] = rate

    def submit_review(self, *, rating=5, comment="Great product"):
        return self.client.post(
            "/api/product/product-review/",
            {
                "product": self.product.id,
                "rating": rating,
                "comment": comment,
            },
            format="json",
        )

    def test_authenticated_purchaser_can_create_pending_review(self):
        self.client.force_authenticate(self.user)

        response = self.submit_review(rating=4, comment="Works well")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        review = Reviews.objects.get(user=self.user, product=self.product)
        self.assertEqual(review.rating, 4)
        self.assertEqual(review.comment, "Works well")
        self.assertEqual(review.status, ReviewStatus.PENDING)
        self.assertFalse(review.accepted)

    def test_review_submission_requires_authenticated_customer(self):
        response = self.submit_review()

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(Reviews.objects.exists())

    def test_authenticated_customer_can_review_without_paid_purchase(self):
        product_without_purchase = Product.objects.create(
            product_type=ProductType.PHYSCIAL,
            title="Review Test Product Without Purchase",
            main_image="product/main_images/review-test-no-purchase.jpg",
            price=100000,
            off_price=90000,
            quantity=5,
            description="product/descriptions/review-test-no-purchase.html",
        )
        self.client.force_authenticate(self.user)

        response = self.client.post(
            "/api/product/product-review/",
            {
                "product": product_without_purchase.id,
                "rating": 5,
                "comment": "No purchase required",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(Reviews.objects.filter(user=self.user, product=product_without_purchase).exists())

    def test_second_submission_updates_existing_review_without_duplicate(self):
        self.client.force_authenticate(self.user)
        first_response = self.submit_review(rating=3, comment="First")
        review_id = first_response.data["id"]

        second_response = self.submit_review(rating=5, comment="Updated")

        self.assertEqual(second_response.status_code, status.HTTP_200_OK)
        self.assertEqual(second_response.data["id"], review_id)
        self.assertEqual(Reviews.objects.filter(user=self.user, product=self.product).count(), 1)
        review = Reviews.objects.get(id=review_id)
        self.assertEqual(review.rating, 5)
        self.assertEqual(review.comment, "Updated")
        self.assertEqual(review.status, ReviewStatus.PENDING)
        self.assertFalse(review.accepted)

    def test_review_update_returns_to_pending_after_previous_approval(self):
        self.client.force_authenticate(self.user)
        self.submit_review(rating=5, comment="Approved version")
        review = Reviews.objects.get(user=self.user, product=self.product)
        review.status = ReviewStatus.APPROVED
        review.accepted = True
        review.save(update_fields=["status", "accepted"])

        response = self.submit_review(rating=2, comment="Needs another moderation pass")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        review.refresh_from_db()
        self.assertEqual(review.status, ReviewStatus.PENDING)
        self.assertFalse(review.accepted)
        self.assertEqual(review.rating, 2)

    def test_moderation_controls_public_visibility(self):
        self.client.force_authenticate(self.user)
        self.submit_review(rating=5, comment="Visible after approval")
        review = Reviews.objects.get(user=self.user, product=self.product)

        public_response = self.client.get(f"/api/product/product-detail/{self.product.slug}/")
        self.assertEqual(public_response.status_code, status.HTTP_200_OK)
        self.assertEqual(public_response.data["reviews"], [])
        self.assertEqual(public_response.data["comments_count"], 0)

        self.client.force_authenticate(self.admin)
        approve_response = self.client.put(
            f"/api/product/review-detail-admin/{review.id}/",
            {"status": ReviewStatus.APPROVED},
            format="json",
        )
        self.assertEqual(approve_response.status_code, status.HTTP_200_OK)
        self.assertTrue(approve_response.data["accepted"])

        admin_list_response = self.client.get("/api/product/review-list-admin/")
        self.assertEqual(admin_list_response.status_code, status.HTTP_200_OK)
        self.assertEqual(admin_list_response.data["count"], 1)

        status_filter_response = self.client.get("/api/product/review-list-admin/?status=approved")
        self.assertEqual(status_filter_response.status_code, status.HTTP_200_OK)
        self.assertEqual(status_filter_response.data["count"], 1)

        accepted_filter_response = self.client.get("/api/product/review-list-admin/?is_accepted=true")
        self.assertEqual(accepted_filter_response.status_code, status.HTTP_200_OK)
        self.assertEqual(accepted_filter_response.data["count"], 1)

        public_response = self.client.get(f"/api/product/product-detail/{self.product.slug}/")
        self.assertEqual(public_response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(public_response.data["reviews"]), 1)
        self.assertEqual(public_response.data["reviews"][0]["comment"], "Visible after approval")
        self.assertEqual(public_response.data["reviews"][0]["rating"], 5)
        self.assertEqual(public_response.data["comments_count"], 1)

        reject_response = self.client.put(
            f"/api/product/review-detail-admin/{review.id}/",
            {"status": ReviewStatus.REJECTED},
            format="json",
        )
        self.assertEqual(reject_response.status_code, status.HTTP_200_OK)
        self.assertFalse(reject_response.data["accepted"])

        public_response = self.client.get(f"/api/product/product-detail/{self.product.slug}/")
        self.assertEqual(public_response.status_code, status.HTTP_200_OK)
        self.assertEqual(public_response.data["reviews"], [])
        self.assertEqual(public_response.data["comments_count"], 0)

    def test_review_submit_endpoint_is_throttled(self):
        self.set_throttle_rate("review_submit", "1/min")
        self.client.force_authenticate(self.user)

        first_response = self.submit_review(rating=4, comment="First")
        second_response = self.submit_review(rating=5, comment="Second")

        self.assertEqual(first_response.status_code, status.HTTP_200_OK)
        self.assertEqual(second_response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)


class ProductReviewThrottleConfigurationTests(TestCase):
    def test_review_submit_throttle_rate_is_configured(self):
        throttle_rates = settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]

        self.assertIn("review_submit", throttle_rates)


class ProductFoundationApiTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls._media_root = tempfile.mkdtemp()
        cls._override_settings = override_settings(MEDIA_ROOT=cls._media_root)
        cls._override_settings.enable()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls._override_settings.disable()
        shutil.rmtree(cls._media_root, ignore_errors=True)

    def setUp(self):
        self.client = APIClient()
        self.admin = BaseUser.objects.create_user(
            phone_number="09175551001",
            firstname="Product",
            lastname="Admin",
            password="StrongPass123!",
            user_type=UserTypes.ADMIN,
        )
        self.admin.phone_verified = True
        self.admin.save(update_fields=["phone_verified"])
        self.category = Category.objects.create(
            category_type=CategoryType.PRODUCT,
            name="Controllers",
        )
        self.product = Product.objects.create(
            product_type=ProductType.PHYSCIAL,
            title="Foundation Product",
            main_image="product/main_images/foundation.jpg",
            price=100000,
            off_price=90000,
            quantity=5,
            description="product/descriptions/foundation.html",
            order_limit=5,
            status=ProductStatus.PUBLISHED,
            meta_description="Foundation meta",
        )
        ProductCategory.objects.create(product=self.product, category=self.category)
        self.warranty = Attachment.objects.create(
            attachment_type=AttachmentType.GUARANTEE,
            title="Warranty title",
            description="Warranty detail",
            price=0,
            is_force_attachment=True,
            product=self.product,
        )

    def product_create_payload(self, **overrides):
        payload = {
            "product_type": ProductType.PHYSCIAL.value,
            "title": "Created Foundation Product",
            "slug": "created-foundation-product",
            "status": ProductStatus.PUBLISHED,
            "seo_title": "",
            "meta_description": "Created meta",
            "main_image": SimpleUploadedFile("main.jpg", b"image", content_type="image/jpeg"),
            "price": "120000",
            "off_price": "110000",
            "quantity": "4",
            "description": SimpleUploadedFile("description.html", b"<p>desc</p>", content_type="text/html"),
            "order_limit": "2",
        }
        payload.update(overrides)
        return payload

    def test_product_detail_exposes_foundation_fields_for_editor(self):
        response = self.client.get(f"/api/product/product-detail/{self.product.slug}/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["slug"], self.product.slug)
        self.assertEqual(response.data["status"], ProductStatus.PUBLISHED)
        self.assertEqual(response.data["seo_title"], self.product.title)
        self.assertEqual(response.data["meta_description"], "Foundation meta")
        self.assertEqual(response.data["order_limit"], 5)
        self.assertEqual(response.data["categories"][0]["id"], self.category.id)

    def test_attachment_mapping_exposes_title_description_and_price(self):
        detail_response = self.client.get(f"/api/product/product-detail/{self.product.slug}/")

        self.assertEqual(detail_response.status_code, status.HTTP_200_OK)
        attachment = detail_response.data["attachments"][0]
        self.assertEqual(attachment["title"], "Warranty title")
        self.assertEqual(attachment["description"], "Warranty detail")
        self.assertEqual(str(attachment["price"]), "0")

        self.client.force_authenticate(self.admin)
        list_response = self.client.get(f"/api/product/attachment-list/{self.product.id}/")

        self.assertEqual(list_response.status_code, status.HTTP_200_OK)
        self.assertEqual(list_response.data[0]["title"], "Warranty title")
        self.assertEqual(list_response.data[0]["description"], "Warranty detail")
        self.assertEqual(str(list_response.data[0]["price"]), "0")

    def test_public_visibility_hides_unpublished_products_but_admin_can_open_them(self):
        hidden_product = Product.objects.create(
            product_type=ProductType.PHYSCIAL,
            title="Hidden Foundation Product",
            main_image="product/main_images/hidden.jpg",
            price=100000,
            off_price=90000,
            quantity=5,
            description="product/descriptions/hidden.html",
            status=ProductStatus.HIDDEN,
        )

        public_detail = self.client.get(f"/api/product/product-detail/{hidden_product.slug}/")
        self.assertEqual(public_detail.status_code, status.HTTP_404_NOT_FOUND)

        public_slugs = list(product_list().values_list("slug", flat=True))
        self.assertIn(self.product.slug, public_slugs)
        self.assertNotIn(hidden_product.slug, public_slugs)

        self.client.force_authenticate(self.admin)
        admin_detail = self.client.get(f"/api/product/product-detail/{hidden_product.slug}/")
        self.assertEqual(admin_detail.status_code, status.HTTP_200_OK)
        self.assertEqual(admin_detail.data["status"], ProductStatus.HIDDEN)

    def test_admin_create_requires_category_unless_explicitly_uncategorized(self):
        self.client.force_authenticate(self.admin)

        missing_category = self.client.post(
            "/api/product/product/",
            self.product_create_payload(slug="missing-category-product"),
            format="multipart",
        )
        self.assertEqual(missing_category.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("categories", missing_category.data["detail"])

        explicit_uncategorized = self.client.post(
            "/api/product/product/",
            self.product_create_payload(
                slug="explicit-uncategorized-product",
                allow_uncategorized="true",
            ),
            format="multipart",
        )
        self.assertEqual(explicit_uncategorized.status_code, status.HTTP_201_CREATED)
        self.assertEqual(explicit_uncategorized.data["categories"], [])

    def test_admin_create_assigns_categories_and_rejects_duplicate_slug(self):
        self.client.force_authenticate(self.admin)

        created = self.client.post(
            "/api/product/product/",
            self.product_create_payload(categories=[self.category.id]),
            format="multipart",
        )
        self.assertEqual(created.status_code, status.HTTP_201_CREATED)
        self.assertEqual(created.data["slug"], "created-foundation-product")
        self.assertEqual(created.data["categories"][0]["id"], self.category.id)

        duplicate_slug = self.client.post(
            "/api/product/product/",
            self.product_create_payload(
                title="Duplicate Foundation Product",
                categories=[self.category.id],
            ),
            format="multipart",
        )
        self.assertEqual(duplicate_slug.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("slug", duplicate_slug.data["detail"])
