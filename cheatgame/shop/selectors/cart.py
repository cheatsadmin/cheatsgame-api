from django.utils import timezone
from typing import List

from django.db.models import QuerySet, Sum
from rest_framework.exceptions import APIException

from cheatgame.product.models import Attachment, ProductType
from cheatgame.shop.models import CartItem, CartItemAttachment, Order, OrderStatus, OrderItem
from cheatgame.users.models import BaseUser


def cart_item_list_user(* , user:BaseUser) ->QuerySet[BaseUser]:
    return CartItem.objects.filter(cart__user = user)

def cart_item_attachment_list(* , cart_item:CartItem) -> List[Attachment]:
    cart_item_attachments = CartItemAttachment.objects.filter(cart_item = cart_item).prefetch_related("attachment")
    attachments = [cart_item_attachment.attachment for cart_item_attachment in cart_item_attachments]
    return attachments


def order_list_user(*, user: BaseUser, is_game=False) -> QuerySet[Order]:
    if is_game:
        return Order.objects.filter(user=user,
                                    order_items__product__product_type__in=[ProductType.GAME,
                                                                            ProductType.PACKAGE]).distinct().prefetch_related(
            "schedule__type",
            "schedule__schedule",
            "schedule__address")
    return Order.objects.filter(user=user,
                                order_items__product__product_type__in=[ProductType.PHYSCIAL,
                                                                        ProductType.GIFTCART]).distinct().prefetch_related(
        "schedule__type",
        "schedule__schedule",
        "schedule__address")

def check_order_exists(* , order_id:int) -> bool:
    return Order.objects.filter(id = order_id).exists()
def get_order(* , order_id:int)-> Order:
    return Order.objects.get(id = order_id)

def bought_order_item(* , user:BaseUser , product_id : int) -> bool:
    return OrderItem.objects.filter(order__payment_status = OrderStatus.PAID.value , order__user = user, product_id = product_id).exists()


def sell_report(filters = None) -> dict:
    if filters is None:
        game_amount = Order.objects.filter(payment_status = OrderStatus.PAID ,is_game=True,).aggregate(Sum("total_price_discount")).get("total_price_discount__sum", 0)
        game_number = Order.objects.filter(payment_status = OrderStatus.PAID , is_game=True).count()
        physical_amount = Order.objects.filter(order_items__product__product_type__in=[ProductType.PHYSCIAL],
                             payment_status=OrderStatus.PAID).aggregate(Sum("total_price_discount")).get("total_price_discount__sum")
        physical_number = Order.objects.filter(order_items__product__product_type__in=[ProductType.PHYSCIAL],
                             payment_status=OrderStatus.PAID).count()
        giftcart_amount = Order.objects.filter(order_items__product__product_type__in=[ProductType.GIFTCART],
                             payment_status=OrderStatus.PAID).aggregate(Sum("total_price_discount")).get("total_price_discount__sum")
        giftcart_number = Order.objects.filter(order_items__product__product_type__in=[ProductType.GIFTCART],
                             payment_status=OrderStatus.PAID).count()
        informaton = {
            "game_amount": game_amount,
            "game_number": game_number,
            "physical_amount": physical_amount,
            "physical_number": physical_number,
            "giftcart_amount": giftcart_amount,
            "giftcart_number": giftcart_number,
        }
        return informaton
    else:
        limit = 2
        created_at__in = filters.split(",")
        if len(created_at__in) > limit:
            raise APIException("please just add two created_at with , in the middle")
        created_at_0, created_at_1 = created_at__in

        if not created_at_1:
            created_at_1 = timezone.now()


        if not created_at_0:
            queryset = Order.objects.filter(created_at__date__lt=created_at_1)
            game_amount = queryset.filter(payment_status=OrderStatus.PAID, is_game=True, ).aggregate(
                Sum("total_price_discount")).get("total_price_discount", 0)
            game_number = queryset.filter(payment_status=OrderStatus.PAID, is_game=True).count()
            physical_amount = Order.objects.filter(order_items__product__product_type__in=[ProductType.PHYSCIAL],
                                                   payment_status=OrderStatus.PAID).aggregate(
                Sum("total_price_discount")).get("total_price_discount__sum")
            physical_number = Order.objects.filter(order_items__product__product_type__in=[ProductType.PHYSCIAL],
                                                   payment_status=OrderStatus.PAID).count()
            giftcart_amount = Order.objects.filter(order_items__product__product_type__in=[ProductType.GIFTCART],
                                                   payment_status=OrderStatus.PAID).aggregate(
                Sum("total_price_discount")).get("total_price_discount__sum")
            giftcart_number = Order.objects.filter(order_items__product__product_type__in=[ProductType.GIFTCART],
                                                   payment_status=OrderStatus.PAID).count()

            informaton = {
                "game_amount": game_amount,
                "game_number": game_number,
                "physical_amount": physical_amount,
                "physical_number": physical_number,
                "giftcart_amount": giftcart_amount,
                "giftcart_number": giftcart_number,
            }
            return informaton

        queryset = Order.objects.filter(created_at__range=(created_at_0, created_at_1))
        game_amount = queryset.filter(payment_status=OrderStatus.PAID, is_game=True, ).aggregate(
            Sum("total_price_discount")).get("total_price_discount", 0)
        game_number = queryset.filter(payment_status=OrderStatus.PAID, is_game=True).count()
        physical_amount = Order.objects.filter(order_items__product__product_type__in=[ProductType.PHYSCIAL],
                                               payment_status=OrderStatus.PAID).aggregate(
            Sum("total_price_discount")).get("total_price_discount__sum")
        physical_number = Order.objects.filter(order_items__product__product_type__in=[ProductType.PHYSCIAL],
                                               payment_status=OrderStatus.PAID).count()
        giftcart_amount = Order.objects.filter(order_items__product__product_type__in=[ProductType.GIFTCART],
                                               payment_status=OrderStatus.PAID).aggregate(
            Sum("total_price_discount")).get("total_price_discount__sum")
        giftcart_number = Order.objects.filter(order_items__product__product_type__in=[ProductType.GIFTCART],
                                               payment_status=OrderStatus.PAID).count()

        informaton = {
            "game_amount": game_amount,
            "game_number": game_number,
            "physical_amount": physical_amount,
            "physical_number": physical_number,
            "giftcart_amount": giftcart_amount,
            "giftcart_number": giftcart_number,
        }
        return informaton
