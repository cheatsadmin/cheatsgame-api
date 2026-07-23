import os
from config.env import env, BASE_DIR

env.read_env(os.path.join(BASE_DIR, ".env"))
IS_SEND_SMS = env.bool("IS_SEND_SMS", default=False)
VERIFY_PATTERN = env("VERIFY_PATTERN", default=None)
FORGET_PASSWORD_PATTERN = env("FORGET_PASSWORD_PATTERN", default=None)
PANEL_SMS_URL = env("PANEL_SMS_URL", default=None)
PANEL_SMS_USER = env("PANEL_SMS_USER", default=None)
PANEL_SMS_PASS = env("PANEL_SMS_PASS", default=None)
PANEL_SMS_API_KEY = env("PANEL_SMS_API_KEY", default=None)
PANEL_SMS_FROM = env("PANEL_SMS_FROM", default=None)
PANEL_SMS_PATTERN_VARIABLE = env("PANEL_SMS_PATTERN_VARIABLE", default=None)
PANEL_SMS_TIMEOUT_SECONDS = env.int("PANEL_SMS_TIMEOUT_SECONDS", default=15)
COMMERCE_CHECKOUT_V2_ENABLED = env.bool("COMMERCE_CHECKOUT_V2_ENABLED", default=False)
COMMERCE_CHECKOUT_TTL_SECONDS = env.int("COMMERCE_CHECKOUT_TTL_SECONDS", default=1800)
COMMERCE_CHECKOUT_MAXIMUM_LIFETIME_SECONDS = env.int(
    "COMMERCE_CHECKOUT_MAXIMUM_LIFETIME_SECONDS", default=7200
)
PAYMENT_GATEWAY_PROVIDER = env("PAYMENT_GATEWAY_PROVIDER", default="fake")
PAYMENT_FAKE_PROVIDER_ENABLED = env.bool("PAYMENT_FAKE_PROVIDER_ENABLED", default=True)
PAYMENT_SUCCESS_REDIRECT_URL = env("PAYMENT_SUCCESS_REDIRECT_URL", default="")
PAYMENT_AMOUNT_UNIT = env("PAYMENT_AMOUNT_UNIT", default="IRT")
ZARINPAL_MERCHANT_ID = env("ZARINPAL_MERCHANT_ID", default="")
ZARINPAL_SANDBOX = env.bool("ZARINPAL_SANDBOX", default=True)
ZARINPAL_REQUEST_URL = env(
    "ZARINPAL_REQUEST_URL",
    default="https://sandbox.zarinpal.com/pg/v4/payment/request.json",
)
ZARINPAL_VERIFY_URL = env(
    "ZARINPAL_VERIFY_URL",
    default="https://sandbox.zarinpal.com/pg/v4/payment/verify.json",
)
ZARINPAL_STARTPAY_URL = env(
    "ZARINPAL_STARTPAY_URL",
    default="https://sandbox.zarinpal.com/pg/StartPay/{authority}",
)
BLOG_AI_PROVIDER = env("BLOG_AI_PROVIDER", default="openai_compatible")
BLOG_AI_MODEL = env("BLOG_AI_MODEL", default="gpt-4o-mini")
BLOG_AI_API_KEY = env("BLOG_AI_API_KEY", default="")
BLOG_AI_API_URL = env("BLOG_AI_API_URL", default="https://api.openai.com/v1/chat/completions")
BLOG_AI_MOCK_ENABLED = env.bool("BLOG_AI_MOCK_ENABLED", default=False)
BLOG_AI_TIMEOUT_SECONDS = env.int("BLOG_AI_TIMEOUT_SECONDS", default=30)
# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = '=ug_ucl@yi6^mrcjyz%(u0%&g2adt#bz3@yos%#@*t#t!ypx=a'

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = True

ALLOWED_HOSTS = env.list('ALLOWED_HOSTS', default=['*'])

CORS_ALLOW_ALL_ORIGINS = True
CORS_ALLOW_CREDENTIALS = True

