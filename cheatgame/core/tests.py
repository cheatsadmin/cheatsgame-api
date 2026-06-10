import importlib
import os
import sys
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase


PRODUCTION_ENV = {
    "SECRET_KEY": "test-production-secret-key",
    "DEBUG": "False",
    "ALLOWED_HOSTS": "api.example.com",
    "CORS_ALLOWED_ORIGINS": "https://www.example.com,https://admin.example.com",
    "AWS_S3_ENDPOINT_URL": "https://s3.example.com",
    "AWS_ACCESS_KEY_ID": "access-key",
    "AWS_SECRET_ACCESS_KEY": "secret-key",
    "AWS_STORAGE_BUCKET_NAME": "bucket",
    "AWS_S3_REGION_NAME": "us-east-1",
}


class ProductionSettingsTests(SimpleTestCase):
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
        self.assertEqual(module.ALLOWED_HOSTS, ["api.example.com"])
        self.assertEqual(
            module.CORS_ALLOWED_ORIGINS,
            ["https://www.example.com", "https://admin.example.com"],
        )
        self.assertEqual(module.CORS_ORIGIN_WHITELIST, module.CORS_ALLOWED_ORIGINS)
        self.assertEqual(module.SECRET_KEY, "test-production-secret-key")
        self.assertTrue(module.SESSION_COOKIE_SECURE)
        self.assertTrue(module.SESSION_COOKIE_HTTPONLY)
        self.assertTrue(module.CSRF_COOKIE_SECURE)
        self.assertEqual(module.SESSION_COOKIE_SAMESITE, "Lax")
        self.assertEqual(module.CSRF_COOKIE_SAMESITE, "Lax")
        self.assertTrue(module.SECURE_SSL_REDIRECT)
        self.assertEqual(module.SECURE_PROXY_SSL_HEADER, ("HTTP_X_FORWARDED_PROTO", "https"))
        self.assertTrue(module.SECURE_CONTENT_TYPE_NOSNIFF)
        self.assertGreater(module.SECURE_HSTS_SECONDS, 0)
        self.assertTrue(module.SECURE_HSTS_INCLUDE_SUBDOMAINS)
        self.assertTrue(module.SECURE_HSTS_PRELOAD)
        self.assertEqual(module.SECURE_REFERRER_POLICY, "same-origin")
        self.assertEqual(module.X_FRAME_OPTIONS, "DENY")

    def test_production_settings_reject_debug_true(self):
        env = {**PRODUCTION_ENV, "DEBUG": "True"}

        with self.assertRaises(ImproperlyConfigured):
            self.import_production_settings(env)

    def test_production_settings_require_explicit_allowed_hosts(self):
        env = {**PRODUCTION_ENV, "ALLOWED_HOSTS": ""}

        with self.assertRaises(ImproperlyConfigured):
            self.import_production_settings(env)

    def test_production_settings_reject_wildcard_allowed_hosts(self):
        env = {**PRODUCTION_ENV, "ALLOWED_HOSTS": "*"}

        with self.assertRaises(ImproperlyConfigured):
            self.import_production_settings(env)

    def test_production_settings_require_explicit_cors_origins(self):
        env = {**PRODUCTION_ENV, "CORS_ALLOWED_ORIGINS": ""}

        with self.assertRaises(ImproperlyConfigured):
            self.import_production_settings(env)
