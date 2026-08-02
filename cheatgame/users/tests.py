import io
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from unittest.mock import patch

import pyotp
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from cheatgame.users.apis import ChangePasswordApi
from cheatgame.users.models import Address, BaseUser, VerifyType


class RegisterApiTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()

    def tearDown(self):
        cache.clear()

    def test_register_accepts_normalized_iranian_mobile_number(self):
        response = self.client.post(
            "/api/user/register/",
            {
                "firstname": "Register",
                "lastname": "Customer",
                "phone_number": "09170000004",
                "password": "StrongPass123!",
                "confirm_password": "StrongPass123!",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(BaseUser.objects.filter(phone_number="09170000004").exists())

    def test_register_rejects_non_mobile_phone_number(self):
        response = self.client.post(
            "/api/user/register/",
            {
                "firstname": "Register",
                "lastname": "Customer",
                "phone_number": "02170000004",
                "password": "StrongPass123!",
                "confirm_password": "StrongPass123!",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(BaseUser.objects.filter(phone_number="02170000004").exists())

    def test_register_normalizes_international_and_localized_phone_numbers(self):
        response = self.client.post(
            "/api/user/register/",
            {
                "firstname": "Register",
                "lastname": "Normalized",
                "phone_number": "+۹۸۹۱۷۰۰۰۰۰۰۶",
                "password": "StrongPass123!",
                "confirm_password": "StrongPass123!",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(BaseUser.objects.filter(phone_number="09170000006").exists())

    def test_duplicate_registration_keeps_one_customer_identity(self):
        payload = {
            "firstname": "Register",
            "lastname": "Duplicate",
            "phone_number": "09170000007",
            "password": "StrongPass123!",
            "confirm_password": "StrongPass123!",
        }

        first_response = self.client.post("/api/user/register/", payload, format="json")
        second_response = self.client.post(
            "/api/user/register/",
            {**payload, "phone_number": "+989170000007"},
            format="json",
        )

        self.assertEqual(first_response.status_code, status.HTTP_200_OK)
        self.assertEqual(second_response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            BaseUser.objects.filter(phone_number="09170000007").count(),
            1,
        )

    def test_registered_unverified_customer_cannot_access_customer_flow(self):
        register_response = self.client.post(
            "/api/user/register/",
            {
                "firstname": "Register",
                "lastname": "Unverified",
                "phone_number": "09170000005",
                "password": "StrongPass123!",
                "confirm_password": "StrongPass123!",
            },
            format="json",
        )

        self.assertEqual(register_response.status_code, status.HTTP_200_OK)
        access_token = register_response.data["token"]["access"]
        response = self.client.post(
            "/api/user/create-address/",
            {
                "province": "Tehran",
                "city": "Tehran",
                "postal_code": "1234567890",
                "address_detail": "Unverified address",
            },
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {access_token}",
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(Address.objects.filter(postal_code="1234567890").exists())

        cart_response = self.client.get(
            "/api/shop/cart-item-list/",
            HTTP_AUTHORIZATION=f"Bearer {access_token}",
        )
        self.assertEqual(cart_response.status_code, status.HTTP_403_FORBIDDEN)


class OtpSecurityTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.user = BaseUser.objects.create_user(
            phone_number="09170000008",
            firstname="Otp",
            lastname="User",
            password="StrongPass123!",
        )

    def tearDown(self):
        cache.clear()

    def current_otp(self):
        self.user.refresh_from_db()
        self.assertIsNotNone(self.user.secret_key)
        return pyotp.TOTP(s=self.user.secret_key, interval=120).now()

    def assert_response_does_not_expose_otp(self, response, otp):
        response_text = str(response.data)
        self.assertNotIn(otp, response_text)
        self.assertNotIn("otp=", response_text.lower())
        self.assertNotIn(self.user.secret_key, response_text)

    @override_settings(DEBUG=False, IS_SEND_SMS=False)
    def test_request_verify_phone_does_not_return_or_print_otp_under_production_like_settings(self):
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            response = self.client.post(
                "/api/user/request-verify-phone/",
                {"phone_number": self.user.phone_number},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        otp = self.current_otp()
        self.assertEqual(self.user.verify_type, VerifyType.PHONENUMBER)
        self.assert_response_does_not_expose_otp(response, otp)
        self.assertEqual(stdout.getvalue(), "")

        verify_response = self.client.post(
            "/api/user/verify-phone/",
            {"phone_number": self.user.phone_number, "otp": otp},
            format="json",
        )

        self.assertEqual(verify_response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertTrue(self.user.phone_verified)
        self.assertIsNone(self.user.secret_key)

    @override_settings(DEBUG=False, IS_SEND_SMS=False)
    def test_resend_invalidates_the_previous_phone_otp(self):
        first_response = self.client.post(
            "/api/user/request-verify-phone/",
            {"phone_number": self.user.phone_number},
            format="json",
        )
        self.assertEqual(first_response.status_code, status.HTTP_200_OK)
        previous_otp = self.current_otp()

        second_response = self.client.post(
            "/api/user/request-verify-phone/",
            {"phone_number": self.user.phone_number},
            format="json",
        )
        self.assertEqual(second_response.status_code, status.HTTP_200_OK)
        current_otp = self.current_otp()

        old_verify_response = self.client.post(
            "/api/user/verify-phone/",
            {"phone_number": self.user.phone_number, "otp": previous_otp},
            format="json",
        )
        self.assertEqual(old_verify_response.status_code, status.HTTP_400_BAD_REQUEST)

        current_verify_response = self.client.post(
            "/api/user/verify-phone/",
            {"phone_number": self.user.phone_number, "otp": current_otp},
            format="json",
        )
        self.assertEqual(current_verify_response.status_code, status.HTTP_200_OK)

    @override_settings(DEBUG=False, IS_SEND_SMS=False)
    def test_otp_request_does_not_reveal_missing_or_inactive_accounts(self):
        missing_response = self.client.post(
            "/api/user/request-verify-phone/",
            {"phone_number": "09170000099"},
            format="json",
        )
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        inactive_response = self.client.post(
            "/api/user/request-verify-phone/",
            {"phone_number": self.user.phone_number},
            format="json",
        )

        self.assertEqual(missing_response.status_code, status.HTTP_200_OK)
        self.assertEqual(inactive_response.status_code, status.HTTP_200_OK)
        self.assertEqual(missing_response.data, inactive_response.data)
        self.user.refresh_from_db()
        self.assertIsNone(self.user.secret_key)

    @override_settings(DEBUG=False, IS_SEND_SMS=False)
    def test_inactive_user_cannot_verify_an_existing_otp(self):
        response = self.client.post(
            "/api/user/request-verify-phone/",
            {"phone_number": self.user.phone_number},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        otp = self.current_otp()
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])

        verify_response = self.client.post(
            "/api/user/verify-phone/",
            {"phone_number": self.user.phone_number, "otp": otp},
            format="json",
        )

        self.assertEqual(verify_response.status_code, status.HTTP_400_BAD_REQUEST)
        self.user.refresh_from_db()
        self.assertFalse(self.user.phone_verified)

    @override_settings(DEBUG=False, IS_SEND_SMS=False)
    def test_password_reset_request_does_not_return_otp_and_reset_still_works(self):
        self.assertFalse(self.user.phone_verified)
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            response = self.client.post(
                "/api/user/requset-change-password/",
                {"phone_number": self.user.phone_number},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        otp = self.current_otp()
        reset_requested_at = self.user.updated_at
        self.assertEqual(self.user.verify_type, VerifyType.PASSWORD)
        self.assert_response_does_not_expose_otp(response, otp)
        self.assertEqual(stdout.getvalue(), "")

        reset_response = self.client.post(
            "/api/user/change-password/",
            {
                "phone_number": self.user.phone_number,
                "otp": otp,
                "new_password": "NewStrongPass123!",
                "confirm_new_password": "NewStrongPass123!",
            },
            format="json",
        )

        self.assertEqual(reset_response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("NewStrongPass123!"))
        self.assertTrue(self.user.phone_verified)
        self.assertGreater(self.user.updated_at, reset_requested_at)
        self.assertIsNone(self.user.secret_key)
        self.assertIsNone(self.user.verify_type)

        login_response = self.client.post(
            "/api/auth/jwt/customer-login/",
            {
                "phone_number": self.user.phone_number,
                "password": "NewStrongPass123!",
            },
            format="json",
        )
        self.assertEqual(login_response.status_code, status.HTTP_200_OK)

        old_password_response = self.client.post(
            "/api/auth/jwt/customer-login/",
            {
                "phone_number": self.user.phone_number,
                "password": "StrongPass123!",
            },
            format="json",
        )
        self.assertEqual(
            old_password_response.status_code,
            status.HTTP_401_UNAUTHORIZED,
        )

        replay_response = self.client.post(
            "/api/user/change-password/",
            {
                "phone_number": self.user.phone_number,
                "otp": otp,
                "new_password": "ReplayMustNotWin123!",
                "confirm_new_password": "ReplayMustNotWin123!",
            },
            format="json",
        )
        self.assertEqual(replay_response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            replay_response.data,
            {
                "error": "کد تأیید صحیح نیست یا منقضی شده است.",
                "code": "PASSWORD_RESET_CODE_INVALID",
            },
        )
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("NewStrongPass123!"))

    @override_settings(DEBUG=False, IS_SEND_SMS=False)
    def test_phone_otp_is_accepted_first_time_across_totp_boundary(self):
        issued_at = timezone.make_aware(datetime(2026, 8, 2, 15, 9, 59))
        verified_at = timezone.make_aware(datetime(2026, 8, 2, 15, 10, 19))
        secret = pyotp.random_base32()
        BaseUser.objects.filter(pk=self.user.pk).update(
            secret_key=secret,
            verify_type=VerifyType.PHONENUMBER,
            updated_at=issued_at,
        )
        otp = pyotp.TOTP(secret, interval=120).at(issued_at)

        with patch("cheatgame.users.services.timezone.now", return_value=verified_at):
            first_response = self.client.post(
                "/api/user/verify-phone/",
                {"phone_number": self.user.phone_number, "otp": otp},
                format="json",
            )

        self.assertEqual(
            first_response.status_code,
            status.HTTP_200_OK,
            first_response.data,
        )
        self.user.refresh_from_db()
        self.assertTrue(self.user.phone_verified)
        self.assertIsNone(self.user.secret_key)
        self.assertIsNone(self.user.verify_type)

        replay_response = self.client.post(
            "/api/user/verify-phone/",
            {"phone_number": self.user.phone_number, "otp": otp},
            format="json",
        )
        self.assertEqual(replay_response.status_code, status.HTTP_400_BAD_REQUEST)

    @override_settings(DEBUG=False, IS_SEND_SMS=False)
    def test_phone_otp_expires_after_issued_policy_interval(self):
        issued_at = timezone.make_aware(datetime(2026, 8, 2, 15, 9, 59))
        expired_at = issued_at + timedelta(seconds=121)
        secret = pyotp.random_base32()
        BaseUser.objects.filter(pk=self.user.pk).update(
            secret_key=secret,
            verify_type=VerifyType.PHONENUMBER,
            updated_at=issued_at,
        )
        otp = pyotp.TOTP(secret, interval=120).at(issued_at)

        with patch("cheatgame.users.services.timezone.now", return_value=expired_at):
            response = self.client.post(
                "/api/user/verify-phone/",
                {"phone_number": self.user.phone_number, "otp": otp},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.user.refresh_from_db()
        self.assertFalse(self.user.phone_verified)
        self.assertEqual(self.user.secret_key, secret)

    @override_settings(DEBUG=False, IS_SEND_SMS=False)
    def test_password_recovery_phone_variants_resolve_one_customer(self):
        original_count = BaseUser.objects.count()
        variants = (
            self.user.phone_number,
            f"98{self.user.phone_number[1:]}",
            f"+98{self.user.phone_number[1:]}",
            self.user.phone_number.translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")),
            self.user.phone_number.translate(str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")),
            f" {self.user.phone_number[:4]} {self.user.phone_number[4:7]} {self.user.phone_number[7:]} ",
        )

        for phone_number in variants:
            cache.clear()
            response = self.client.post(
                "/api/user/requset-change-password/",
                {"phone_number": phone_number},
                format="json",
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.assertEqual(BaseUser.objects.count(), original_count)
        self.user.refresh_from_db()
        self.assertEqual(self.user.verify_type, VerifyType.PASSWORD)

    @override_settings(DEBUG=False, IS_SEND_SMS=False)
    def test_customer_with_unusable_password_can_recover_securely(self):
        self.user.set_unusable_password()
        self.user.save(update_fields=["password"])

        request_response = self.client.post(
            "/api/user/requset-change-password/",
            {"phone_number": self.user.phone_number},
            format="json",
        )
        self.assertEqual(request_response.status_code, status.HTTP_200_OK)
        otp = self.current_otp()

        reset_response = self.client.post(
            "/api/user/change-password/",
            {
                "phone_number": self.user.phone_number,
                "otp": otp,
                "new_password": "EstablishedSecurely123!",
                "confirm_new_password": "EstablishedSecurely123!",
            },
            format="json",
        )
        self.assertEqual(reset_response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("EstablishedSecurely123!"))

    def test_password_recovery_preserves_leading_zero_and_normalizes_localized_otp(self):
        serializer = ChangePasswordApi.InputChangePasswordSerializer(
            data={
                "phone_number": self.user.phone_number,
                "otp": "۰۱۲۳۴۵",
                "new_password": "NewStrongPass123!",
                "confirm_new_password": "NewStrongPass123!",
            }
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["otp"], "012345")

    @override_settings(DEBUG=False, IS_SEND_SMS=False)
    def test_password_recovery_and_login_preserve_unicode_password_exactly(self):
        passwords = (
            "Latin@1۱۲",
            "Latin@1١٢",
            "Mix@1۱۲١",
        )

        for password in passwords:
            with self.subTest(password_kind=len(password)):
                request_response = self.client.post(
                    "/api/user/requset-change-password/",
                    {"phone_number": self.user.phone_number},
                    format="json",
                )
                self.assertEqual(request_response.status_code, status.HTTP_200_OK)
                otp = self.current_otp()

                reset_response = self.client.post(
                    "/api/user/change-password/",
                    {
                        "phone_number": self.user.phone_number,
                        "otp": otp,
                        "new_password": password,
                        "confirm_new_password": password,
                    },
                    format="json",
                )
                self.assertEqual(reset_response.status_code, status.HTTP_200_OK)

                self.user.refresh_from_db()
                self.assertTrue(self.user.check_password(password))
                login_response = self.client.post(
                    "/api/auth/jwt/customer-login/",
                    {
                        "phone_number": self.user.phone_number,
                        "password": password,
                    },
                    format="json",
                )
                self.assertEqual(login_response.status_code, status.HTTP_200_OK)

    def test_password_recovery_mismatch_returns_customer_actionable_error(self):
        serializer = ChangePasswordApi.InputChangePasswordSerializer(
            data={
                "phone_number": self.user.phone_number,
                "otp": "012345",
                "new_password": "NewStrongPass123!",
                "confirm_new_password": "DifferentPass123!",
            }
        )

        self.assertFalse(serializer.is_valid())
        self.assertEqual(
            serializer.errors["non_field_errors"][0],
            "رمز عبور و تکرار آن یکسان نیستند.",
        )

    @override_settings(DEBUG=False, IS_SEND_SMS=False)
    def test_request_verify_email_does_not_return_or_print_otp_under_production_like_settings(self):
        self.client.force_authenticate(self.user)
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            response = self.client.post(
                "/api/user/requset-verify-emali/",
                {"email": "otp-user@example.com"},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        otp = self.current_otp()
        self.assertEqual(self.user.verify_type, VerifyType.EMAIL)
        self.assert_response_does_not_expose_otp(response, otp)
        self.assertEqual(stdout.getvalue(), "")


class UserProfileTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = BaseUser.objects.create_user(
            phone_number="09170000009",
            firstname="Old",
            lastname="Name",
            password="StrongPass123!",
        )
        self.user.email = "old@example.com"
        self.user.phone_verified = True
        self.user.save(update_fields=["email", "phone_verified"])
        self.client.force_authenticate(self.user)

    def test_customer_can_update_profile_without_email(self):
        response = self.client.put(
            "/api/user/user/",
            {
                "firstname": "New",
                "lastname": "Name",
            },
            format="multipart",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.user.refresh_from_db()
        self.assertEqual(self.user.firstname, "New")
        self.assertEqual(self.user.lastname, "Name")
        self.assertIsNone(self.user.email)
        self.assertEqual(response.data["phone_number"], self.user.phone_number)


class AddressOwnershipTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = self.create_verified_user("09170000006")
        self.other_user = self.create_verified_user("09170000007")
        self.other_address = Address.objects.create(
            user=self.other_user,
            province="Tehran",
            city="Tehran",
            postal_code="2234567890",
            address_detail="Other address",
        )
        self.client.force_authenticate(self.user)

    def create_verified_user(self, phone_number):
        user = BaseUser.objects.create_user(
            phone_number=phone_number,
            firstname="Address",
            lastname="User",
            password="StrongPass123!",
        )
        user.phone_verified = True
        user.save(update_fields=["phone_verified"])
        return user

    def test_customer_cannot_update_another_users_address(self):
        response = self.client.put(
            f"/api/user/address-detail/{self.other_address.id}/",
            {
                "province": "Updated",
                "city": "Updated",
                "postal_code": "9999999999",
                "address_detail": "Updated address",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.other_address.refresh_from_db()
        self.assertEqual(self.other_address.province, "Tehran")
        self.assertEqual(self.other_address.postal_code, "2234567890")

    def test_customer_cannot_delete_another_users_address(self):
        response = self.client.delete(f"/api/user/address-detail/{self.other_address.id}/")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(Address.objects.filter(id=self.other_address.id).exists())

    def test_verified_customer_can_create_address(self):
        response = self.client.post(
            "/api/user/create-address/",
            {
                "province": "Tehran",
                "city": "Tehran",
                "postal_code": "3234567890",
                "address_detail": "Own address",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(Address.objects.filter(user=self.user, postal_code="3234567890").exists())
