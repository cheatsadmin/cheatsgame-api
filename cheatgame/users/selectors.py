from django.db.models import QuerySet
from django.utils import timezone
from gunicorn.config import User
from rest_framework.exceptions import APIException

from .filter import UserFilter
from .models import BaseUser, VerifyType, Address, FavoriteProduct, UserTypes
import pyotp

from ..general.models import  ContactForm
from ..product.models import Product

def check_user_exists(* , phone_number: str) -> bool:
    return BaseUser.objects.filter(phone_number=phone_number).exists()
def get_user(*, phone_number: str) -> BaseUser:
    return BaseUser.objects.get(phone_number=phone_number)


def verify_phone_otp(*, phone_number: str, otp: str) -> bool:
    try:
        user = BaseUser.objects.get(phone_number=phone_number)
    except BaseUser.DoesNotExist:
        return False
    if not user.is_active or user.verify_type != VerifyType.PHONENUMBER or not user.secret_key:
        return False
    totp = pyotp.TOTP(s=user.secret_key, interval=120)
    return totp.verify(otp=str(otp))


def verify_password_otp(*, phone_number: str, otp: str) -> bool:
    try:
        user = BaseUser.objects.get(phone_number=phone_number)
    except BaseUser.DoesNotExist:
        return False
    if not user.is_active or user.verify_type != VerifyType.PASSWORD or not user.secret_key:
        return False
    totp = pyotp.TOTP(s=user.secret_key, interval=120)
    return totp.verify(otp)


def verify_email_otp(*, user: BaseUser, otp: str) -> bool:
    totp = pyotp.TOTP(s=user.secret_key, interval=120)
    if not user.verify_type == VerifyType.EMAIL:
        return False
    return totp.verify(otp=otp)


def user_address_list(*, user: BaseUser) -> QuerySet[Address]:
    return Address.objects.filter(user=user)


def customers_numbers() -> int:
    return BaseUser.objects.filter(user_type=UserTypes.CUSTOMER).count()

def number_of_user_address(*, user: BaseUser) -> int:
    return Address.objects.filter(user=user).count()


def number_of_favorite_product(*, user: BaseUser) -> int:
    return FavoriteProduct.objects.filter(user=user).count()


def favoirte_product_exists(*, user: BaseUser, product: Product) -> bool:
    return FavoriteProduct.objects.filter(user=user, product=product).exists()


def user_favorite_product_list(*, user: BaseUser) -> QuerySet[FavoriteProduct]:
    return FavoriteProduct.objects.filter(user=user)


def get_contact_form_list(* , is_checked:bool) -> QuerySet[ContactForm]:
    return ContactForm.objects.filter(is_checked = is_checked)

def user_list(* , filters=None) -> QuerySet[User]:
    filters = filters or {}
    qs = BaseUser.objects.all()
    return UserFilter(filters , qs).qs


def user_number_register(* ,value)-> int:
    limit = 2
    created_at__in = value.split(",")
    if len(created_at__in) > limit:
        raise APIException("please just add two created_at with , in the middle")
    created_at_0, created_at_1 = created_at__in

    if not created_at_1:
        created_at_1 = timezone.now()

    queryset = BaseUser.objects.filter(user_type = UserTypes.CUSTOMER)

    if not created_at_0:
        return queryset.filter(created_at__date__lt=created_at_1).count()

    return queryset.filter(created_at__range=(created_at_0, created_at_1)).count()
