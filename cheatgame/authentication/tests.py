from django.conf import settings
from django.core.cache import cache
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework.throttling import ScopedRateThrottle

from cheatgame.users.models import BaseUser


class LoginThrottleTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.user = BaseUser.objects.create_user(
            phone_number="09175550001",
            firstname="Login",
            lastname="Throttle",
            password="StrongPass123!",
        )
        self.user.phone_verified = True
        self.user.save(update_fields=["phone_verified"])

    def set_throttle_rate(self, scope, rate):
        self.original_throttle_rates = ScopedRateThrottle.THROTTLE_RATES.copy()
        ScopedRateThrottle.THROTTLE_RATES = self.original_throttle_rates.copy()
        ScopedRateThrottle.THROTTLE_RATES[scope] = rate

    def tearDown(self):
        if hasattr(self, "original_throttle_rates"):
            ScopedRateThrottle.THROTTLE_RATES = self.original_throttle_rates
        cache.clear()

    def test_customer_login_endpoint_is_throttled(self):
        self.set_throttle_rate("login", "1/min")
        first_response = self.client.post(
            "/api/auth/jwt/customer-login/",
            {
                "phone_number": self.user.phone_number,
                "password": "wrong-password",
            },
            format="json",
        )
        second_response = self.client.post(
            "/api/auth/jwt/customer-login/",
            {
                "phone_number": self.user.phone_number,
                "password": "wrong-password",
            },
            format="json",
        )

        self.assertEqual(first_response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(second_response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)


class AuthThrottleConfigurationTests(TestCase):
    def test_security_sprint_auth_throttle_rates_are_configured(self):
        throttle_rates = settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]

        self.assertIn("login", throttle_rates)
        self.assertIn("register", throttle_rates)
        self.assertIn("otp_request", throttle_rates)
        self.assertIn("otp_verify", throttle_rates)
        self.assertIn("password_reset_request", throttle_rates)
        self.assertIn("password_reset_confirm", throttle_rates)
