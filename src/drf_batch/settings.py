"""Project settings for ``drf_batch``.

Configure via a ``DRF_BATCH`` dictionary in your Django settings:

    DRF_BATCH = {"BATCH_MAX_SIZE": 1000}

Every key has a default, so the setting is optional. A view attribute
(``batch_max_size``) overrides the setting per view.
"""

from __future__ import annotations

import collections.abc
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

DEFAULTS: dict[str, Any] = {
    "BATCH_MAX_SIZE": 1000,
    "BULK_SIZE": 1000,
    "COLLECT_TIMEOUT": 10.0,
    # "auto": the ASGI server's loop when there is one, else thread mode;
    # "thread": always thread mode (see drf_batch.loop).
    "EVENT_LOOP": "auto",
    # Thread mode: dotted path to a callable returning a loop the
    # application already runs in its own thread; None = built-in thread.
    "THREAD_LOOP": None,
}


def get_setting(name: str) -> Any:
    """Return ``DRF_BATCH[name]`` if set, otherwise its default."""
    user_settings: Any = getattr(settings, "DRF_BATCH", None) or {}
    if not isinstance(user_settings, collections.abc.Mapping):
        raise ImproperlyConfigured(
            "DRF_BATCH must be a dictionary of settings, "
            f"got {type(user_settings).__name__}."
        )
    if name in user_settings:
        return user_settings[name]
    return DEFAULTS[name]
