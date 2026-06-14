import decimal

from cheatgame.product.models import Product, Attachment


def create_attchement(*, attachment_type: int, title: str, price: decimal, is_force_attachment: bool,
                      product: Product , description:str) -> Attachment:
    return Attachment.objects.create(
        attachment_type=attachment_type,
        title=title,
        price=price,
        is_force_attachment=is_force_attachment,
        product=product,
        description=description
    )


def update_attachment(*, attachment_type: int, title: str, price: decimal, is_force_attachment: bool,
                      product: Product, attachment_id: int , description: str) -> Attachment:
    attachment = Attachment.objects.get(id=attachment_id)
    attachment.attachment_type = attachment_type
    attachment.title = title
    attachment.price = price
    attachment.is_force_attachment = is_force_attachment
    attachment.product = product
    if description is not None:
        attachment.description = description
    attachment.save()
    return attachment


def delete_attachment(*, attachment_id: int) -> None:
    Attachment.objects.get(id=attachment_id).delete()
