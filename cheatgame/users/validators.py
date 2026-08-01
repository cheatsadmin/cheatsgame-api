from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ValidationError

import re


_LOCALIZED_DIGITS = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
    "01234567890123456789",
)


def normalize_iranian_phone_number(phone_number):
    value = str(phone_number or "").translate(_LOCALIZED_DIGITS).strip()
    value = re.sub(r"[\s()\-]", "", value)

    if re.fullmatch(r"\+989\d{9}", value):
        return f"0{value[3:]}"
    if re.fullmatch(r"00989\d{9}", value):
        return f"0{value[4:]}"
    if re.fullmatch(r"989\d{9}", value):
        return f"0{value[2:]}"
    return value



def phone_number_validator(phone_number):
    regex = re.compile(r'^09\d{9}$')
    if not regex.fullmatch(normalize_iranian_phone_number(phone_number)):
        raise ValidationError(
            _("شماره تماس وارد شده معتبر نمی باشد.")
        )
def check_phone_number(phone_number):
    regex = re.compile('[0-9]')
    for char in phone_number:
        if not regex.fullmatch(char):
            return False
    return True
def number_validator(password):
    regex = re.compile('[0-9]')
    if regex.search(password) == None:
        raise ValidationError(
                _("رمز عبور باید شامل عدد باشد."),
                code="password_must_include_number"
                )

def letter_validator(password):
    regex = re.compile('[a-zA-Z]')
    if regex.search(password) == None:
        raise ValidationError(
                _("رمز عبور باید شامل حرف باشد."),
                code="password_must_include_letter"
                )

def special_char_validator(password):
    regex = re.compile('[^a-zA-Z0-9]')
    if regex.search(password) == None:
        raise ValidationError(
                _("رمز عبور باید شامل یک نشانه مثل خط تیره یا @ باشد."),
                code="password_must_include_special_char"
                )
