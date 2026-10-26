"""Guard against a CI matrix that silently tests the locked versions.

CI exports the Django/DRF specifiers it means to test; the installed versions
must satisfy them (a ``uv run`` re-sync to ``uv.lock`` would not).
"""

from __future__ import annotations

import os

import django
import pytest
import rest_framework
from packaging.specifiers import SpecifierSet


@pytest.mark.parametrize(
    ("env_var", "version"),
    [
        ("DRF_BATCH_EXPECT_DJANGO", django.__version__),
        ("DRF_BATCH_EXPECT_DRF", rest_framework.__version__),
    ],
)
def test_installed_version_matches_ci_matrix(env_var, version):
    spec = os.environ.get(env_var)
    if not spec:
        pytest.skip(f"{env_var} not set (local run)")
    assert version in SpecifierSet(spec), f"{version} does not satisfy {spec}"
