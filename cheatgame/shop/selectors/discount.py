import decimal
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from django.db.models import QuerySet
from django.utils import timezone

from cheatgame.shop.models import Discount, UserDiscount, DiscountType, DiscountValueType
from cheatgame.users.models import BaseUser


@dataclass
class DiscountValidationResult:
    is_valid: bool
    message: str
    discount: Optional[Discount] = None
    discounted_total: Optional[Decimal] = None


def calculate_discounted_total(*, discount: Discount, total_price: decimal) -> Decimal:
    total_price = Decimal(total_price)
    if discount.value_type == DiscountValueType.AMOUNT.value:
        return max(total_price - discount.amount, Decimal("0"))
    if discount.value_type == DiscountValueType.PERCENT.value:
        percent = min(discount.percent, 100)
        return max(total_price * Decimal(100 - percent) / Decimal(100), Decimal("0"))
    return total_price


def serialize_discount_validation(result: DiscountValidationResult) -> dict:
    discount = result.discount
    return {
        "message": result.is_valid,
        "valid": result.is_valid,
        "detail": result.message,
        "discount": None if discount is None else {
            "id": discount.id,
            "name": discount.name,
            "code": discount.code,
            "type": discount.type,
            "value_type": discount.value_type,
            "amount": discount.amount,
            "percent": discount.percent,
            "min_purchase_amount": discount.min_purchase_amount,
        },
        "discounted_total": result.discounted_total,
    }


def discount_list_admin() -> QuerySet[Discount]:
    return Discount.objects.filter(is_active=True).order_by("-id")


def discount_list_user(*, user: BaseUser) -> QuerySet[UserDiscount]:
    now = timezone.now()
    return UserDiscount.objects.filter(user=user, discount__valid_from__lt=now, discount__valid_until__gt=now,
                                       discount__is_active=True , discount__type = DiscountType.DIRECT , is_used = False)


def validate_discount_code(
    *, code: str, total_price: decimal, user: BaseUser = None, expected_type: DiscountType = None
) -> DiscountValidationResult:
    code = (code or "").strip()
    if not code:
        return DiscountValidationResult(False, "کد تخفیف را وارد کنید.")

    discount = Discount.objects.filter(code__iexact=code).first()
    if discount is None:
        return DiscountValidationResult(False, "کد تخفیف یافت نشد.")

    now = timezone.now()
    if not discount.is_active:
        return DiscountValidationResult(False, "کد تخفیف غیرفعال است.", discount=discount)
    if discount.valid_from > now:
        return DiscountValidationResult(False, "زمان استفاده از این کد تخفیف هنوز شروع نشده است.", discount=discount)
    if discount.valid_until < now:
        return DiscountValidationResult(False, "مهلت استفاده از این کد تخفیف تمام شده است.", discount=discount)
    if expected_type is not None and discount.type != expected_type.value:
        return DiscountValidationResult(False, "نوع کد تخفیف معتبر نیست.", discount=discount)
    if discount.min_purchase_amount > total_price:
        return DiscountValidationResult(
            False,
            "مبلغ سفارش برای استفاده از این کد تخفیف کافی نیست.",
            discount=discount,
        )

    if discount.type == DiscountType.DIRECT.value:
        if user is None:
            return DiscountValidationResult(False, "این کد تخفیف برای کاربر معتبر نیست.", discount=discount)
        user_discount = UserDiscount.objects.filter(discount=discount, user=user).first()
        if user_discount is None:
            return DiscountValidationResult(False, "این کد تخفیف برای شما فعال نیست.", discount=discount)
        if user_discount.is_used:
            return DiscountValidationResult(False, "این کد تخفیف قبلا استفاده شده است.", discount=discount)

    if discount.type == DiscountType.COUPON.value and discount.usage_number <= 0:
        return DiscountValidationResult(False, "ظرفیت استفاده از این کد تخفیف تمام شده است.", discount=discount)

    return DiscountValidationResult(
        True,
        "کد تخفیف معتبر است.",
        discount=discount,
        discounted_total=calculate_discounted_total(discount=discount, total_price=total_price),
    )


def check_discount_code(*, code: str, total_price: decimal, user: BaseUser) -> bool:
    return validate_discount_code(
        code=code,
        total_price=total_price,
        user=user,
        expected_type=DiscountType.DIRECT,
    ).is_valid


def check_coupon_code(*, code: str, total_price: decimal) -> bool:
    return validate_discount_code(
        code=code,
        total_price=total_price,
        expected_type=DiscountType.COUPON,
    ).is_valid
