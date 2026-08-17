from .base import *  # noqa
from config.env import env
from corsheaders.defaults import default_headers
from django.core.exceptions import ImproperlyConfigured
from urllib.parse import urlparse


PROVIDER_TRANSPORT_LOGGER = "cheatgame.financial_core.provider_transport"
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "provider_transport": {
            "format": "%(levelname)s %(name)s %(message)s",
        },
    },
    "handlers": {
        "provider_transport_console": {
            "class": "logging.StreamHandler",
            "formatter": "provider_transport",
            "level": "INFO",
        },
    },
    "loggers": {
        PROVIDER_TRANSPORT_LOGGER: {
            "handlers": ["provider_transport_console"],
            "level": "INFO",
            "propagate": False,
        },
    },
}


def _normalized_host(value):
    return str(value).strip().lower().rstrip(".")


def _validated_https_origins(name, values):
    normalized = []
    for raw_value in values:
        value = str(raw_value).strip().rstrip("/")
        parsed = urlparse(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.params
            or parsed.query
            or parsed.fragment
        ):
            raise ImproperlyConfigured(
                f"{name} must contain explicit HTTPS origins without paths."
            )
        normalized.append(value)
    return list(dict.fromkeys(normalized))

DEBUG = env.bool("DEBUG", default=False)
if DEBUG:
    raise ImproperlyConfigured("DEBUG must be False in production.")

CHEATSGAME_RUNTIME_ENVIRONMENT = env("CHEATSGAME_RUNTIME_ENVIRONMENT").strip().lower()
if CHEATSGAME_RUNTIME_ENVIRONMENT not in {"production", "staging"}:
    raise ImproperlyConfigured(
        "Production settings require an explicit production or staging runtime identity."
    )

FINANCIAL_CERTIFICATION_PROVIDER_ENABLED = env.bool(
    "FINANCIAL_CERTIFICATION_PROVIDER_ENABLED", default=False
)
FINANCIAL_CERTIFICATION_SECRET = env("FINANCIAL_CERTIFICATION_SECRET", default="")
FINANCIAL_CERTIFICATION_ALLOWED_HOSTS = env.list(
    "FINANCIAL_CERTIFICATION_ALLOWED_HOSTS", default=[]
)
FINANCIAL_ZARINPAL_ENABLED = env.bool("FINANCIAL_ZARINPAL_ENABLED", default=False)
ZARINPAL_MERCHANT_ID = env("ZARINPAL_MERCHANT_ID", default="")
ZARINPAL_SANDBOX = env.bool("ZARINPAL_SANDBOX", default=True)
ZARINPAL_REQUEST_URL = env("ZARINPAL_REQUEST_URL", default="")
ZARINPAL_VERIFY_URL = env("ZARINPAL_VERIFY_URL", default="")
ZARINPAL_STARTPAY_URL = env("ZARINPAL_STARTPAY_URL", default="")
FINANCIAL_ZARINPAL_ACCOUNT_KEY = env("FINANCIAL_ZARINPAL_ACCOUNT_KEY", default="")
FINANCIAL_ZARINPAL_OWNER_KEY = env("FINANCIAL_ZARINPAL_OWNER_KEY", default="")
FINANCIAL_PROVIDER_CALLBACK_BASE_URL = env(
    "FINANCIAL_PROVIDER_CALLBACK_BASE_URL", default=""
)
DIGITAL_PAYMENT_CUSTOMER_RETURN_BASE_URL = env(
    "DIGITAL_PAYMENT_CUSTOMER_RETURN_BASE_URL", default=""
)
IS_SEND_SMS = env.bool("IS_SEND_SMS", default=False)
VERIFY_PATTERN = env("VERIFY_PATTERN", default="")
FORGET_PASSWORD_PATTERN = env("FORGET_PASSWORD_PATTERN", default="")
PANEL_SMS_URL = env("PANEL_SMS_URL", default="")
PANEL_SMS_API_KEY = env("PANEL_SMS_API_KEY", default="")
PANEL_SMS_FROM = env("PANEL_SMS_FROM", default="")

PAYMENT_GATEWAY_PROVIDER = env("PAYMENT_GATEWAY_PROVIDER", default=PAYMENT_GATEWAY_PROVIDER)
if PAYMENT_GATEWAY_PROVIDER.strip().lower() in {"fake", "financial_certification"}:
    raise ImproperlyConfigured(
        "Fake and financial-certification payment providers are forbidden as public gateways."
    )
PAYMENT_FAKE_PROVIDER_ENABLED = False
if CHEATSGAME_RUNTIME_ENVIRONMENT == "production" and PAYMENT_GATEWAY_PROVIDER.strip().lower() != "zarinpal":
    raise ImproperlyConfigured("Production PAYMENT_GATEWAY_PROVIDER must be zarinpal.")
