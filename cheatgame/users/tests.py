import io
from contextlib import redirect_stdout

import pyotp
from django.test import TestCase, override_settings
from rest_framework import status
from rest_framework.test import APIClient

from cheatgame.users.models import Address, BaseUser, VerifyType


class RegisterApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()

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
        self.client = APIClient()
        self.user = BaseUser.objects.create_user(
            phone_number="09170000008",
            firstname="Otp",
            lastname="User",
            password="StrongPass123!",
        )

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
    def test_password_reset_request_does_not_return_otp_and_reset_still_works(self):
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            response = self.client.post(
                "/api/user/requset-change-password/",
                {"phone_number": self.user.phone_number},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        otp = self.current_otp()
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
