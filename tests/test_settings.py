from __future__ import annotations

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from drf_batch.settings import get_setting


def test_defaults_are_returned_when_drf_batch_is_unset():
    assert get_setting("BATCH_MAX_SIZE") == 1000
    assert get_setting("BULK_SIZE") == 1000
    assert get_setting("COLLECT_TIMEOUT") == 10.0


def test_user_setting_overrides_the_default():
    with override_settings(DRF_BATCH={"BATCH_MAX_SIZE": 42}):
        assert get_setting("BATCH_MAX_SIZE") == 42
        assert get_setting("BULK_SIZE") == 1000


def test_drf_batch_must_be_a_mapping():
    with (
        override_settings(DRF_BATCH="nope"),
        pytest.raises(ImproperlyConfigured, match="must be a dictionary"),
    ):
        get_setting("BATCH_MAX_SIZE")
