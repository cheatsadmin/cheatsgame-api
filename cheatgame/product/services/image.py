from functools import partial

from django.db import transaction

from cheatgame.product.models import Image, Product
from cheatgame.product.services.media import (
    delete_file_if_owned,
    is_owned_product_gallery_image,
    save_product_gallery_image,
)


@transaction.atomic
def create_image(*, proudct: Product, image) -> Image:
    product_image = Image.objects.create(product=proudct, file="")
    try:
        save_product_gallery_image(image=product_image, upload=image)
        product_image.save(update_fields=["file", "updated_at"])
        return product_image
    except Exception:
        if product_image.file.name:
            product_image.file.storage.delete(product_image.file.name)
        raise


@transaction.atomic
def update_image(*, image_id: int, product: Product, image=None) -> Image:
    file = Image.objects.select_for_update().get(id=image_id)
    old_product_id = file.product_id
    old_name = file.file.name
    if image is not None:
        file.product = product
        save_product_gallery_image(image=file, upload=image)
    file.product = product
    file.save()
    if image is not None and old_name != file.file.name:
        transaction.on_commit(
            partial(
                delete_file_if_owned,
                storage=file.file.storage,
                name=old_name,
                owned=is_owned_product_gallery_image(
                    old_name,
                    product_id=old_product_id,
                    image_id=file.id,
                ),
            )
        )
    return file


@transaction.atomic
def delete_image(*, image_id: int) -> None:
    image = Image.objects.select_for_update().get(id=image_id)
    image_id = image.id
    product_id = image.product_id
    name = image.file.name
    storage = image.file.storage
    image.delete()
    transaction.on_commit(
        partial(
            delete_file_if_owned,
            storage=storage,
            name=name,
            owned=is_owned_product_gallery_image(
                name,
                product_id=product_id,
                image_id=image_id,
            ),
        )
    )
