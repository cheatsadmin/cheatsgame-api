from _decimal import Decimal
from typing import List

from django.db import transaction

from cheatgame.product.models import Product, Attachment, ProductType, AttachmentType
from cheatgame.shop.models import Cart, CartItemAttachment, CartItem, OrderItemAttachment, OrderItem
from cheatgame.shop.services.pricing import product_line_payable_total, selected_attachment_unit_total
from cheatgame.users.models import BaseUser


def check_product_limit(*, product: Product, quantity: int) -> bool:
    return product.order_limit >= quantity


def check_product_avaliablity(*, product: Product, quantity: int) -> bool:
    if product.quantity <= 0:
        return False
    return product.quantity - quantity >= 0


def get_cart_or_create(*, user: BaseUser) -> Cart:
    cart = Cart.objects.filter(user=user)
    if cart:
        return cart.first()
    return Cart.objects.create(user=user)


def normalize_attachments(*, attachments: List[Attachment]) -> List[Attachment]:
    normalized_attachments = []
    for item in attachments or []:
        if isinstance(item, dict):
            attachment = item.get("attachment")
        else:
            attachment = item
        if attachment:
            normalized_attachments.append(attachment)
    return normalized_attachments


def validate_product_attachments(*, product: Product, attachments: List[Attachment]) -> tuple[bool, str]:
    selected_attachments = normalize_attachments(attachments=attachments)
    selected_attachment_types = []

    for attachment in selected_attachments:
        if attachment.product_id != product.id:
            return False, "گزینه انتخابی برای این محصول معتبر نیست."
        if attachment.attachment_type in selected_attachment_types:
            return False, "از هر گروه گارانتی، بیمه یا ظرفیت فقط یک گزینه قابل انتخاب است."
        selected_attachment_types.append(attachment.attachment_type)

    if product.product_type == ProductType.GAME and AttachmentType.CAPACITY not in selected_attachment_types:
        return False, "انتخاب ظرفیت بازی الزامی است."

    required_attachments = Attachment.objects.filter(product=product, is_force_attachment=True)
    selected_attachment_ids = {attachment.id for attachment in selected_attachments}
    missing_required_attachments = [
        attachment.title for attachment in required_attachments if attachment.id not in selected_attachment_ids
    ]
    if missing_required_attachments:
        return False, f"انتخاب {'، '.join(missing_required_attachments)} الزامی است."

    return True, ""


def check_attachment(*, attachments: List[Attachment], ) -> bool:
    attachment_type_list = []
    for attachment in normalize_attachments(attachments=attachments):
        if attachment.attachment_type in attachment_type_list:
            return False
        attachment_type_list.append(attachment.attachment_type)
    return True


def check_attachment_order(*, attachments: List[Attachment]) -> bool:
    attachment_type_list = []
    for attachment in attachments:
        if attachment.attachment_type in attachment_type_list:
            return False
        else:
            attachment_type_list.append(attachment.attachment_type)
    return True


def calculate_attchment_price_cart(*, attachments: List[Attachment], product: Product, cart_item: CartItem) -> Decimal:
    selected_attachments = normalize_attachments(attachments=attachments)
    cart_item_attachment = []

    for attachment in selected_attachments:
        cart_item_attachment.append(CartItemAttachment(cart_item=cart_item, attachment=attachment))
    CartItemAttachment.objects.bulk_create(cart_item_attachment)
    return selected_attachment_unit_total(attachments=selected_attachments, product=product)


def calculate_attchment_price_order(*, attachments: List[Attachment], product: Product,
                                    order_item: OrderItem) -> Decimal:
    selected_attachments = normalize_attachments(attachments=attachments)
    order_item_attachment = []

    for attachment in selected_attachments:
        order_item_attachment.append(OrderItemAttachment(order_item=order_item, attachment=attachment))
    OrderItemAttachment.objects.bulk_create(order_item_attachment)
    return selected_attachment_unit_total(attachments=selected_attachments, product=product)


def check_cart_item_exists(*, product: Product, user: BaseUser) -> bool:
    if CartItem.objects.filter(product=product, cart__user=user).exists():
        return True
    return False


@transaction.atomic
def add_to_cart(*, attachment: List[Attachment], quantity: int, product: Product, user: BaseUser) -> CartItem:
    cart = get_cart_or_create(user=user)
    cart_item = CartItem.objects.create(cart=cart, price=0, product=product)
    total_attachment_price = calculate_attchment_price_cart(attachments=attachment, cart_item=cart_item,
                                                            product=product)
    cart_item.quantity = quantity
    cart_item.price = product_line_payable_total(
        product=product,
        attachment_total=total_attachment_price,
        quantity=quantity,
    )
    cart_item.save()
    return cart_item


def cartitem_attachment_total_price(*, cart_item: CartItem) -> Decimal:
    attachments = [
        item.attachment for item in CartItemAttachment.objects.filter(cart_item=cart_item).select_related("attachment")
    ]
    return selected_attachment_unit_total(attachments=attachments, product=cart_item.product)


@transaction.atomic
def update_cart_item(*, cart_item: CartItem, quantity: int = None):
    attachment_price = cartitem_attachment_total_price(cart_item=cart_item)
    cart_item.price = product_line_payable_total(
        product=cart_item.product,
        attachment_total=attachment_price,
        quantity=quantity,
    )
    cart_item.quantity = quantity
    cart_item.save()
    return cart_item


def delete_cart_item(*, cart_item_id: int) -> None:
    CartItem.objects.get(id=cart_item_id).delete()
