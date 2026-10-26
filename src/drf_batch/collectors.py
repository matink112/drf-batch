"""The collector hook: full-replacement collection for a batch request.

Auto collection is the default (v0.3): the serializer's cached fields are
fetched by the list serializer itself, in topological order. A
:class:`BatchCollector` is a **full-replacement hook**: when
``batch_collector_class`` is set, its ``collect(keys, context)`` runs
*instead of* auto collection and must return **every** map the fields need
(a missing map means every row gets a miss error for it).

``collect`` receives the coerced key sets derived from the serializer's
cached fields (``{map: set}``; keys are the type-coerced lookup keys, never
raw rows), so a collector can no longer crash on malformed data.

``collect`` may be **synchronous** (the default; runs inline on the
request thread, zero overhead) or **asynchronous** (``async def collect``;
auto-detected and bridged to a process-wide event loop, with a
``COLLECT_TIMEOUT`` guard). An async ``collect`` runs on the
``drf-batch-loop`` thread, so Django ORM access inside it should go through
``self.to_thread(...)`` (a worker thread whose connection is closed after
the call) or an async driver; direct synchronous ORM calls from the loop
thread work but keep that connection open for the process lifetime.

Update/destroy targets are **not** the collector's job: the pipeline
resolves them from the view's (scoped) queryset before ``collect`` runs.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, TypedDict

from rest_framework.request import Request

from drf_batch import loop


class BatchContext(TypedDict, total=False):
    """The serializer context handed to :meth:`BatchCollector.collect`.

    In practice this is ``view.get_serializer_context()`` (``request``,
    ``view`` and ``format`` keys), plus ``collected``: the maps returned by
    ``collect`` are injected back into it before the rows are validated.
    """

    request: Request
    view: Any
    format: Any | None
    collected: dict[str, Any]


class BatchCollector:
    """Base class for a full-replacement collection hook.

    Attributes:
        collect_timeout: per-collector timeout in seconds for an async
            ``collect``; ``None`` (default) falls back to the
            ``COLLECT_TIMEOUT`` setting. Ignored for sync collectors.
    """

    collect_timeout: float | None = None

    def collect(
        self, keys: dict[str, set[Any]], context: BatchContext
    ) -> dict[str, Any] | Awaitable[dict[str, Any]]:
        """Resolve the coerced key sets into the maps validation will use.

        ``keys`` is the ``{map_name: {coerced_key, ...}}`` derived from the
        serializer's cached fields. Return a ``{map_name: {lookup_key:
        value}}`` mapping — **every** map the serializer's cached fields
        need (a missing map means every row gets a miss error for it). The
        result is exposed to serializers as ``serializer.collected`` and as
        ``context["collected"]``.

        May be overridden as ``async def collect(...)`` for concurrent
        lookups (the awaitable is then bridged to the shared loop); see the
        module docstring for the threading model.
        """
        raise NotImplementedError

    async def to_thread(
        self, func: Callable[..., Any], /, *args: Any, **kwargs: Any
    ) -> Any:
        """Run ``func(*args, **kwargs)`` on the default thread pool.

        Use inside an async ``collect`` for blocking calls (notably Django
        ORM queries). The call runs on its own connection, which is closed
        afterwards (see :func:`drf_batch.loop.to_thread`).
        """
        return await loop.to_thread(func, *args, **kwargs)

    def taskgroup(self) -> asyncio.TaskGroup:
        """An ``asyncio.TaskGroup`` for structured concurrency inside ``collect``."""
        return asyncio.TaskGroup()
