from _decimal import Decimal
from typing import List

from django.db import transaction
from django.db.models import F, QuerySet, Sum

from cheatgame.product.models import Product, ProductType
from cheatgame.shop.models import (
    Order,
    CartItem,
    OrderItem,
    DeliveryData,
    DeliverySchedule,
    DeliverySide,
    DeliveryType,
    Discount,
    DiscountType,
    DiscountValueType,
    OrderStatus,
    UserDiscount,
)
from cheatgame.shop.selectors.cart import cart_item_attachment_list
from cheatgame.shop.selectors.discount import calculate_discounted_total, validate_discount_code
from cheatgame.shop.services.cart import calculate_attchment_price_order, lock_and_assert_user_cart_mutable
from cheatgame.shop.services.delivery_schedule import (
    DeliveryDataAlreadyUsedError,
    DeliverySlotFullError,
    is_delivery_schedule_full,
)
from cheatgame.shop.services.pricing import product_line_original_total, product_line_payable_total
from cheatgame.users.models import BaseUser


class StockUnavailableError(Exception):
    pass


class DiscountUnavailableError(Exception):
    pass


class DeliverySlotUnavailableError(Exception):
    pass


class ShippingUnavailableError(Exception):
    pass


def _requirements_from_cart_items(cart_items: List[CartItem]) -> dict:
    requirements = {}
    for cart_item in cart_items:
        requirements[cart_item.product_id] = requirements.get(cart_item.product_id, 0) + cart_item.quantity
    return requirements


def _requirements_from_order(order: Order) -> dict:
    return {
        item["product_id"]: item["quantity"]
        for item in order.order_items.values("product_id").annotate(quantity=Sum("quantity"))
    }


def order_item_payable_total(*, order: Order) -> Decimal:
    total = order.order_items.aggregate(total=Sum("price")).get("total")
    return total or Decimal("0")


def validate_stock_requirements(*, requirements: dict, lock: bool = False) -> None:
    if not requirements:
        return
    product_ids = list(requirements.keys())
    products = Product.objects.filter(id__in=product_ids)
    if lock:
        products = products.select_for_update()
    product_map = {product.id: product for product in products}
    for product_id, requested_quantity in requirements.items():
        product = product_map.get(product_id)
        if product is None:
            raise StockUnavailableError("یکی از محصولات سبد خرید دیگر موجود نیست.")
        if product.order_limit is not None and requested_quantity > product.order_limit:
            raise StockUnavailableError(f"تعداد انتخاب شده برای {product.title} بیش از حد مجاز است.")
        available_quantity = product.quantity
        if available_quantity < requested_quantity:
            available_quantity = max(available_quantity, 0)
            raise StockUnavailableError(
                f"موجودی {product.title} کافی نیست. موجودی قابل خرید: {available_quantity}"
            )


def validate_cart_stock(*, cart_items: List[CartItem], lock: bool = False) -> None:
    validate_stock_requirements(requirements=_requirements_from_cart_items(cart_items), lock=lock)


def ensure_order_stock_available(*, order: Order, lock: bool = False) -> None:
    validate_stock_requirements(
        requirements=_requirements_from_order(order),
        lock=lock,
    )


def commit_order_stock(*, order: Order) -> None:
    requirements = _requirements_from_order(order)
    validate_stock_requirements(requirements=requirements, lock=True)
    for product_id, quantity in requirements.items():
        Product.objects.filter(id=product_id).update(quantity=F("quantity") - quantity)


def _delivery_data_used_by_other_paid_order(*, order: Order, delivery_data: DeliveryData) -> bool:
    return Order.objects.filter(
        schedule=delivery_data,
        payment_status=OrderStatus.PAID.value,
    ).exclude(id=order.id).exists()


