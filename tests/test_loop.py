from __future__ import annotations

import asyncio
import inspect
import os
import subprocess
import sys
import threading
from unittest import mock

import pytest
from asgiref.sync import sync_to_async
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from drf_batch import loop
from drf_batch.exceptions import BatchCollectionTimeout


@pytest.fixture(autouse=True)
def _fresh_loop_state():
    # Every test starts (and ends) with no loop thread running; the
    # teardown proves shutdown stops the thread and a later session
    # re-creates a working loop.
    loop.shutdown()
    yield
    loop.shutdown()


def _assert_idle():
    assert loop._manager.in_flight == 0


def _assert_no_loop():
    # No thread and no refcount: the state before first use / after shutdown.
    assert loop._loop_thread() is None
    _assert_idle()


def test_importing_drf_batch_starts_no_loop_thread():
    _assert_no_loop()


def test_import_starts_no_loop_thread_in_subprocess():
    code = (
        "import threading, drf_batch, drf_batch.loop; "
        "print(sorted(t.name for t in threading.enumerate()))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert "drf-batch-loop" not in result.stdout


def test_session_creates_the_loop_once_and_reuses_it():
    with loop.session() as first:
        thread = loop._loop_thread()
        assert thread is not None
        assert thread.is_alive()
        assert first.is_running()
        with loop.session() as second:
            pass
    assert first is second
    # The refcount drained to 0; the thread intentionally stays alive until
    # shutdown() (no per-request thread churn).
    _assert_idle()


def test_shutdown_stops_the_thread_and_a_later_session_recreates_the_loop():
    with loop.session() as first:
        pass
    thread = loop._loop_thread()
    assert thread is not None

    loop.shutdown()

    assert not thread.is_alive()
    assert first.is_closed()
    _assert_no_loop()

    with loop.session() as second:
        assert second is not first
        assert loop._loop_thread() is not None


def test_shutdown_without_a_loop_is_a_noop():
    _assert_no_loop()
    loop.shutdown()
    _assert_no_loop()


def test_run_in_loop_returns_the_coroutine_result():
    async def coro():
        await asyncio.sleep(0.01)
        return 123

    assert loop.run_in_loop(coro()) == 123


def test_run_in_loop_propagates_coroutine_exceptions():
    async def coro():
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        loop.run_in_loop(coro())


def test_run_in_loop_from_a_running_loop_raises_runtime_error():
    async def runner():
        coro = asyncio.sleep(0)
        try:
            with pytest.raises(RuntimeError, match="drf_batch\\.loop\\.wait"):
                loop.run_in_loop(coro)
        finally:
            coro.close()

    asyncio.run(runner())


def test_run_in_loop_timeout_raises_batch_collection_timeout():
    async def coro():
        await asyncio.sleep(0.5)

    with pytest.raises(BatchCollectionTimeout):
        loop.run_in_loop(coro(), timeout=0.05)


def test_wait_awaits_directly_inside_a_running_loop():
    async def runner():
        return await loop.wait(asyncio.sleep(0, result=42))

    assert asyncio.run(runner()) == 42


def test_wait_timeout_maps_to_batch_collection_timeout():
    async def runner():
        await loop.wait(asyncio.sleep(0.5), timeout=0.05)

    with pytest.raises(BatchCollectionTimeout):
        asyncio.run(runner())


def test_shutdown_raises_if_sessions_do_not_drain_in_time():
    with loop.session(), pytest.raises(RuntimeError, match="in-flight"):
        loop.shutdown(timeout=0.1)


# ---------------------------------------------------------------------------
# loop selection: user loop (thread mode), THREAD_LOOP, ASGI, EVENT_LOOP
# ---------------------------------------------------------------------------


@pytest.fixture
def user_loop():
    """A loop the "application" runs in its own thread."""
    owned = asyncio.new_event_loop()
    thread = threading.Thread(target=owned.run_forever, name="app-loop", daemon=True)
    thread.start()
    yield owned
    loop.use_loop(None)
    owned.call_soon_threadsafe(owned.stop)
    thread.join(2)
    owned.close()


async def _thread_name() -> str:
    return threading.current_thread().name


_REGISTERED: list[asyncio.AbstractEventLoop] = []


def registered_loop() -> asyncio.AbstractEventLoop:
    """THREAD_LOOP target used by the tests below."""
    return _REGISTERED[-1]


def test_use_loop_runs_collection_on_the_application_loop(user_loop):
    loop.use_loop(user_loop)

    assert loop.run_in_loop(_thread_name()) == "app-loop"
    assert loop.current_loop() is user_loop
    _assert_no_loop()  # the built-in thread was never needed


def test_use_loop_none_restores_the_builtin_thread(user_loop):
    loop.use_loop(user_loop)
    loop.use_loop(None)

    assert loop.run_in_loop(_thread_name()) == "drf-batch-loop"


def test_use_loop_rejects_a_non_loop():
    with pytest.raises(TypeError, match="event loop"):
        loop.use_loop(object())  # type: ignore[arg-type]


def test_shutdown_leaves_the_application_loop_running(user_loop):
    loop.use_loop(user_loop)
    loop.shutdown()

    assert user_loop.is_running()


def test_a_registered_loop_that_is_not_running_is_a_configuration_error():
    stopped = asyncio.new_event_loop()
    loop.use_loop(stopped)
    coro = _thread_name()
    try:
        with pytest.raises(ImproperlyConfigured, match="not running"):
            loop.run_in_loop(coro)
    finally:
        loop.use_loop(None)
        stopped.close()
    assert inspect.getcoroutinestate(coro) == "CORO_CLOSED"  # not left un-awaited


@override_settings(DRF_BATCH={"THREAD_LOOP": "tests.test_loop.registered_loop"})
def test_thread_loop_setting_names_the_application_loop(user_loop):
    _REGISTERED.append(user_loop)
    try:
        assert loop.run_in_loop(_thread_name()) == "app-loop"
    finally:
        _REGISTERED.clear()


def test_use_loop_takes_precedence_over_the_setting(user_loop):
    other = asyncio.new_event_loop()  # never running: would be an error
    _REGISTERED.append(other)
    loop.use_loop(user_loop)
    try:
        with override_settings(
            DRF_BATCH={"THREAD_LOOP": "tests.test_loop.registered_loop"}
        ):
            assert loop.run_in_loop(_thread_name()) == "app-loop"
    finally:
        _REGISTERED.clear()
        other.close()


@pytest.mark.parametrize(
    ("value", "match"),
    [
        ("tests.test_loop.missing", "cannot import"),
        ("tests.test_loop.WAIT_NOT_CALLABLE", "not callable"),
        ("tests.test_loop._thread_name_sync", "not an event loop"),
        (42, "dotted path"),
    ],
)
def test_invalid_thread_loop_settings(value, match):
    with (
        override_settings(DRF_BATCH={"THREAD_LOOP": value}),
        pytest.raises(ImproperlyConfigured, match=match),
    ):
        loop.current_loop()


WAIT_NOT_CALLABLE = 1.0


def _thread_name_sync() -> str:
    return threading.current_thread().name


@override_settings(DRF_BATCH={"EVENT_LOOP": "process"})
def test_unknown_event_loop_mode_is_a_configuration_error():
    with pytest.raises(ImproperlyConfigured, match="EVENT_LOOP"):
        loop.current_loop()


def _collect_under_asgi() -> tuple[str, str]:
    """Like a sync DRF view under ASGI: called through sync_to_async."""

    async def main() -> tuple[str, str]:
        view_thread = await sync_to_async(threading.current_thread)()
        ran_on = await sync_to_async(loop.run_in_loop)(_thread_name())
        assert view_thread.name != threading.current_thread().name
        return threading.current_thread().name, ran_on

    return asyncio.run(main())


def test_auto_mode_runs_on_the_asgi_server_loop():
    server_thread, ran_on = _collect_under_asgi()

    assert ran_on == server_thread
    _assert_no_loop()


@override_settings(DRF_BATCH={"EVENT_LOOP": "thread"})
def test_thread_mode_ignores_the_asgi_server_loop():
    server_thread, ran_on = _collect_under_asgi()

    assert ran_on == "drf-batch-loop"
    assert ran_on != server_thread


def test_auto_mode_outside_asgi_uses_thread_mode():
    assert loop._asgi_main_loop() is None
    assert loop.run_in_loop(_thread_name()) == "drf-batch-loop"


# ---------------------------------------------------------------------------
# serving the caller: run_sync on the waiting thread
# ---------------------------------------------------------------------------


def test_run_sync_runs_on_the_waiting_thread_when_serving_it():
    async def coro():
        return await asyncio.gather(
            loop.run_sync(_thread_name_sync), loop.run_sync(_thread_name_sync)
        )

    caller = threading.current_thread().name
    assert loop.run_in_loop(coro(), serve_caller=True) == [caller, caller]


def test_run_sync_without_a_served_caller_uses_a_worker_thread():
    async def coro():
        return await loop.run_sync(_thread_name_sync)

    name = loop.run_in_loop(coro())
    assert name not in {threading.current_thread().name, "drf-batch-loop"}


def test_run_sync_propagates_the_call_exception():
    def boom():
        raise ValueError("bad query")

    async def coro():
        return await loop.run_sync(boom)

    with pytest.raises(ValueError, match="bad query"):
        loop.run_in_loop(coro(), serve_caller=True)


def test_serving_the_caller_still_times_out():
    async def coro():
        await asyncio.sleep(0.5)

    with pytest.raises(BatchCollectionTimeout):
        loop.run_in_loop(coro(), timeout=0.05, serve_caller=True)


# ---------------------------------------------------------------------------
# fork and exit
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX only")
def test_a_forked_child_gets_a_working_loop():
    assert loop.run_in_loop(_thread_name()) == "drf-batch-loop"  # parent started it

    pid = os.fork()
    if pid == 0:  # pragma: no cover - runs in the child
        try:
            ok = loop.run_in_loop(_thread_name(), timeout=2) == "drf-batch-loop"
        except BaseException:
            ok = False
        os._exit(0 if ok else 1)
    _, status = os.waitpid(pid, 0)

    assert os.waitstatus_to_exitcode(status) == 0


def test_exit_shutdown_is_bounded_and_quiet():
    started = threading.Event()
    release = threading.Event()

    async def blocker():
        started.set()
        release.wait(5)  # blocks the loop thread itself

    with loop.session() as running:
        asyncio.run_coroutine_threadsafe(blocker(), running)
    assert started.wait(2)
    thread = loop._loop_thread()
    with mock.patch.object(loop, "_EXIT_SHUTDOWN_TIMEOUT", 0.1):
        loop._shutdown_at_exit()  # the thread cannot stop in time: no raise
    assert thread is not None and thread.is_alive()

    release.set()
    thread.join(2)
    running.close()
