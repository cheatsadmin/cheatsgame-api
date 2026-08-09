import hashlib
import os
import shutil
import tempfile

from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from cheatgame.product.models import Category, CategoryType, Image, Product, ProductStatus, ProductType
from cheatgame.users.models import BaseUser, UserTypes


def content_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()[:12]


class OverwriteFileSystemStorage(FileSystemStorage):
    """Match the active S3 backend's overwrite behavior inside the test suite."""

    def get_available_name(self, name, max_length=None):
        if self.exists(name):
            os.remove(self.path(name))
        return name


class ProductMediaIsolationTests(TestCase):
    def setUp(self):
        self.media_root = tempfile.mkdtemp()
        self.storage = OverwriteFileSystemStorage(location=self.media_root, base_url="/media/")
        self.product_main_field = Product._meta.get_field("main_image")
        self.product_description_field = Product._meta.get_field("description")
        self.gallery_field = Image._meta.get_field("file")
        self.original_storages = (
            self.product_main_field.storage,
            self.product_description_field.storage,
            self.gallery_field.storage,
        )
        self.product_main_field.storage = self.storage
        self.product_description_field.storage = self.storage
        self.gallery_field.storage = self.storage

        self.client = APIClient()
        self.admin = BaseUser.objects.create_user(
            phone_number="09170000123",
            firstname="Media",
            lastname="Admin",
            password="StrongPass123!",
            user_type=UserTypes.ADMIN,
        )
        self.admin.phone_verified = True
        self.admin.save(update_fields=["phone_verified"])
        self.client.force_authenticate(self.admin)
        self.category = Category.objects.create(
            category_type=CategoryType.PRODUCT,
            name="Media isolation",
            slug="media-isolation",
        )

    def tearDown(self):
        (
            self.product_main_field.storage,
            self.product_description_field.storage,
            self.gallery_field.storage,
        ) = self.original_storages
        shutil.rmtree(self.media_root, ignore_errors=True)

    def create_product(self, *, title, slug, image_bytes, description_bytes):
        response = self.client.post(
            "/api/product/product/",
            {
                "product_type": ProductType.PHYSCIAL.value,
                "title": title,
                "slug": slug,
                "status": ProductStatus.PUBLISHED,
                "main_image": SimpleUploadedFile(
                    "1-Photo-1.jpg",
                    image_bytes,
                    content_type="image/jpeg",
                ),
                "price": "100000",
                "off_price": "0",
                "quantity": "5",
                "description": SimpleUploadedFile(
                    "content.html",
                    description_bytes,
                    content_type="text/html",
                ),
                "order_limit": "5",
                "categories": [self.category.id],
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        return Product.objects.get(id=response.data["id"])

    def update_product(self, product, *, description_bytes, title=None, image_bytes=None):
        payload = {
            "product_type": product.product_type,
            "title": title or product.title,
            "slug": product.slug,
            "status": product.status,
            "price": str(product.price),
            "off_price": str(product.off_price),
            "quantity": str(product.quantity),
            "order_limit": str(product.order_limit),
            "description": SimpleUploadedFile(
                "content.html",
                description_bytes,
                content_type="text/html",
            ),
            "categories": [self.category.id],
        }
        if image_bytes is not None:
            payload["main_image"] = SimpleUploadedFile(
                "1-Photo-1.jpg",
                image_bytes,
                content_type="image/jpeg",
            )
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.put(
                f"/api/product/product-deatil/{product.id}/",
                payload,
                format="multipart",
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        product.refresh_from_db()
        return product

    def stored_bytes(self, field_file):
        with field_file.storage.open(field_file.name, "rb") as stored:
            return stored.read()

    def stored_files(self):
        return sorted(
            os.path.relpath(os.path.join(root, filename), self.media_root)
            for root, _, filenames in os.walk(self.media_root)
            for filename in filenames
        )

    def test_product_description_and_identically_named_main_images_are_isolated(self):
        product_x = self.create_product(
            title="Product X",
            slug="media-product-x",
            image_bytes=b"image-x",
            description_bytes=b"description-x",
        )
        product_y = self.create_product(
            title="Product Y",
            slug="media-product-y",
            image_bytes=b"image-y",
            description_bytes=b"description-y",
        )

        self.assertEqual(product_x.description.name, f"product/descriptions/{product_x.id}/content.html")
        self.assertEqual(product_y.description.name, f"product/descriptions/{product_y.id}/content.html")
        self.assertNotEqual(product_x.description.name, product_y.description.name)
        self.assertEqual(
            product_x.main_image.name,
            f"product/main_images/{product_x.id}/main-{content_hash(b'image-x')}.jpg",
        )
        self.assertEqual(
            product_y.main_image.name,
            f"product/main_images/{product_y.id}/main-{content_hash(b'image-y')}.jpg",
        )
        self.assertNotEqual(product_x.main_image.name, product_y.main_image.name)
        self.assertEqual(self.stored_bytes(product_x.main_image), b"image-x")
        self.assertEqual(self.stored_bytes(product_y.main_image), b"image-y")

        self.update_product(product_y, description_bytes=b"description-y-2")
        self.assertEqual(self.stored_bytes(product_x.description), b"description-x")
        self.assertEqual(self.stored_bytes(product_y.description), b"description-y-2")

        self.update_product(product_x, description_bytes=b"description-x-2", title="Product X edited")
        self.assertEqual(product_x.description.name, f"product/descriptions/{product_x.id}/content.html")
        self.assertEqual(self.stored_bytes(product_x.description), b"description-x-2")
        self.assertEqual(self.stored_bytes(product_y.description), b"description-y-2")

    def test_repeated_product_edits_reuse_the_same_owned_description_key(self):
        product = self.create_product(
            title="Repeated edit",
            slug="repeated-edit",
            image_bytes=b"image",
            description_bytes=b"description-0",
        )
        initial_files = self.stored_files()

        for index in range(1, 4):
            self.update_product(product, description_bytes=f"description-{index}".encode())

        self.assertEqual(product.description.name, f"product/descriptions/{product.id}/content.html")
        self.assertEqual(self.stored_bytes(product.description), b"description-3")
        self.assertEqual(self.stored_files(), initial_files)

    def test_main_image_path_changes_only_when_content_changes(self):
        product = self.create_product(
            title="Versioned main image",
            slug="versioned-main-image",
            image_bytes=b"image-a",
            description_bytes=b"description",
        )
        image_a_path = (
            f"product/main_images/{product.id}/main-{content_hash(b'image-a')}.jpg"
        )
        self.assertEqual(product.main_image.name, image_a_path)
        initial_files = self.stored_files()

        self.update_product(
            product,
            image_bytes=b"image-a",
            description_bytes=b"description",
        )
        self.assertEqual(product.main_image.name, image_a_path)
        self.assertEqual(self.stored_files(), initial_files)

        self.update_product(
            product,
            image_bytes=b"image-b",
            description_bytes=b"description",
        )
        image_b_path = (
            f"product/main_images/{product.id}/main-{content_hash(b'image-b')}.jpg"
        )
        self.assertEqual(product.main_image.name, image_b_path)
        self.assertNotEqual(image_a_path, image_b_path)
        self.assertEqual(self.stored_bytes(product.main_image), b"image-b")
        self.assertFalse(self.storage.exists(image_a_path))

    def test_legacy_main_image_remains_readable_and_is_not_deleted_on_update(self):
        product = self.create_product(
            title="Legacy main image",
            slug="legacy-main-image",
            image_bytes=b"initial",
            description_bytes=b"description",
        )
        legacy_path = self.storage.save(
            "product/main_images/legacy-main.jpg",
            ContentFile(b"legacy-main"),
        )
        product.main_image = legacy_path
        product.save(update_fields=["main_image"])

        self.assertEqual(self.stored_bytes(product.main_image), b"legacy-main")
        self.assertEqual(product.main_image.url, "/media/product/main_images/legacy-main.jpg")

        self.update_product(
            product,
            image_bytes=b"replacement",
            description_bytes=b"description",
        )
        self.assertEqual(
            product.main_image.name,
            f"product/main_images/{product.id}/main-{content_hash(b'replacement')}.jpg",
        )
        self.assertTrue(self.storage.exists(legacy_path))

    def test_owned_main_image_is_not_deleted_while_another_product_references_it(self):
        product_x = self.create_product(
            title="Shared reference X",
            slug="shared-reference-x",
            image_bytes=b"shared-main",
            description_bytes=b"description-x",
        )
        product_y = self.create_product(
            title="Shared reference Y",
            slug="shared-reference-y",
            image_bytes=b"other-main",
            description_bytes=b"description-y",
        )
        shared_path = product_x.main_image.name
        product_y.main_image = shared_path
        product_y.save(update_fields=["main_image"])

        self.update_product(
            product_x,
            image_bytes=b"replacement-x",
            description_bytes=b"description-x",
        )

        self.assertTrue(self.storage.exists(shared_path))
        product_y.refresh_from_db()
        self.assertEqual(product_y.main_image.name, shared_path)
        self.assertEqual(self.stored_bytes(product_y.main_image), b"shared-main")

    def test_identically_named_gallery_images_are_isolated_and_stable_on_update(self):
        product_x = self.create_product(
            title="Gallery X",
            slug="gallery-x",
            image_bytes=b"main-x",
            description_bytes=b"description-x",
        )
        product_y = self.create_product(
            title="Gallery Y",
            slug="gallery-y",
            image_bytes=b"main-y",
            description_bytes=b"description-y",
        )

        response_x = self.client.post(
            "/api/product/image/",
            {
                "product": product_x.id,
                "image": SimpleUploadedFile("1-Photo-1.jpg", b"gallery-x", content_type="image/jpeg"),
            },
            format="multipart",
        )
        response_y = self.client.post(
            "/api/product/image/",
            {
                "product": product_y.id,
                "image": SimpleUploadedFile("1-Photo-1.jpg", b"gallery-y", content_type="image/jpeg"),
            },
            format="multipart",
        )
        self.assertEqual(response_x.status_code, status.HTTP_201_CREATED, response_x.data)
        self.assertEqual(response_y.status_code, status.HTTP_201_CREATED, response_y.data)
        image_x = Image.objects.get(id=response_x.data["id"])
        image_y = Image.objects.get(id=response_y.data["id"])

        self.assertEqual(
            image_x.file.name,
            f"product_images/{product_x.id}/{image_x.id}-{content_hash(b'gallery-x')}.jpg",
        )
        self.assertEqual(
            image_y.file.name,
            f"product_images/{product_y.id}/{image_y.id}-{content_hash(b'gallery-y')}.jpg",
        )
        self.assertNotEqual(image_x.file.name, image_y.file.name)
        self.assertEqual(self.stored_bytes(image_x.file), b"gallery-x")
        self.assertEqual(self.stored_bytes(image_y.file), b"gallery-y")

        image_y_initial_path = image_y.file.name
        initial_files = self.stored_files()
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.put(
                f"/api/product/image-detail/{image_y.id}/",
                {
                    "product": product_y.id,
                    "image": SimpleUploadedFile("1-Photo-1.jpg", b"gallery-y", content_type="image/jpeg"),
                },
                format="multipart",
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        image_y.refresh_from_db()
        self.assertEqual(image_y.file.name, image_y_initial_path)
        self.assertEqual(self.stored_files(), initial_files)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.put(
                f"/api/product/image-detail/{image_y.id}/",
                {
                    "product": product_y.id,
                    "image": SimpleUploadedFile("1-Photo-1.jpg", b"gallery-y-2", content_type="image/jpeg"),
                },
                format="multipart",
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        image_y.refresh_from_db()
        self.assertEqual(
            image_y.file.name,
            f"product_images/{product_y.id}/{image_y.id}-{content_hash(b'gallery-y-2')}.jpg",
        )
        self.assertEqual(self.stored_bytes(image_x.file), b"gallery-x")
        self.assertEqual(self.stored_bytes(image_y.file), b"gallery-y-2")
        self.assertFalse(self.storage.exists(image_y_initial_path))

    def test_legacy_gallery_image_remains_readable_and_is_not_deleted_on_update(self):
        product = self.create_product(
            title="Legacy gallery",
            slug="legacy-gallery",
            image_bytes=b"main",
            description_bytes=b"description",
        )
        legacy_path = self.storage.save(
            "product_images/legacy-gallery.jpg",
            ContentFile(b"legacy-gallery"),
        )
        image = Image.objects.create(product=product, file=legacy_path)
        self.assertEqual(self.stored_bytes(image.file), b"legacy-gallery")

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.put(
                f"/api/product/image-detail/{image.id}/",
                {
                    "product": product.id,
                    "image": SimpleUploadedFile(
                        "1-Photo-1.jpg",
                        b"gallery-replacement",
                        content_type="image/jpeg",
                    ),
                },
                format="multipart",
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        image.refresh_from_db()
        self.assertEqual(
            image.file.name,
            f"product_images/{product.id}/{image.id}-{content_hash(b'gallery-replacement')}.jpg",
        )
        self.assertTrue(self.storage.exists(legacy_path))
