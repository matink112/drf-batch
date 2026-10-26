"""Small internal helpers shared by the pipeline modules."""

from __future__ import annotations

from typing import Any

from django.db import models
from rest_framework.settings import api_settings


def default_manager(model: type[models.Model]) -> Any:
    """``model._default_manager`` (never assume a manager named ``objects``)."""
    return model._default_manager


def non_field_key() -> str:
    """DRF's key for row-level (non-field) errors, read at call time."""
    return str(api_settings.NON_FIELD_ERRORS_KEY)
