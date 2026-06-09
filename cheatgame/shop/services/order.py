from _decimal import Decimal
from typing import List

from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone

from cheatgame.product.models import ProductType
from cheatgame.shop.models import Order, CartItem, OrderItem, DeliveryData, Discount, DiscountValueType
from cheatgame.shop.selectors.cart import cart_item_attachment_list
from cheatgame.shop.services.cart import calculate_attchment_price_order
from cheatgame.users.models import BaseUser


def create_order(*, user: BaseUser, total_price: int) -> Order:
    return Order.objects.create(
        user=user,
        total_price=total_price,
        total_price_discount=total_price
    )


def create_order_item(*, cart_item: CartItem, order: Order) -> OrderItem:
    return OrderItem.objects.create(
        product=cart_item.product,
        price=Decimal("0"),
        order=order
    )


def remove_user_cart_items(*, user: BaseUser) -> None:
    CartItem.objects.filter(cart__user=user).delete()


@transaction.atomic
def submit_order(*, user: BaseUser, total_price: int, product: List[CartItem], game: List[CartItem],
                 cart_items: QuerySet[CartItem]):
    order_price_product = Decimal("0")
    order_price_game = Decimal("0")
    order_list = []
    if product and game:
        game_order = create_order(user=user, total_price=total_price)
        product_order = create_order(user=user, total_price=total_price)
        for game_cart_item in game:
            quantity = game_cart_item.quantity
            order_item = create_order_item(cart_item=game_cart_item, order=game_order)
            attachements = cart_item_attachment_list(cart_item=game_cart_item)
            total_attachment_price = calculate_attchment_price_order(
                product=game_cart_item.product,
                attachments=attachements,
                order_item=order_item
            )
            final_price = total_attachment_price * quantity
            order_price_game += final_price
            order_item.price = final_price
            order_item.quantity = quantity
            order_item.save()
        game_order.total_price = order_price_game
        game_order.total_price_discount = order_price_game
        game_order.is_game = True
        game_order.save()
        order_list.append(game_order)
        for product_cart_item in product:
            quantity = product_cart_item.quantity
            price = product_cart_item.product.price if not product_cart_item.product.discount_end_time or product_cart_item.product.discount_end_time < timezone.now() else product_cart_item.product.off_price
            order_item = create_order_item(cart_item=product_cart_item, order=product_order)
            attachements = cart_item_attachment_list(cart_item=product_cart_item)
            total_attachment_price = calculate_attchment_price_order(
                product=product_cart_item.product,
                attachments=attachements,
                order_item=order_item
            )
            final_price = price * quantity + total_attachment_price
            order_price_product += final_price
            order_item.price = final_price
            order_item.quantity = quantity
            order_item.save()
        product_order.total_price = order_price_product
        product_order.total_price_discount = order_price_product
        product_order.is_game = False
        product_order.save()
        order_list.append(product_order)
        remove_user_cart_items(user=user)
        return order_list
    else:
        order = create_order(user=user, total_price=total_price)
        is_game = bool
        for cart_item in cart_items:
            quantity = cart_item.quantity
            order_item = create_order_item(cart_item=cart_item, order=order)
            attachements = cart_item_attachment_list(cart_item=cart_item)
            total_attachment_price = calculate_attchment_price_order(
                product=cart_item.product,
                attachments=attachements,
                order_item=order_item
            )
            if cart_item.product.product_type in [ProductType.GAME , ProductType.PACKAGE]:
                is_game = True
            else:
                is_game = False
            if cart_item.product.product_type == ProductType.GAME:
                final_price = total_attachment_price * quantity
                order_price_product += final_price
            else:
                price = cart_item.product.price if not cart_item.product.discount_end_time or cart_item.product.discount_end_time < timezone.now() else cart_item.product.off_price
                final_price = price * quantity + total_attachment_price
                order_price_product += final_price
            order_item.price = final_price
            order_item.quantity = quantity
            order_item.save()
        order.total_price = order_price_product
        order.total_price_discount = order_price_product
        order.is_game = is_game
        order.save()
        remove_user_cart_items(user=user)
        order_list.append(order)
        return order_list


def update_order(*, order_id: int, schedule: DeliveryData, discount: Discount = None) -> Order:
    order = Order.objects.get(id=order_id)
    old_schedule = order.schedule
    update_fields = []
    if schedule is not None:
        order.schedule = schedule
        update_fields.append("schedule")
    if discount is not None:
        order.discount = discount
        update_fields.append("discount")
        if discount.value_type == DiscountValueType.AMOUNT.value:
            order.total_price_discount = order.total_price - discount.amount

        elif discount.value_type == DiscountValueType.PERCENT.value:
            order.total_price_discount = order.total_price * (1 - discount.percent)
        update_fields.append("total_price_discount")
    if update_fields:
        order.save(update_fields=[*update_fields, "updated_at"])
    if schedule is not None:
        DeliveryData.objects.filter(id=schedule.id).update(is_used=True)
    if old_schedule is not None and old_schedule != schedule and not Order.objects.filter(schedule=old_schedule).exists():
        DeliveryData.objects.filter(id=old_schedule.id).update(is_used=False)
    return order
