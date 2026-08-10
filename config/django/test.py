from .base import *  # noqa

# Based on https://www.hacksoft.io/blog/optimize-django-build-to-run-faster-on-github-actions

DEBUG = False
PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']

CELERY_BROKER_BACKEND = "memory"
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    }
}

TEST_DATABASE_URL = env("DATABASE_URL", default="")
if TEST_DATABASE_URL:
    DATABASES = {"default": env.db_url_config(TEST_DATABASE_URL)}
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": "db.sqlite3",
        }
    }
DATABASES["default"]["ATOMIC_REQUESTS"] = True
