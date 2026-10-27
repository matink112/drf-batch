"""Event-loop bridge for collection.

Django (and therefore DRF) dispatches viewsets synchronously, under WSGI
and ASGI alike, so the collection pass needs a way to run its coroutine
from a synchronous thread. ``run_in_loop(coro)`` submits it to a loop
running in **another** thread and blocks until it resolves. The loop is
chosen per call (``EVENT_LOOP`` setting):

* ``"auto"`` (default): under ASGI, the **server's own loop** (the sync view
  runs in an ``asgiref`` worker thread, which knows the loop that spawned
  it), so collection shares the loop the app's async clients live on.
  Anywhere else, the thread-mode loop below.
* ``"thread"``: always the thread-mode loop:

  1. a loop the application already runs in its own thread, registered
     with :func:`use_loop` or the ``THREAD_LOOP`` setting (a dotted path to
     a zero-argument callable returning it);
  2. otherwise the built-in ``drf-batch-loop`` daemon thread: one event
     loop for the process, created **lazily on first use** (importing this
     module starts no thread), reference-counted per ``session()``, kept
     running until :func:`shutdown` (also at exit and recreated after
     ``fork()``).

A loop drf-batch does not own (ASGI or user) is never stopped by
:func:`shutdown`. Every loop must be running in a thread other than the
caller's.

Django ORM connections are thread-local. :func:`to_thread` runs a blocking
call (notably an ORM query) on a worker thread and applies Django's
request-boundary connection handling around it
(``close_old_connections()``: ``CONN_MAX_AGE`` and health checks apply), so
worker threads reuse connections only as long as the settings allow.
"""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import contextvars
import os
import queue
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from functools import cache
from typing import Any, cast

from django.core.exceptions import ImproperlyConfigured
from django.db import close_old_connections
from django.utils.module_loading import import_string

from drf_batch.exceptions import BatchCollectionTimeout
from drf_batch.settings import get_setting

_LOOP_THREAD_NAME = "drf-batch-loop"
_EVENT_LOOP_MODES = ("auto", "thread")
_EXIT_SHUTDOWN_TIMEOUT = 5.0