if CHEATSGAME_RUNTIME_ENVIRONMENT == "production" and not FINANCIAL_ZARINPAL_ENABLED:
    raise ImproperlyConfigured("FINANCIAL_ZARINPAL_ENABLED must be true in production.")
if FINANCIAL_ZARINPAL_ENABLED:
    if ZARINPAL_SANDBOX:
        raise ImproperlyConfigured("Financial Core Zarinpal cannot use sandbox mode in production.")
    if not ZARINPAL_MERCHANT_ID:
        raise ImproperlyConfigured("ZARINPAL_MERCHANT_ID is required for Financial Core Zarinpal.")
    if not FINANCIAL_PROVIDER_CALLBACK_BASE_URL:
        raise ImproperlyConfigured(
            "FINANCIAL_PROVIDER_CALLBACK_BASE_URL is required for Financial Core Zarinpal."
        )
    if not FINANCIAL_ZARINPAL_ACCOUNT_KEY or not FINANCIAL_ZARINPAL_OWNER_KEY:
        raise ImproperlyConfigured(
            "Financial Core Zarinpal account and owner identities are required."
        )
    provider_urls = [urlparse(value) for value in (ZARINPAL_REQUEST_URL, ZARINPAL_VERIFY_URL, ZARINPAL_STARTPAY_URL)]
    if any(value.scheme != "https" or value.hostname != "payment.zarinpal.com" for value in provider_urls):
        raise ImproperlyConfigured("Production Zarinpal endpoints must use payment.zarinpal.com over HTTPS.")
    if not DIGITAL_PAYMENT_CUSTOMER_RETURN_BASE_URL:
        raise ImproperlyConfigured(
            "DIGITAL_PAYMENT_CUSTOMER_RETURN_BASE_URL is required for Financial Core Zarinpal."
        )
    callback_origin = urlparse(FINANCIAL_PROVIDER_CALLBACK_BASE_URL)
    return_origin = urlparse(DIGITAL_PAYMENT_CUSTOMER_RETURN_BASE_URL)
    if CHEATSGAME_RUNTIME_ENVIRONMENT == "production" and (
        callback_origin.scheme != "https"
        or callback_origin.netloc != "api.cheatsg.ir"
        or callback_origin.path not in {"", "/"}
    ):
        raise ImproperlyConfigured(
            "Production Financial Core callback base must be https://api.cheatsg.ir."
        )
    if CHEATSGAME_RUNTIME_ENVIRONMENT == "production" and (
        return_origin.scheme != "https"
        or return_origin.netloc != "cheatsg.ir"
        or return_origin.path not in {"", "/"}
    ):
        raise ImproperlyConfigured(
            "Production customer return base must be https://cheatsg.ir."
        )

SECRET_KEY = env("SECRET_KEY")
if (
    len(SECRET_KEY) < 50
    or len(set(SECRET_KEY)) < 5
    or SECRET_KEY.startswith("django-insecure-")
):
    raise ImproperlyConfigured(
        "SECRET_KEY must be a high-entropy value containing at least 50 characters."
    )
JWT_SIGNING_KEY = env("JWT_SIGNING_KEY")
if (
    len(JWT_SIGNING_KEY) < 50
    or len(set(JWT_SIGNING_KEY)) < 5
    or JWT_SIGNING_KEY == SECRET_KEY
):
    raise ImproperlyConfigured(
        "JWT_SIGNING_KEY must be a distinct high-entropy secret containing at least 50 characters."
    )
SIMPLE_JWT = {**SIMPLE_JWT, "SIGNING_KEY": JWT_SIGNING_KEY}

DATABASE_URL = env("DATABASE_URL")
DATABASES = {"default": env.db_url_config(DATABASE_URL)}
DATABASES["default"]["ATOMIC_REQUESTS"] = True

ALLOWED_HOSTS = env.list('ALLOWED_HOSTS')
if not ALLOWED_HOSTS or "*" in ALLOWED_HOSTS:
    raise ImproperlyConfigured("ALLOWED_HOSTS must be explicitly set in production.")
ALLOWED_HOSTS = list(dict.fromkeys(_normalized_host(host) for host in ALLOWED_HOSTS))
if CHEATSGAME_RUNTIME_ENVIRONMENT == "production":
    forbidden_hosts = {"127.0.0.1", "localhost", "[::1]"}
    if forbidden_hosts.intersection(ALLOWED_HOSTS) or any(
        "staging" in host for host in ALLOWED_HOSTS
    ):
        raise ImproperlyConfigured(
            "Production ALLOWED_HOSTS cannot contain loopback or staging hosts."
        )
    if set(ALLOWED_HOSTS) != {"api.cheatsg.ir"}:
        raise ImproperlyConfigured(
            "Production ALLOWED_HOSTS must contain only api.cheatsg.ir."
        )

