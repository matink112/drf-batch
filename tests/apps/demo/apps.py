"""Demo Django app: the running example shared by the tests and the README."""

from __future__ import annotations

from django.apps import AppConfig


class DemoConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "tests.apps.demo"