class _LoopManager:
    """Owns the built-in event loop and its worker thread (thread-safe).

    Also holds the loop registered with :func:`use_loop`, which it never
    starts or stops.
    """

    def __init__(self) -> None:
        self._reset()

    def _reset(self) -> None:
        # Also the fork handler: the child inherits the state but not the
        # loop thread, so it starts over (a fresh lock, too, since the
        # parent may have held it while forking).
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self.in_flight = 0
        self.user_loop: asyncio.AbstractEventLoop | None = None

    @contextmanager
    def session(self) -> Iterator[asyncio.AbstractEventLoop]:
        """Yield the built-in loop, holding one reference for its duration."""
        loop = self._acquire()
        try:
            yield loop
        finally:
            self._release()

    def _acquire(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if (
                self._loop is None
                or self._loop.is_closed()
                or self._thread is None
                or not self._thread.is_alive()
            ):
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

    def set_user_loop(self, loop: asyncio.AbstractEventLoop | None) -> None:
        with self._lock:
            self.user_loop = loop

    def shutdown(self, timeout: float | None = None) -> None:
        """Stop the built-in loop thread and reset its state.

        No-op if no loop exists yet. Waits for in-flight sessions to
        drain (bounded by ``timeout`` when given), stops the loop, joins
        the thread, and closes the loop. A later ``session()`` re-creates
        a fresh loop and thread. Raises ``RuntimeError`` if the refcount
        does not drain or the thread does not stop in time. A loop
        registered with :func:`use_loop` is left running.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                loop = self._loop
                if loop is None:
                    return
                in_flight = self.in_flight
                if in_flight == 0:
                    # Detach under the same lock as the drain check, so no
                    # session can pick up the loop that is about to stop.
                    thread = self._thread
                    self._loop = None
                    self._thread = None
                    break
            if deadline is not None and time.monotonic() >= deadline:
                raise RuntimeError(
                    "drf-batch loop shutdown timed out with "
                    f"{in_flight} in-flight session(s)."
                )
            time.sleep(0.01)

        if not loop.is_closed():
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout)
            if thread.is_alive():
                raise RuntimeError(
                    "drf-batch loop thread did not stop in time; a "
                    "coroutine may be blocking the loop."
                )
        if not loop.is_closed():
            loop.close()


_manager = _LoopManager()


def _shutdown_at_exit() -> None:
    # Bounded, and quiet: a coroutine blocking the loop must not hang or
    # crash interpreter exit (the thread is a daemon and dies with it).
    with suppress(RuntimeError):
        _manager.shutdown(timeout=_EXIT_SHUTDOWN_TIMEOUT)


# Allowed: registering callbacks at import time starts no thread.
atexit.register(_shutdown_at_exit)
if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_manager._reset)


@contextmanager
def session() -> Iterator[asyncio.AbstractEventLoop]:
    """Context manager for one use of the built-in, refcounted loop."""
    with _manager.session() as loop:
        yield loop


def shutdown(timeout: float | None = None) -> None:
    """Stop the built-in loop thread and release its state (also runs at exit)."""
    _manager.shutdown(timeout=timeout)


def use_loop(loop: asyncio.AbstractEventLoop | None) -> None:
    """Run thread-mode collection on ``loop``, already running in your thread.

    Use it when the application owns a loop (and the async clients created
    on it) in a thread of its own. It takes precedence over the
    ``THREAD_LOOP`` setting; ``None`` restores the built-in
    ``drf-batch-loop`` thread. drf-batch never starts, stops or closes
    ``loop``. Under ASGI with ``EVENT_LOOP = "auto"`` the server's loop is
    still preferred.
    """
    if loop is not None and not isinstance(loop, asyncio.AbstractEventLoop):
        raise TypeError(
            f"use_loop() expects an asyncio event loop or None, "
            f"got {type(loop).__name__}."
        )
    _manager.set_user_loop(loop)


def _loop_thread() -> threading.Thread | None:
    """The ``drf-batch-loop`` thread, or ``None`` if it is not running."""
    return next((t for t in threading.enumerate() if t.name == _LOOP_THREAD_NAME), None)


def _asgi_main_loop() -> asyncio.AbstractEventLoop | None:
    """The ASGI server's loop when this thread runs a sync view under ASGI.

    ``asgiref``'s ``SyncToAsync`` records the loop that spawned the worker
    thread (the same check ``AsyncToSync`` uses to schedule back onto it).
    Any missing piece means "not under ASGI".
    """
    try:
        from asgiref.sync import SyncToAsync
    except ImportError:  # pragma: no cover - asgiref ships with Django
        return None
    local = SyncToAsync.threadlocal
    if getattr(local, "main_event_loop_pid", None) != os.getpid():
        return None
    loop = getattr(local, "main_event_loop", None)
    if (
        isinstance(loop, asyncio.AbstractEventLoop)
        and loop.is_running()
        and not loop.is_closed()
    ):
        return loop
    return None


@cache
def _import_thread_loop_factory(path: str) -> Callable[[], Any]:
    try:
        factory = import_string(path)
    except ImportError as exc:
        raise ImproperlyConfigured(
            f"DRF_BATCH THREAD_LOOP: cannot import '{path}': {exc}"
        ) from exc
    if not callable(factory):
        raise ImproperlyConfigured(f"DRF_BATCH THREAD_LOOP: '{path}' is not callable.")
    return cast("Callable[[], Any]", factory)


def _thread_loop_from_setting() -> asyncio.AbstractEventLoop | None:
    path = get_setting("THREAD_LOOP")
    if path is None:
        return None
    if not isinstance(path, str):
        raise ImproperlyConfigured(
            "DRF_BATCH THREAD_LOOP must be a dotted path to a callable "
            f"returning an event loop, got {type(path).__name__}."
        )
    loop = _import_thread_loop_factory(path)()
    if not isinstance(loop, asyncio.AbstractEventLoop):
        raise ImproperlyConfigured(
            f"DRF_BATCH THREAD_LOOP: '{path}' returned "
            f"{type(loop).__name__}, not an event loop."
        )
    return loop


def _external_loop() -> asyncio.AbstractEventLoop | None:
    """The loop to submit to, or ``None`` for the built-in thread."""
    mode = get_setting("EVENT_LOOP")
    if mode not in _EVENT_LOOP_MODES:
        raise ImproperlyConfigured(
            f"DRF_BATCH EVENT_LOOP must be one of {', '.join(_EVENT_LOOP_MODES)}; "
            f"got {mode!r}."
        )
    if mode == "auto":
        asgi = _asgi_main_loop()
        if asgi is not None:
            return asgi
    loop = _manager.user_loop
    source = "use_loop()"
    if loop is None:
        loop = _thread_loop_from_setting()
        source = "THREAD_LOOP"
    if loop is None:
        return None
    if loop.is_closed() or not loop.is_running():
        raise ImproperlyConfigured(
            f"drf-batch: the event loop from {source} is not running; it "
            "must run (loop.run_forever()) in a thread of its own."
        )
    return loop


def current_loop() -> asyncio.AbstractEventLoop:
    """The loop :func:`run_in_loop` would use from the calling thread.

    Lets the application create loop-bound clients on it (with
    ``asyncio.run_coroutine_threadsafe``). Starts the built-in thread if
    that is the loop in use.
    """
    loop = _external_loop()
    if loop is not None:
        return loop
    with _manager.session() as builtin:
        return builtin


def _close_unawaited(coro: Any) -> None:
    # A coroutine that is never scheduled warns "was never awaited".
    close = getattr(coro, "close", None)
    if callable(close):
        close()


def run_in_loop(
    coro: Any, timeout: float | None = None, *, serve_caller: bool = False
) -> Any:
    """Run ``coro`` on the selected loop and block until it resolves.

    ``coro`` is a coroutine (an ``async def collect`` result, the collection
    graph, or any awaitable a sync ``collect`` returns). Raises
    ``RuntimeError`` if the calling thread already has a running event
    loop: call the synchronous code through ``asgiref``'s
    ``sync_to_async`` there (which then runs collection on that loop), or
    await :func:`wait` directly. Raises
    :class:`~drf_batch.exceptions.BatchCollectionTimeout` if the result is
    not available within ``timeout`` seconds; any other exception raised
    by ``coro`` propagates unchanged.

    With ``serve_caller=True`` the calling thread does not just wait: it
    runs the blocking calls ``coro`` hands it through :func:`run_sync`, one
    at a time, so they use this thread's database connection (and see its
    open transaction).
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        _close_unawaited(coro)
        raise RuntimeError(
            "drf_batch.loop.run_in_loop() must be called from a thread "
            "without a running event loop; wrap the synchronous call in "
            "asgiref.sync.sync_to_async(), or await drf_batch.loop.wait()."
        ) from None

    try:
        external = _external_loop()
    except BaseException:
        _close_unawaited(coro)
        raise
    if external is not None:
        return _submit(coro, external, timeout, serve_caller)
    with _manager.session() as builtin:
        return _submit(coro, builtin, timeout, serve_caller)


