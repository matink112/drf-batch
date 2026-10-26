"""Standalone test settings for the demo app (in-memory SQLite)."""

from __future__ import annotations

import os

SECRET_KEY = "drf-batch-test-key-not-for-production"
DEBUG = True
ALLOWED_HOSTS = ["*"]
ROOT_URLCONF = "tests.apps.demo.urls"

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.staticfiles",
    "rest_framework",
    "drf_spectacular",
    "drf_batch",
    "tests.apps.demo",
]

STATIC_URL = "/static/"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
    }
]

# Shared-cache in-memory SQLite: ORM queries from the drf-batch loop thread
# (and the thread pool) connect to the same in-memory database as the test
# process. Under the test runner Django swaps in its own (already
# shared-cache) test database name, so this only shapes non-test usage.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": "file::memory:?cache=shared",
        "OPTIONS": {"uri": True},
    }
}

# CI's PostgreSQL job: DRF_BATCH_TEST_DB=postgres (+ the standard PG* env vars).
if os.environ.get("DRF_BATCH_TEST_DB") == "postgres":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("PGDATABASE", "drf_batch"),
            "USER": os.environ.get("PGUSER", "postgres"),
            "PASSWORD": os.environ.get("PGPASSWORD", "postgres"),
            "HOST": os.environ.get("PGHOST", "localhost"),
            "PORT": os.environ.get("PGPORT", "5432"),
        }
    }

REST_FRAMEWORK = {
    "TEST_REQUEST_DEFAULT_FORMAT": "json",
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
