import hashlib
import re
from pathlib import Path


PRODUCT_DESCRIPTION_ROOT = "product/descriptions"
PRODUCT_MAIN_IMAGE_ROOT = "product/main_images"
PRODUCT_GALLERY_ROOT = "product_images"
CONTENT_HASH_LENGTH = 12


def _safe_extension(upload, *, default: str) -> str:
    suffix = Path(getattr(upload, "name", "") or "").suffix.lower()
    if len(suffix) <= 10 and suffix[1:].isalnum():
        return suffix
    return default


def _content_hash(upload) -> str:
    digest = hashlib.sha256()
    try:
        upload.seek(0)
    except (AttributeError, OSError):
        pass

    if callable(getattr(upload, "chunks", None)):
        chunks = upload.chunks()
    else:
        chunks = iter(lambda: upload.read(64 * 1024), b"")

    for chunk in chunks:
        digest.update(chunk)

    try:
        upload.seek(0)
    except (AttributeError, OSError):
        pass
    return digest.hexdigest()[:CONTENT_HASH_LENGTH]


def _save_and_verify(*, field_file, storage_name: str, save_name: str, upload) -> str:
    if field_file.name == storage_name and field_file.storage.exists(storage_name):
        return storage_name

    field_file.save(save_name, upload, save=False)
    if not field_file.storage.exists(field_file.name):
        raise IOError(f"Uploaded product media is missing from storage: {field_file.name}")
    return field_file.name


def save_product_description(*, product, upload) -> str:
    name = f"{PRODUCT_DESCRIPTION_ROOT}/{product.id}/content.html"
    product.description.save(name, upload, save=False)
    return product.description.name


def save_product_main_image(*, product, upload) -> str:
    extension = _safe_extension(upload, default=".img")
    content_hash = _content_hash(upload)
    # Product.main_image already adds the product/main_images/ prefix.
    relative_name = f"{product.id}/main-{content_hash}{extension}"
    full_name = f"{PRODUCT_MAIN_IMAGE_ROOT}/{relative_name}"
    return _save_and_verify(
        field_file=product.main_image,
        storage_name=full_name,
        save_name=relative_name,
        upload=upload,
    )


def save_product_gallery_image(*, image, upload) -> str:
    extension = _safe_extension(upload, default=".img")
    content_hash = _content_hash(upload)
    # Image.file already adds the product_images/ prefix.
    relative_name = f"{image.product_id}/{image.id}-{content_hash}{extension}"
    full_name = f"{PRODUCT_GALLERY_ROOT}/{relative_name}"
    return _save_and_verify(
        field_file=image.file,
        storage_name=full_name,
        save_name=relative_name,
        upload=upload,
    )


def is_owned_product_description(name: str, *, product_id: int) -> bool:
    return bool(name) and name.startswith(f"{PRODUCT_DESCRIPTION_ROOT}/{product_id}/")


def is_owned_product_main_image(name: str, *, product_id: int) -> bool:
    return bool(name) and name.startswith(f"{PRODUCT_MAIN_IMAGE_ROOT}/{product_id}/")


def is_owned_product_gallery_image(name: str, *, product_id: int, image_id: int) -> bool:
    if not name or not name.startswith(f"{PRODUCT_GALLERY_ROOT}/{product_id}/"):
        return False
    return bool(re.fullmatch(rf"{image_id}(?:-[0-9a-f]{{{CONTENT_HASH_LENGTH}}})?", Path(name).stem))


def delete_file_if_owned(*, storage, name: str, owned: bool) -> None:
    if owned and name:
        storage.delete(name)
