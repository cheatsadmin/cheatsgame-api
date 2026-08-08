from pathlib import Path


PRODUCT_DESCRIPTION_ROOT = "product/descriptions"
PRODUCT_MAIN_IMAGE_ROOT = "product/main_images"
PRODUCT_GALLERY_ROOT = "product_images"


def _safe_extension(upload, *, default: str) -> str:
    suffix = Path(getattr(upload, "name", "") or "").suffix.lower()
    if len(suffix) <= 10 and suffix[1:].isalnum():
        return suffix
    return default


def save_product_description(*, product, upload) -> str:
    name = f"{PRODUCT_DESCRIPTION_ROOT}/{product.id}/content.html"
    product.description.save(name, upload, save=False)
    return product.description.name


def save_product_main_image(*, product, upload) -> str:
    extension = _safe_extension(upload, default=".img")
    # Product.main_image already adds the product/main_images/ prefix.
    name = f"{product.id}/main{extension}"
    product.main_image.save(name, upload, save=False)
    return product.main_image.name


def save_product_gallery_image(*, image, upload) -> str:
    extension = _safe_extension(upload, default=".img")
    # Image.file already adds the product_images/ prefix.
    name = f"{image.product_id}/{image.id}{extension}"
    image.file.save(name, upload, save=False)
    return image.file.name


def is_owned_product_description(name: str, *, product_id: int) -> bool:
    return bool(name) and name.startswith(f"{PRODUCT_DESCRIPTION_ROOT}/{product_id}/")


def is_owned_product_main_image(name: str, *, product_id: int) -> bool:
    return bool(name) and name.startswith(f"{PRODUCT_MAIN_IMAGE_ROOT}/{product_id}/")


def is_owned_product_gallery_image(name: str, *, product_id: int, image_id: int) -> bool:
    if not name or not name.startswith(f"{PRODUCT_GALLERY_ROOT}/{product_id}/"):
        return False
    return Path(name).stem == str(image_id)


def delete_file_if_owned(*, storage, name: str, owned: bool) -> None:
    if owned and name:
        storage.delete(name)
