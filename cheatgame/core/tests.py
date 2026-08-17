import importlib
import io
import logging
import logging.config
import os
import sys
from copy import deepcopy
from unittest.mock import patch

from config.django import base as base_settings
from corsheaders.middleware import CorsMiddleware
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, override_settings


PRODUCTION_ENV = {
    "SECRET_KEY": "test-production-secret-key-with-at-least-32-characters",
    "JWT_SIGNING_KEY": "test-production-jwt-key-distinct-with-at-least-32-characters",
    "DEBUG": "False",
    "CHEATSGAME_RUNTIME_ENVIRONMENT": "production",
    "DATABASE_URL": "postgresql://test:test@db.example.com:5432/cheatgame",
    "ALLOWED_HOSTS": "api.cheatsg.ir",
    "CORS_ALLOWED_ORIGINS": "https://cheatsg.ir,https://admin.cheatsg.ir",
    "CSRF_TRUSTED_ORIGINS": "https://cheatsg.ir,https://admin.cheatsg.ir",
    "AWS_S3_ENDPOINT_URL": "https://s3.example.com",
    "AWS_ACCESS_KEY_ID": "access-key",
    "AWS_SECRET_ACCESS_KEY": "secret-key",
    "AWS_STORAGE_BUCKET_NAME": "bucket",
    "AWS_S3_REGION_NAME": "us-east-1",
    "AWS_S3_CUSTOM_DOMAIN": "cdn.cheatsg.ir",
    "AWS_STORAGE_ENVIRONMENT": "production",
    "IS_SEND_SMS": "True",
    "VERIFY_PATTERN": "verify-pattern",
    "FORGET_PASSWORD_PATTERN": "recovery-pattern",
    "PANEL_SMS_URL": "https://edge.ippanel.com/v1/api/send",
    "PANEL_SMS_API_KEY": "test-api-key",
    "PANEL_SMS_FROM": "+983000505",
    "PAYMENT_GATEWAY_PROVIDER": "zarinpal",
    "FINANCIAL_ZARINPAL_ENABLED": "True",
    "ZARINPAL_MERCHANT_ID": "a" * 36,
    "ZARINPAL_SANDBOX": "False",
    "ZARINPAL_REQUEST_URL": "https://payment.zarinpal.com/pg/v4/payment/request.json",
    "ZARINPAL_VERIFY_URL": "https://payment.zarinpal.com/pg/v4/payment/verify.json",
    "ZARINPAL_STARTPAY_URL": "https://payment.zarinpal.com/pg/StartPay/{authority}",
    "FINANCIAL_ZARINPAL_ACCOUNT_KEY": "production-terminal",
    "FINANCIAL_ZARINPAL_OWNER_KEY": "cheatsg-production",
    "FINANCIAL_PROVIDER_CALLBACK_BASE_URL": "https://api.cheatsg.ir",
    "DIGITAL_PAYMENT_CUSTOMER_RETURN_BASE_URL": "https://cheatsg.ir",
    "FINANCIAL_CERTIFICATION_PROVIDER_ENABLED": "False",
    "FINANCIAL_CERTIFICATION_SECRET": "",
    "FINANCIAL_CERTIFICATION_ALLOWED_HOSTS": "",
}


