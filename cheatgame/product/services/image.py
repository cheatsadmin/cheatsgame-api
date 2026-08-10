from cheatgame.product.models import Image, Product


def create_image(*, proudct: Product, image) -> Image:
    return Image.objects.create(product=proudct,
                                file=image)


def update_image(*, image_id: int, product: Product, image=None) -> Image:
    file = Image.objects.get(id=image_id)
    if image is not None:
        file.file = image
    file.product = product
    file.save()
    return file


def delete_image(*, image_id: int) -> None:
    Image.objects.get(id=image_id).delete()