def ensure_order_delivery_slot_available(*, order: Order, lock: bool = False) -> DeliveryData:
    if order.schedule_id is None:
        return None

    delivery_data_ref = DeliveryData.objects.only("id", "schedule_id").get(id=order.schedule_id)
    schedule_queryset = DeliverySchedule.objects
    delivery_data_queryset = DeliveryData.objects.select_related("schedule", "address", "type")
    if lock:
        schedule_queryset = schedule_queryset.select_for_update()
        # Nullable select_related joins cannot be locked by PostgreSQL. The
        # DeliveryData row is the lock owner; its schedule is locked above.
        delivery_data_queryset = delivery_data_queryset.select_for_update(of=("self",))

    schedule = schedule_queryset.get(id=delivery_data_ref.schedule_id)
    delivery_data = delivery_data_queryset.get(id=delivery_data_ref.id)

    if _delivery_data_used_by_other_paid_order(order=order, delivery_data=delivery_data):
        raise DeliverySlotUnavailableError("این زمان ارسال قبلا برای سفارش دیگری نهایی شده است.")

    if not delivery_data.is_used and is_delivery_schedule_full(schedule=schedule):
        raise DeliverySlotUnavailableError("ظرفیت زمان ارسال انتخابی تکمیل شده است.")

    return delivery_data


def ensure_order_shipping_ready(*, order: Order, lock: bool = False) -> None:
    if order.is_game:
        return

    if order.schedule_id is not None:
        raise ShippingUnavailableError("برای سفارش محصول زمان ارسال انتخاب نمی‌شود. فقط روش ارسال را انتخاب کنید.")

    if order.shipping_address_id is None:
        raise ShippingUnavailableError("آدرس ارسال سفارش را انتخاب کنید.")

    if order.shipping_method_id is None:
        raise ShippingUnavailableError("روش ارسال سفارش را انتخاب کنید.")

    shipping_method_queryset = DeliveryType.objects
    if lock:
        shipping_method_queryset = shipping_method_queryset.select_for_update()
    shipping_method = shipping_method_queryset.get(id=order.shipping_method_id)
    if shipping_method.side != DeliverySide.SENDTOUSER.value:
        raise ShippingUnavailableError("روش ارسال انتخاب شده برای سفارش محصول معتبر نیست.")

    if order.shipping_address.user_id != order.user_id:
        raise ShippingUnavailableError("آدرس ارسال باید برای خود کاربر باشد.")


def commit_order_delivery_slot(*, order: Order) -> None:
    delivery_data = ensure_order_delivery_slot_available(order=order, lock=True)
    if delivery_data is None or delivery_data.is_used:
        return
    delivery_data.is_used = True
    delivery_data.save(update_fields=["is_used", "updated_at"])


def ensure_order_discount_available(*, order: Order, lock: bool = False) -> None:
    if order.discount_id is None:
        return
    discount = order.discount
    if lock:
        discount = Discount.objects.select_for_update().get(id=order.discount_id)
    result = validate_discount_code(code=discount.code, total_price=order_item_payable_total(order=order), user=order.user)
    if not result.is_valid:
        raise DiscountUnavailableError(result.message)


def commit_order_discount_usage(*, order: Order) -> None:
    if order.discount_id is None:
        return
    discount = Discount.objects.select_for_update().get(id=order.discount_id)
    if discount.type == DiscountType.COUPON.value:
        if discount.usage_number <= 0:
            raise DiscountUnavailableError("ظرفیت استفاده از این کد تخفیف تمام شده است.")
        discount.usage_number = F("usage_number") - 1
        discount.save(update_fields=["usage_number", "updated_at"])
        return
    if discount.type == DiscountType.DIRECT.value:
        user_discount = UserDiscount.objects.select_for_update().filter(discount=discount, user=order.user).first()
        if user_discount is None:
            raise DiscountUnavailableError("این کد تخفیف برای شما فعال نیست.")
        if user_discount.is_used:
            raise DiscountUnavailableError("این کد تخفیف قبلا استفاده شده است.")
        user_discount.is_used = True
        user_discount.save(update_fields=["is_used", "updated_at"])


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
    lock_and_assert_user_cart_mutable(user=user)
    CartItem.objects.filter(cart__user=user).delete()


