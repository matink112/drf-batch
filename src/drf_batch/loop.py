"""Process-wide event-loop bridge for async collectors (v0.2).

Django (and therefore DRF) dispatches viewsets synchronously, under WSGI
and ASGI alike, so ``BatchMixin`` needs a way to run a collector
coroutine from a synchronous thread. This module provides that bridge:

* One daemon thread runs one event loop for the process lifetime. The
  thread and loop are created **lazily on first use** — importing this
  module (or even running the sync-only pipeline) starts no thread.
* Uses are reference-counted: each ``session()`` holds one reference.
  The loop keeps running after the refcount drains (until
  :func:`shutdown`), so repeated requests do not churn threads.
* ``run_in_loop(coro)`` is the sync entry point used by
  :class:`~drf_batch.mixins.BatchMixin`; ``wait(coro)`` is the async
  entry point for genuinely async hosts (see the ASGI note in the
  README).

The coroutines run on the ``drf-batch-loop`` thread. Django ORM
connections are thread-local, so a synchronous ORM call made directly
from that thread keeps the loop thread's connection open for the
process lifetime; use :func:`to_thread` (or an async driver) instead —
it runs the call on a worker thread and closes that thread's database
connections afterwards, so pool threads never hold connections.
"""

from __future__ import annotations

import asyncio
import atexit
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from django.db import connections

from drf_batch.exceptions import BatchCollectionTimeout

_LOOP_THREAD_NAME = "drf-batch-loop"


class _LoopManager:
    """Owns the shared event loop and its worker thread (thread-safe)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self.in_flight = 0

    @contextmanager
    def session(self) -> Iterator[asyncio.AbstractEventLoop]:
        """Yield the shared loop, holding one reference for its duration."""
        loop = self._acquire()
        try:
            yield loop
        finally:
            self._release()

    def _acquire(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is None or self._loop.is_closed():
                self._loop = asyncio.new_event_loop()
                self._thread = threading.Thread(
                    target=self._loop.run_forever,
                    name=_LOOP_THREAD_NAME,
                    daemon=True,
                )
                self._thread.start()
            self.in_flight += 1
            return self._loop

    def _release(self) -> None:
        with self._lock:
            self.in_flight -= 1

    def shutdown(self, timeout: float | None = None) -> None:
        """Stop the loop thread and reset all state.

        No-op if no loop exists yet. Waits for in-flight sessions to
        drain (bounded by ``timeout`` when given), stops the loop, joins
        the thread, and closes the loop. A later ``session()`` re-creates
        a fresh loop and thread. Raises ``RuntimeError`` if the refcount
        does not drain or the thread does not stop in time.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                loop = self._loop
                drained = self.in_flight == 0
            if loop is None:
                return
            if drained:
                break
            if deadline is not None and time.monotonic() >= deadline:
                raise RuntimeError(
                    "drf-batch loop shutdown timed out with "
                    f"{self.in_flight} in-flight session(s)."
                )
            time.sleep(0.01)

        with self._lock:
            loop = self._loop
            thread = self._thread
            self._loop = None
            self._thread = None
            self.in_flight = 0

        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout)
            if thread.is_alive():
                raise RuntimeError(
                    "drf-batch loop thread did not stop in time; a "
                    "coroutine may be blocking the loop."
                )
        if loop is not None and not loop.is_closed():
            loop.close()


_manager = _LoopManager()
# Allowed: registering a callback at import time starts no thread.
atexit.register(_manager.shutdown)


@contextmanager
def session() -> Iterator[asyncio.AbstractEventLoop]:
    """Context manager for one use of the shared, refcounted loop."""
    with _manager.session() as loop:
        yield loop


def shutdown(timeout: float | None = None) -> None:
    """Stop the loop thread and release all state (also runs at exit)."""
    _manager.shutdown(timeout=timeout)


def _loop_thread() -> threading.Thread | None:
    """The ``drf-batch-loop`` thread, or ``None`` if it is not running."""
    return next((t for t in threading.enumerate() if t.name == _LOOP_THREAD_NAME), None)


def run_in_loop(coro: Any, timeout: float | None = None) -> Any:
    """Run ``coro`` on the shared loop and block until it resolves.

    ``coro`` is a coroutine (an ``async def collect`` result, or any
    awaitable a sync ``collect`` returns). Raises ``RuntimeError`` if the
    calling thread already has a running event loop — use
    :func:`wait` there instead. Raises
    :class:`~drf_batch.exceptions.BatchCollectionTimeout` if the result
    is not available within ``timeout`` seconds; any other exception
    raised by ``coro`` propagates unchanged.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError(
            "drf_batch.loop.run_in_loop() must be called from a thread "
            "without a running event loop; await the coroutine directly "
            "or use drf_batch.loop.wait()."
        ) from None

    with _manager.session() as loop:
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            # result() timing out means the task is still running: cancel
            # it so it stops doing work. (If the coroutine itself raised
            # TimeoutError the future is done and the original exception
            # is re-raised unchanged.)
            if not future.done():
                future.cancel()
                raise BatchCollectionTimeout() from None
            raise


async def wait(coro: Any, timeout: float | None = None) -> Any:
    """Await ``coro`` directly, for callers already inside a running loop.

    This is the await-directly path for genuinely async hosts: when a
    loop is running in the calling thread, await the coroutine instead of
    bridging it to the shared loop. Maps ``asyncio``'s timeout to
    :class:`~drf_batch.exceptions.BatchCollectionTimeout`.
    """
    try:
        return await asyncio.wait_for(coro, timeout)
    except TimeoutError:
        raise BatchCollectionTimeout() from None


def _call_then_close_connections(
    func: Callable[..., Any], /, *args: Any, **kwargs: Any
) -> Any:
    try:
        return func(*args, **kwargs)
    finally:
        # Only this (worker) thread's connections: Django's are thread-local.
        connections.close_all()


async def to_thread(func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """Run a blocking call (e.g. a Django ORM query) on the thread pool.

    Like :func:`asyncio.to_thread`, but the worker's database connections are
    closed when the call returns, so pooled threads never keep connections
    open. The call runs on its own connection: it does **not** see
    uncommitted writes of the request's transaction.
    """
    return await asyncio.to_thread(_call_then_close_connections, func, *args, **kwargs)
