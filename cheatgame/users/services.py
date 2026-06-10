from .models import BaseUser, Address, FavoriteProduct
import pyotp

from ..general.models import  ContactForm
from ..product.models import Product


def create_user(*, firstname: str, lastname: str, phone_number: str, password: str, user_type: int) -> BaseUser:
    return BaseUser.objects.create_user(firstname=firstname, lastname=lastname, phone_number=phone_number,
                                        password=password,
                                        user_type=user_type)


def update_user(*, user: BaseUser, firstname: str, lastname: str, email: str, birthdate: str,
                email_status: bool, profile_image=None) -> BaseUser:
    user.firstname = firstname
    user.lastname = lastname
    user.email = email
    user.birthdate = birthdate
    user.email_verified = email_status
    if profile_image:
        user.profile_image = profile_image
    user.save()
    return user


def update_user_secret(*, user: BaseUser, secret: str, verify_type: int) -> None:
    user.secret_key = secret
    user.verify_type = verify_type
    user.save()


def generate_otp(*, user: BaseUser, verify_type: int) -> str:
    secret = pyotp.random_base32()
    update_user_secret(user=user, secret=secret, verify_type=verify_type)
    totp = pyotp.TOTP(s=secret, interval=120)
    return totp.now()


def change_password(user: BaseUser, password) -> None:
    user.set_password(password)
    user.full_clean()
    user.save()


def confirm_phone(*, phone_number: str) -> None:
    BaseUser.objects.filter(phone_number=phone_number).update(secret_key=None, verify_type=None, phone_verified=True)


def confirm_email(*, user: BaseUser) -> None:
    user.secret_key = None
    user.verify_type = None
    user.email_verified = True
    user.save()


def create_address(*, province: str, city: str, postal_code: str, address_detail: str, user: BaseUser) -> Address:
    return Address.objects.create(
        province=province,
        city=city,
        postal_code=postal_code,
        address_detail=address_detail,
        user=user
    )


def create_favorite_product(*, user: BaseUser, product: Product) -> FavoriteProduct:
    return FavoriteProduct.objects.create(user=user, product=product)


def delete_favorite_product(*, user: BaseUser, id: int) -> None:
    FavoriteProduct.objects.get(user=user, id=id).delete()



def update_address(*, user: BaseUser, address_id: int, province: str, city: str, postal_code: str, address_detail: str) -> Address:
    address = Address.objects.get(id=address_id, user=user)
    address.province = province
    address.city = city
    address.postal_code = postal_code
    address.address_detail = address_detail
    address.save()
    return address


def delete_address(*, user: BaseUser, address_id: int) -> None:
    return Address.objects.get(id=address_id, user=user).delete()


def create_contact_form(*, firstname: str, lastname: str, description: str, phone_number: str,
                        subject: str) -> ContactForm:
    return ContactForm.objects.create(
        firstname=firstname,
        lastname=lastname,
        description=description,
        phone_number=phone_number,
        subject=subject
    )



def update_contact_form(contact_form_id:int) -> ContactForm:
    contact_form = ContactForm.objects.get(id = contact_form_id)
    contact_form.is_checked = True
    contact_form.save()
    return contact_form