# Application definition
LOCAL_APPS = [
    'cheatgame.core.apps.CoreConfig',
    'cheatgame.common.apps.CommonConfig',
    'cheatgame.users.apps.UsersConfig',
    'cheatgame.authentication.apps.AuthenticationConfig',
    'cheatgame.product.apps.ProductConfig',
    "cheatgame.general.apps.GeneralConfig",
    "cheatgame.shop.apps.ShopConfig",
    "cheatgame.digital_products.apps.DigitalProductsConfig",
    "cheatgame.financial_core.apps.FinancialCoreConfig",
    "cheatgame.issue.apps.IssueConfig",
]

THIRD_PARTY_APPS = [
    'rest_framework',
    'django_filters',
    'corsheaders',
    'drf_spectacular',
    'django_extensions',
    'storages',
    'mptt',
]

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    # http://whitenoise.evans.io/en/stable/django.html#using-whitenoise-in-development
    'whitenoise.runserver_nostatic',
    'django.contrib.staticfiles',
    *THIRD_PARTY_APPS,
    *LOCAL_APPS,
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.middleware.gzip.GZipMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

# Database
# https://docs.djangoproject.com/en/3.0/ref/settings/#databases

DATABASES = {
    'default': env.db('DATABASE_URL', default='psql://postgres:hamid14529@127.0.0.1:5432/cheatgame'),
}
DATABASES['default']['ATOMIC_REQUESTS'] = True

if os.environ.get('GITHUB_WORKFLOW'):
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': 'github_actions',
            'USER': 'user',
            'PASSWORD': 'password',
            'HOST': '127.0.0.1',
            'PORT': '5432',
        }
    }

# Password validation
# https://docs.djangoproject.com/en/3.0/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]
AUTH_USER_MODEL = 'users.BaseUser'

# Internationalization
# https://docs.djangoproject.com/en/3.0/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_L10N = True

USE_TZ = True

# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/3.0/howto/static-files/

STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')
STATIC_URL = '/static/'
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

REST_FRAMEWORK = {
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
    'EXCEPTION_HANDLER': 'cheatgame.api.exception_handlers.drf_default_with_modifications_exception_handler',
    # 'EXCEPTION_HANDLER': 'cheatgame.api.exception_handlers.hacksoft_proposed_exception_handler',
    'DEFAULT_FILTER_BACKENDS': (
        'django_filters.rest_framework.DjangoFilterBackend',
    ),
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ],
    'DEFAULT_THROTTLE_RATES': {
        'login': env('DRF_THROTTLE_LOGIN', default='10/min'),
        'register': env('DRF_THROTTLE_REGISTER', default='5/min'),
        'otp_request': env('DRF_THROTTLE_OTP_REQUEST', default='3/min'),
        'otp_verify': env('DRF_THROTTLE_OTP_VERIFY', default='10/min'),
        'password_reset_request': env('DRF_THROTTLE_PASSWORD_RESET_REQUEST', default='3/min'),
        'password_reset_confirm': env('DRF_THROTTLE_PASSWORD_RESET_CONFIRM', default='10/min'),
        'checkout_write': env('DRF_THROTTLE_CHECKOUT_WRITE', default='60/min'),
        'payment_write': env('DRF_THROTTLE_PAYMENT_WRITE', default='60/min'),
        'review_submit': env('DRF_THROTTLE_REVIEW_SUBMIT', default='10/min'),
        # C2B1's callback view is intentionally not URL-wired. Keep the fixed
        # boundary configured so direct validation cannot accidentally run
        # without a conservative abuse-control policy.
        'financial_callback': env('DRF_THROTTLE_FINANCIAL_CALLBACK', default='120/min'),
    },
}


# APP_DOMAIN = env("APP_DOMAIN", default="http://localhost:8000")

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

DEFAULT_FILE_STORAGE = 'django.core.files.storage.FileSystemStorage'
# DEFAULT_FILE_STORAGE = 'storages.backends.3sbot3.3SBoto3Storage'
# from config.settings.cors import *  # noqa
from config.settings.jwt import *  # noqa
# from config.settings.sessions import *  # noqa
from config.settings.swagger import *  # noqa
# from config.settings.sentry import *  # noqa
# from config.settings.email_sending import *  # noqa