def _submit(
    coro: Any,
    loop: asyncio.AbstractEventLoop,
    timeout: float | None,
    serve_caller: bool,
) -> Any:
    executor = _CallerExecutor() if serve_caller else None
    # The task runs in a copy of this context (call_soon_threadsafe copies
    # it), so run_sync() inside the coroutine finds this thread's executor.
    token = _caller_executor.set(executor)
    try:
        future = asyncio.run_coroutine_threadsafe(coro, loop)
    finally:
        _caller_executor.reset(token)
    try:
        if executor is not None:
            return executor.serve(future, timeout)
        return future.result(timeout=timeout)
    except TimeoutError:
        # Timing out while the task is still running: cancel it so it stops
        # doing work. (If the coroutine itself raised TimeoutError the
        # future is done and the original exception is re-raised unchanged.)
        if not future.done():
            future.cancel()
            raise BatchCollectionTimeout() from None
        raise
    except BaseException:
        if not future.done():
            future.cancel()
        raise


class _CallerCall:
    """One blocking call handed from the loop to the waiting thread."""

    def __init__(
        self,
        func: Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        future: asyncio.Future[Any],
    ) -> None:
        self.func = func
        self.args = args
        self.kwargs = kwargs
        self.future = future

    def run(self) -> None:
        loop = self.future.get_loop()
        try:
            result = self.func(*self.args, **self.kwargs)
        except Exception as exc:
            outcome: Any = exc
            failed = True
        else:
            outcome = result
            failed = False
        # The loop may already be gone (timeout, shutdown): nothing to settle.
        with suppress(RuntimeError):
            loop.call_soon_threadsafe(_settle, self.future, outcome, failed)