@transaction.atomic
def submit_order(*, user: BaseUser, total_price: int, product: List[CartItem], game: List[CartItem],
                 cart_items: QuerySet[CartItem]):
    lock_and_assert_user_cart_mutable(user=user)
    cart_items = list(cart_items)
    product = list(product)
    game = list(game)
    validate_cart_stock(cart_items=cart_items, lock=True)
    order_original_price_product = Decimal("0")
    order_payable_price_product = Decimal("0")
    order_original_price_game = Decimal("0")
    order_payable_price_game = Decimal("0")
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
            original_price = product_line_original_total(
                product=game_cart_item.product,
                attachment_total=total_attachment_price,
                quantity=quantity,
            )
            payable_price = product_line_payable_total(
                product=game_cart_item.product,
                attachment_total=total_attachment_price,
                quantity=quantity,
            )
            order_original_price_game += original_price
            order_payable_price_game += payable_price
            order_item.price = payable_price
            order_item.quantity = quantity
            order_item.save()
        game_order.total_price = order_original_price_game
        game_order.total_price_discount = order_payable_price_game
        game_order.is_game = True
        game_order.save()
        order_list.append(game_order)
        for product_cart_item in product:
            quantity = product_cart_item.quantity
            order_item = create_order_item(cart_item=product_cart_item, order=product_order)
            attachements = cart_item_attachment_list(cart_item=product_cart_item)
            total_attachment_price = calculate_attchment_price_order(
                product=product_cart_item.product,
                attachments=attachements,
                order_item=order_item
            )
            original_price = product_line_original_total(
                product=product_cart_item.product,
                attachment_total=total_attachment_price,
                quantity=quantity,
            )
            payable_price = product_line_payable_total(
                product=product_cart_item.product,
                attachment_total=total_attachment_price,
                quantity=quantity,
            )
            order_original_price_product += original_price
            order_payable_price_product += payable_price
            order_item.price = payable_price
            order_item.quantity = quantity
            order_item.save()
        product_order.total_price = order_original_price_product
        product_order.total_price_discount = order_payable_price_product
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
            original_price = product_line_original_total(
                product=cart_item.product,
                attachment_total=total_attachment_price,
                quantity=quantity,
            )
            payable_price = product_line_payable_total(
                product=cart_item.product,
                attachment_total=total_attachment_price,
                quantity=quantity,
            )
            order_original_price_product += original_price
            order_payable_price_product += payable_price
            order_item.price = payable_price
            order_item.quantity = quantity
            order_item.save()
        order.total_price = order_original_price_product
        order.total_price_discount = order_payable_price_product
        order.is_game = is_game
        order.save()
        remove_user_cart_items(user=user)
        order_list.append(order)
        return order_list


@transaction.atomic
def update_order(
    *,
    order_id: int,
    schedule: DeliveryData = None,
    discount: Discount = None,
    shipping_address=None,
    shipping_method: DeliveryType = None,
) -> Order:
    order = Order.objects.select_for_update().get(id=order_id)
    old_schedule = order.schedule
    update_fields = []
    if schedule is not None:
        if order.schedule_id is None:
            schedule = DeliveryData.objects.select_related("schedule").get(id=schedule.id)
            if schedule.is_used:
                raise DeliveryDataAlreadyUsedError()
            if is_delivery_schedule_full(schedule=schedule.schedule):
                raise DeliverySlotFullError()
            order.schedule = schedule
            update_fields.append("schedule")
        elif order.schedule_id != schedule.id:
            raise ValueError("Order already has a reservation.")
    if shipping_address is not None:
        order.shipping_address = shipping_address
        update_fields.append("shipping_address")
    if shipping_method is not None:
        order.shipping_method = shipping_method
        update_fields.append("shipping_method")
    if discount is not None:
        order.discount = discount
        update_fields.append("discount")
        if discount.value_type in (DiscountValueType.AMOUNT.value, DiscountValueType.PERCENT.value):
            order.total_price_discount = calculate_discounted_total(
                discount=discount,
                total_price=order_item_payable_total(order=order),
            )
        update_fields.append("total_price_discount")
    if update_fields:
        order.save(update_fields=[*update_fields, "updated_at"])
    if old_schedule is not None and old_schedule != schedule and not Order.objects.filter(schedule=old_schedule).exists():
        DeliveryData.objects.filter(id=old_schedule.id).update(is_used=False)
    return order