class ProductionSettingsTests(SimpleTestCase):
    def test_local_sqlite_name_is_django_52_compatible(self):
        database = base_settings.DATABASES["default"]
        if database["ENGINE"] == "django.db.backends.sqlite3":
            self.assertIsInstance(database["NAME"], str)

    def import_production_settings(self, env_overrides):
        sys.modules.pop("config.django.production", None)
        with patch.dict(os.environ, env_overrides, clear=False):
            module = importlib.import_module("config.django.production")
        sys.modules.pop("config.django.production", None)
        return module

    def test_production_settings_are_hardened_with_explicit_env(self):
        module = self.import_production_settings(PRODUCTION_ENV)

        self.assertFalse(module.DEBUG)
        self.assertFalse(module.CORS_ALLOW_ALL_ORIGINS)
        self.assertIn("idempotency-key", module.CORS_ALLOW_HEADERS)
        self.assertEqual(module.ALLOWED_HOSTS, ["api.cheatsg.ir"])
        self.assertEqual(
            module.CORS_ALLOWED_ORIGINS,
            ["https://cheatsg.ir", "https://admin.cheatsg.ir"],
        )
        self.assertEqual(
            module.CSRF_TRUSTED_ORIGINS,
            ["https://cheatsg.ir", "https://admin.cheatsg.ir"],
        )
        self.assertEqual(module.CORS_ORIGIN_WHITELIST, module.CORS_ALLOWED_ORIGINS)
        self.assertEqual(
            module.SECRET_KEY,
            "test-production-secret-key-with-at-least-32-characters",
        )
        self.assertTrue(module.SESSION_COOKIE_SECURE)
        self.assertTrue(module.SESSION_COOKIE_HTTPONLY)
        self.assertTrue(module.CSRF_COOKIE_SECURE)
        self.assertEqual(module.SESSION_COOKIE_SAMESITE, "Lax")
        self.assertEqual(module.CSRF_COOKIE_SAMESITE, "Lax")
        self.assertTrue(module.SECURE_SSL_REDIRECT)
        self.assertEqual(module.SECURE_PROXY_SSL_HEADER, ("HTTP_X_FORWARDED_PROTO", "https"))
        self.assertEqual(
            module.SECURE_REDIRECT_EXEMPT,
            [r"^health/live/$", r"^health/ready/$"],
        )
        self.assertTrue(module.SECURE_CONTENT_TYPE_NOSNIFF)
        self.assertGreater(module.SECURE_HSTS_SECONDS, 0)
        self.assertTrue(module.SECURE_HSTS_INCLUDE_SUBDOMAINS)
        self.assertTrue(module.SECURE_HSTS_PRELOAD)
        self.assertEqual(module.SECURE_REFERRER_POLICY, "same-origin")
        self.assertEqual(module.X_FRAME_OPTIONS, "DENY")
        self.assertFalse(module.PAYMENT_FAKE_PROVIDER_ENABLED)
        self.assertTrue(module.FINANCIAL_ZARINPAL_ENABLED)
        self.assertEqual(module.SIMPLE_JWT["SIGNING_KEY"], PRODUCTION_ENV["JWT_SIGNING_KEY"])

    def test_production_logging_emits_only_dedicated_provider_transport_info(self):
        module = self.import_production_settings(PRODUCTION_ENV)
        target_name = "cheatgame.financial_core.provider_transport"
        noisy_name = "cheatgame.noisy"
        target = logging.getLogger(target_name)
        noisy = logging.getLogger(noisy_name)
        root = logging.getLogger()
        previous = {
            logger: (logger.level, list(logger.handlers), logger.propagate, logger.disabled)
            for logger in (target, noisy, root)
        }
        stream = io.StringIO()
        config = deepcopy(module.LOGGING)
        config["handlers"]["provider_transport_console"]["stream"] = stream

        try:
            logging.config.dictConfig(config)
            target.info(
                'zarinpal_transport {"event":"provider_transport_response",'
                '"http_status":200,"response_shape":"object"}'
            )
            noisy.info("unrelated-noisy-info")

            rendered = stream.getvalue()
            self.assertIn("provider_transport_response", rendered)
            self.assertIn('"http_status":200', rendered)
            self.assertNotIn("unrelated-noisy-info", rendered)
            self.assertEqual(target.getEffectiveLevel(), logging.INFO)
            self.assertGreaterEqual(root.getEffectiveLevel(), logging.WARNING)
            self.assertFalse(target.propagate)
        finally:
            for logger, (level, handlers, propagate, disabled) in previous.items():
                for handler in logger.handlers:
                    if handler not in handlers:
                        handler.close()
                logger.handlers = handlers
                logger.setLevel(level)
                logger.propagate = propagate
                logger.disabled = disabled

    def test_production_cors_preflight_allows_payment_idempotency_header(self):
        module = self.import_production_settings(PRODUCTION_ENV)
        request = RequestFactory().options(
            "/api/digital-products/customer/checkout/example/payment/request/",
            HTTP_ORIGIN="https://cheatsg.ir",
            HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
            HTTP_ACCESS_CONTROL_REQUEST_HEADERS=(
                "authorization,content-type,idempotency-key"
            ),
        )
        with self.settings(
            CORS_ALLOWED_ORIGINS=module.CORS_ALLOWED_ORIGINS,
            CORS_ALLOW_HEADERS=module.CORS_ALLOW_HEADERS,
        ):
            response = CorsMiddleware(lambda _request: HttpResponse())(request)

        allowed_headers = {
            value.strip().lower()
            for value in response["access-control-allow-headers"].split(",")
        }
        self.assertIn("idempotency-key", allowed_headers)

    def test_production_settings_reject_fake_payment_provider(self):
        env = {**PRODUCTION_ENV, "PAYMENT_GATEWAY_PROVIDER": "fake"}

        with self.assertRaises(ImproperlyConfigured):
            self.import_production_settings(env)

    def test_production_settings_require_distinct_jwt_signing_material(self):
        env = {**PRODUCTION_ENV, "JWT_SIGNING_KEY": PRODUCTION_ENV["SECRET_KEY"]}
        with self.assertRaises(ImproperlyConfigured):
            self.import_production_settings(env)

    def test_production_settings_reject_low_entropy_signing_material(self):
        with self.assertRaises(ImproperlyConfigured):
            self.import_production_settings({**PRODUCTION_ENV, "SECRET_KEY": "s" * 64})
        with self.assertRaises(ImproperlyConfigured):
            self.import_production_settings({**PRODUCTION_ENV, "JWT_SIGNING_KEY": "j" * 64})

    def test_production_settings_require_zarinpal_and_real_mode(self):
        with self.assertRaises(ImproperlyConfigured):
            self.import_production_settings({**PRODUCTION_ENV, "FINANCIAL_ZARINPAL_ENABLED": "False"})
        with self.assertRaises(ImproperlyConfigured):
            self.import_production_settings({**PRODUCTION_ENV, "ZARINPAL_SANDBOX": "True"})

    def test_production_settings_require_exact_callback_and_return_origins(self):
        with self.assertRaises(ImproperlyConfigured):
            self.import_production_settings(
                {**PRODUCTION_ENV, "FINANCIAL_PROVIDER_CALLBACK_BASE_URL": "https://staging.example"}
            )
        with self.assertRaises(ImproperlyConfigured):
            self.import_production_settings(
                {**PRODUCTION_ENV, "DIGITAL_PAYMENT_CUSTOMER_RETURN_BASE_URL": "https://staging.example"}
            )

    def test_production_settings_require_sms_and_isolated_media(self):
        with self.assertRaises(ImproperlyConfigured):
            self.import_production_settings({**PRODUCTION_ENV, "PANEL_SMS_API_KEY": ""})
        with self.assertRaises(ImproperlyConfigured):
            self.import_production_settings(
                {**PRODUCTION_ENV, "AWS_STORAGE_ENVIRONMENT": "staging"}
            )

    def test_production_runtime_rejects_financial_certification_before_startup(self):
        env = {
            **PRODUCTION_ENV,
            "CHEATSGAME_RUNTIME_ENVIRONMENT": "production",
            "FINANCIAL_CERTIFICATION_PROVIDER_ENABLED": "True",
            "FINANCIAL_CERTIFICATION_SECRET": "x" * 48,
            "FINANCIAL_CERTIFICATION_ALLOWED_HOSTS": "api.cheatsg.ir",
        }

        with self.assertRaises(ImproperlyConfigured):
            self.import_production_settings(env)

    def test_staging_identity_rejects_nonstaging_certification_host(self):
        env = {
            **PRODUCTION_ENV,
            "CHEATSGAME_RUNTIME_ENVIRONMENT": "staging",
            "ALLOWED_HOSTS": "backend-cheatsgame-staging.liara.run",
            "CORS_ALLOWED_ORIGINS": "https://frontend-cheatsgame-v1-staging.liara.run",
            "CSRF_TRUSTED_ORIGINS": "https://admin-cheatsgame-staging.liara.run",
            "FINANCIAL_CERTIFICATION_PROVIDER_ENABLED": "True",
            "FINANCIAL_CERTIFICATION_SECRET": "x" * 48,
            "FINANCIAL_CERTIFICATION_ALLOWED_HOSTS": "api.example.com",
        }

        with self.assertRaises(ImproperlyConfigured):
            self.import_production_settings(env)

    @override_settings(PAYMENT_FAKE_PROVIDER_ENABLED=False)
    def test_fake_callback_is_not_wired_when_provider_is_disabled(self):
        sys.modules.pop("cheatgame.api.urls", None)
        module = importlib.import_module("cheatgame.api.urls")
        try:
            self.assertNotIn(
                "fake-payment-callback",
                {getattr(pattern, "name", None) for pattern in module.urlpatterns},
            )
        finally:
            sys.modules.pop("cheatgame.api.urls", None)

    def test_production_settings_reject_debug_true(self):
        env = {**PRODUCTION_ENV, "DEBUG": "True"}

        with self.assertRaises(ImproperlyConfigured):
            self.import_production_settings(env)

    def test_production_settings_require_explicit_allowed_hosts(self):
        env = {**PRODUCTION_ENV, "ALLOWED_HOSTS": ""}

        with self.assertRaises(ImproperlyConfigured):
            self.import_production_settings(env)

    def test_production_settings_require_explicit_runtime_identity(self):
        with self.assertRaises(ImproperlyConfigured):
            self.import_production_settings(
                {**PRODUCTION_ENV, "CHEATSGAME_RUNTIME_ENVIRONMENT": ""}
            )

    def test_production_settings_reject_wildcard_allowed_hosts(self):
        env = {**PRODUCTION_ENV, "ALLOWED_HOSTS": "*"}

        with self.assertRaises(ImproperlyConfigured):
            self.import_production_settings(env)

    def test_production_settings_reject_loopback_hosts(self):
        env = {
            **PRODUCTION_ENV,
            "ALLOWED_HOSTS": "api.cheatsg.ir,localhost,127.0.0.1,[::1]",
        }

        with self.assertRaises(ImproperlyConfigured):
            self.import_production_settings(env)

    def test_production_settings_reject_extra_allowed_hosts(self):
        with self.assertRaises(ImproperlyConfigured):
            self.import_production_settings(
                {**PRODUCTION_ENV, "ALLOWED_HOSTS": "api.cheatsg.ir,api-alt.cheatsg.ir"}
            )

    def test_production_settings_reject_weakened_transport_headers(self):
        unsafe_overrides = (
            {"SECURE_SSL_REDIRECT": "False"},
            {"SECURE_HSTS_SECONDS": "0"},
            {"SECURE_HSTS_INCLUDE_SUBDOMAINS": "False"},
            {"SECURE_HSTS_PRELOAD": "False"},
            {"SECURE_CONTENT_TYPE_NOSNIFF": "False"},
            {"SECURE_REFERRER_POLICY": "unsafe-url"},
        )
        for override in unsafe_overrides:
            with self.subTest(override=override):
                with self.assertRaises(ImproperlyConfigured):
                    self.import_production_settings({**PRODUCTION_ENV, **override})

    def test_production_settings_preserve_existing_redirect_exemptions(self):
        with patch.object(
            base_settings,
            "SECURE_REDIRECT_EXEMPT",
            [r"^existing/internal/$"],
            create=True,
        ):
            module = self.import_production_settings(PRODUCTION_ENV)

        self.assertEqual(
            module.SECURE_REDIRECT_EXEMPT,
            [r"^existing/internal/$", r"^health/live/$", r"^health/ready/$"],
        )

    def test_production_settings_require_explicit_cors_origins(self):
        env = {**PRODUCTION_ENV, "CORS_ALLOWED_ORIGINS": ""}

        with self.assertRaises(ImproperlyConfigured):
            self.import_production_settings(env)

    def test_production_settings_require_explicit_csrf_origins(self):
        env = {**PRODUCTION_ENV, "CSRF_TRUSTED_ORIGINS": ""}

        with self.assertRaises(ImproperlyConfigured):
            self.import_production_settings(env)

    def test_production_settings_reject_staging_origin(self):
        env = {
            **PRODUCTION_ENV,
            "CORS_ALLOWED_ORIGINS": (
                "https://cheatsg.ir,https://admin-cheatsgame-staging.liara.run"
            ),
        }

        with self.assertRaises(ImproperlyConfigured):
            self.import_production_settings(env)

    def test_production_settings_reject_disabled_certification_secret_residue(self):
        env = {
            **PRODUCTION_ENV,
            "FINANCIAL_CERTIFICATION_PROVIDER_ENABLED": "False",
            "FINANCIAL_CERTIFICATION_SECRET": "x" * 48,
        }

        with self.assertRaises(ImproperlyConfigured):
            self.import_production_settings(env)

    def test_production_settings_reject_certification_gateway_identity(self):
        env = {
            **PRODUCTION_ENV,
            "PAYMENT_GATEWAY_PROVIDER": "financial_certification",
        }

        with self.assertRaises(ImproperlyConfigured):
            self.import_production_settings(env)

    @override_settings(
        CORS_ALLOW_ALL_ORIGINS=False,
        CORS_ALLOWED_ORIGINS=["https://admin-cheatsgame-staging.liara.run"],
    )
    def test_private_admin_staging_origin_is_allowed_exactly(self):
        response = self.client.options(
            "/health/live/",
            HTTP_ORIGIN="https://admin-cheatsgame-staging.liara.run",
            HTTP_ACCESS_CONTROL_REQUEST_METHOD="GET",
        )

        self.assertEqual(
            response.headers["Access-Control-Allow-Origin"],
            "https://admin-cheatsgame-staging.liara.run",
        )

        rejected = self.client.options(
            "/health/live/",
            HTTP_ORIGIN="https://untrusted.example",
            HTTP_ACCESS_CONTROL_REQUEST_METHOD="GET",
        )

        self.assertNotIn("Access-Control-Allow-Origin", rejected.headers)