def _settle(future: asyncio.Future[Any], outcome: Any, failed: bool) -> None:
    # On the loop. A cancelled (timed out) call's future is already done.
    if future.done():
        return
    if failed:
        future.set_exception(outcome)
    else:
        future.set_result(outcome)


class _CallerExecutor:
    """Runs blocking calls on the thread blocked in :func:`run_in_loop`.

    The loop side queues calls (:meth:`submit`); the waiting thread runs
    them in order (:meth:`serve`) until the submitted coroutine is done.
    """

    _DONE = object()

    def __init__(self) -> None:
        self._queue: queue.SimpleQueue[Any] = queue.SimpleQueue()

    def submit(
        self, func: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> asyncio.Future[Any]:
        future = asyncio.get_running_loop().create_future()
        self._queue.put(_CallerCall(func, args, kwargs, future))
        return future

    def serve(
        self, future: concurrent.futures.Future[Any], timeout: float | None
    ) -> Any:
        future.add_done_callback(lambda _: self._queue.put(self._DONE))
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise TimeoutError
            try:
                item = self._queue.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError from None
            if item is self._DONE:
                return future.result()
            item.run()


_caller_executor: contextvars.ContextVar[_CallerExecutor | None] = (
    contextvars.ContextVar("drf_batch_caller_executor", default=None)
)


async def run_sync(func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """Run a blocking call where it can see the request's database state.

    Inside collection started with ``serve_caller=True`` (the request's
    connection is in a transaction) the call runs on the **request thread**,
    which serves such calls one at a time while it waits, so it sees the
    transaction's uncommitted writes. Anywhere else it runs on a worker
    thread, like :func:`to_thread`.
    """
    executor = _caller_executor.get()
    if executor is None:
        return await to_thread(func, *args, **kwargs)
    return await executor.submit(func, args, kwargs)


async def wait(coro: Any, timeout: float | None = None) -> Any:
    """Await ``coro`` directly, for callers already inside a running loop.

    Maps ``asyncio``'s timeout to
    :class:`~drf_batch.exceptions.BatchCollectionTimeout`.
    """
    try:
        return await asyncio.wait_for(coro, timeout)
    except TimeoutError:
        raise BatchCollectionTimeout() from None


def _call_with_connection_hygiene(
    func: Callable[..., Any], /, *args: Any, **kwargs: Any
) -> Any:
    # Django's request-boundary handling, applied to this (worker) thread's
    # connections only (they are thread-local): drop unusable or expired
    # ones before the call, and close the ones CONN_MAX_AGE does not keep
    # after it.
    close_old_connections()
    try:
        return func(*args, **kwargs)
    finally:
        close_old_connections()


async def to_thread(func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """Run a blocking call (e.g. a Django ORM query) on the thread pool.

    Like :func:`asyncio.to_thread`, with Django's connection handling around
    the call (``close_old_connections()``): with ``CONN_MAX_AGE = 0`` (the
    default) the worker's connection is closed afterwards; with a positive
    ``CONN_MAX_AGE`` it is reused until it expires or breaks. The call runs
    on its own connection: it does **not** see uncommitted writes of the
    request's transaction.
    """
    return await asyncio.to_thread(_call_with_connection_hygiene, func, *args, **kwargs)