if CHEATSGAME_RUNTIME_ENVIRONMENT == "production" and (
    FINANCIAL_CERTIFICATION_PROVIDER_ENABLED
    or FINANCIAL_CERTIFICATION_SECRET
    or FINANCIAL_CERTIFICATION_ALLOWED_HOSTS
):
    raise ImproperlyConfigured(
        "Financial Certification configuration is forbidden in production."
    )
if FINANCIAL_CERTIFICATION_PROVIDER_ENABLED:
    if CHEATSGAME_RUNTIME_ENVIRONMENT != "staging":
        raise ImproperlyConfigured(
            "The Financial Certification provider is forbidden outside staging."
        )
    if len(FINANCIAL_CERTIFICATION_SECRET) < 32:
        raise ImproperlyConfigured(
            "FINANCIAL_CERTIFICATION_SECRET must contain at least 32 characters."
        )
    certification_hosts = {
        str(host).strip().lower().rstrip(".")
        for host in FINANCIAL_CERTIFICATION_ALLOWED_HOSTS
        if str(host).strip()
    }
    configured_hosts = {str(host).strip().lower().rstrip(".") for host in ALLOWED_HOSTS}
    if (
        not certification_hosts
        or not certification_hosts.issubset(configured_hosts)
        or any("staging" not in host for host in certification_hosts)
    ):
        raise ImproperlyConfigured(
            "Financial Certification hosts must be explicit staging ALLOWED_HOSTS."
        )

CORS_ALLOW_ALL_ORIGINS = False
CORS_ALLOW_HEADERS = (*default_headers, "idempotency-key")
CORS_ALLOWED_ORIGINS = _validated_https_origins(
    "CORS_ALLOWED_ORIGINS",
    env.list(
    'CORS_ALLOWED_ORIGINS',
    default=env.list('CORS_ORIGIN_WHITELIST', default=[]),
    ),
)
if not CORS_ALLOWED_ORIGINS:
    raise ImproperlyConfigured("CORS_ALLOWED_ORIGINS must be explicitly set in production.")
CORS_ORIGIN_WHITELIST = CORS_ALLOWED_ORIGINS

CSRF_TRUSTED_ORIGINS = _validated_https_origins(
    "CSRF_TRUSTED_ORIGINS",
    env.list("CSRF_TRUSTED_ORIGINS", default=[]),
)
if not CSRF_TRUSTED_ORIGINS:
    raise ImproperlyConfigured("CSRF_TRUSTED_ORIGINS must be explicitly set.")

if CHEATSGAME_RUNTIME_ENVIRONMENT == "production":
    expected_origins = {"https://cheatsg.ir", "https://admin.cheatsg.ir"}
    if set(CORS_ALLOWED_ORIGINS) != expected_origins:
        raise ImproperlyConfigured(
            "Production CORS origins must exactly match Storefront and Admin."
        )
    if set(CSRF_TRUSTED_ORIGINS) != expected_origins:
        raise ImproperlyConfigured(
            "Production CSRF trusted origins must exactly match Storefront and Admin."
        )

SESSION_COOKIE_SECURE = env.bool('SESSION_COOKIE_SECURE', default=True)
SESSION_COOKIE_HTTPONLY = env.bool('SESSION_COOKIE_HTTPONLY', default=True)
SESSION_COOKIE_SAMESITE = env('SESSION_COOKIE_SAMESITE', default='Lax')
CSRF_COOKIE_SECURE = env.bool('CSRF_COOKIE_SECURE', default=True)
CSRF_COOKIE_SAMESITE = env('CSRF_COOKIE_SAMESITE', default='Lax')
if not SESSION_COOKIE_SECURE or not SESSION_COOKIE_HTTPONLY or not CSRF_COOKIE_SECURE:
    raise ImproperlyConfigured("Production cookies must retain Secure and HttpOnly policy.")
if SESSION_COOKIE_SAMESITE not in {"Lax", "Strict"} or CSRF_COOKIE_SAMESITE not in {"Lax", "Strict"}:
    raise ImproperlyConfigured("Production cookies require Lax or Strict SameSite policy.")

