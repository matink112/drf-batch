"""Optional Django app config.

Adding ``"drf_batch"`` to ``INSTALLED_APPS`` is **not** required to use the
package. It only registers integrations whose dependency is installed —
currently the drf-spectacular schema extensions
(:mod:`drf_batch.contrib.spectacular`).
"""

from __future__ import annotations

import importlib.util

from django.apps import AppConfig


class DrfBatchConfig(AppConfig):
    name = "drf_batch"
    verbose_name = "drf-batch"

    def ready(self) -> None:
        if importlib.util.find_spec("drf_spectacular") is not None:
            import drf_batch.contrib.spectacular  # noqa: F401  (registers)