# https://docs.djangoproject.com/en/dev/ref/settings/#secure-proxy-ssl-header
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
# https://docs.djangoproject.com/en/dev/ref/settings/#secure-ssl-redirect
SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=True)
SECURE_REDIRECT_EXEMPT = list(dict.fromkeys([
    *globals().get("SECURE_REDIRECT_EXEMPT", []),
    r"^health/live/$",
    r"^health/ready/$",
]))
SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=31536000)
SECURE_HSTS_INCLUDE_SUBDOMAINS = env.bool("SECURE_HSTS_INCLUDE_SUBDOMAINS", default=True)
SECURE_HSTS_PRELOAD = env.bool("SECURE_HSTS_PRELOAD", default=True)
SECURE_REFERRER_POLICY = env("SECURE_REFERRER_POLICY", default="same-origin")
X_FRAME_OPTIONS = "DENY"
# https://docs.djangoproject.com/en/dev/ref/middleware/#x-content-type-options-nosniff
SECURE_CONTENT_TYPE_NOSNIFF = env.bool(
    "SECURE_CONTENT_TYPE_NOSNIFF", default=True
)
if not SECURE_SSL_REDIRECT:
    raise ImproperlyConfigured("SECURE_SSL_REDIRECT must remain enabled in production.")
if (
    SECURE_HSTS_SECONDS < 31536000
    or not SECURE_HSTS_INCLUDE_SUBDOMAINS
    or not SECURE_HSTS_PRELOAD
):
    raise ImproperlyConfigured(
        "Production HSTS must cover all subdomains for at least one year with preload enabled."
    )
if not SECURE_CONTENT_TYPE_NOSNIFF:
    raise ImproperlyConfigured("SECURE_CONTENT_TYPE_NOSNIFF must remain enabled in production.")
if SECURE_REFERRER_POLICY != "same-origin":
    raise ImproperlyConfigured("Production referrer policy must be same-origin.")
AWS_S3_ENDPOINT_URL = env('AWS_S3_ENDPOINT_URL')
AWS_ACCESS_KEY_ID=  env('AWS_ACCESS_KEY_ID')
AWS_SECRET_ACCESS_KEY= env('AWS_SECRET_ACCESS_KEY')
AWS_STORAGE_BUCKET_NAME = env('AWS_STORAGE_BUCKET_NAME')
AWS_S3_REGION_NAME = env('AWS_S3_REGION_NAME')
AWS_S3_CUSTOM_DOMAIN = env('AWS_S3_CUSTOM_DOMAIN', default=None)
AWS_STORAGE_ENVIRONMENT = env("AWS_STORAGE_ENVIRONMENT")
AWS_S3_FILE_OVERWRITE = False
AWS_S3_OBJECT_PARAMETERS = {
    "CacheControl": env(
        "AWS_S3_MEDIA_CACHE_CONTROL",
        default="public, max-age=31536000, immutable",
    )
}
STORAGES = {
    "default": {
        "BACKEND": "storages.backends.s3.S3Storage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

if CHEATSGAME_RUNTIME_ENVIRONMENT == "production":
    if AWS_STORAGE_ENVIRONMENT.strip().lower() != "production":
        raise ImproperlyConfigured(
            "Production media must use a storage identity explicitly marked production."
        )
    if any(marker in AWS_STORAGE_BUCKET_NAME.lower() for marker in ("staging", "test", "qa")):
        raise ImproperlyConfigured("Production cannot use a staging/test media bucket.")
    if _normalized_host(AWS_S3_CUSTOM_DOMAIN) != "cdn.cheatsg.ir":
        raise ImproperlyConfigured(
            "Production AWS_S3_CUSTOM_DOMAIN must be cdn.cheatsg.ir."
        )

if CHEATSGAME_RUNTIME_ENVIRONMENT == "production":
    if not IS_SEND_SMS:
        raise ImproperlyConfigured("IS_SEND_SMS must be true in production.")
    required_sms_settings = {
        "PANEL_SMS_URL": PANEL_SMS_URL,
        "PANEL_SMS_API_KEY": PANEL_SMS_API_KEY,
        "PANEL_SMS_FROM": PANEL_SMS_FROM,
        "VERIFY_PATTERN": VERIFY_PATTERN,
        "FORGET_PASSWORD_PATTERN": FORGET_PASSWORD_PATTERN,
    }
    missing_sms = [name for name, value in required_sms_settings.items() if not value]
    if missing_sms:
        raise ImproperlyConfigured(
            "Production SMS configuration is incomplete: " + ", ".join(sorted(missing_sms))
        )
    sms_url = urlparse(PANEL_SMS_URL)
    if sms_url.scheme != "https" or not sms_url.hostname:
        raise ImproperlyConfigured("Production PANEL_SMS_URL must be an absolute HTTPS URL.")
