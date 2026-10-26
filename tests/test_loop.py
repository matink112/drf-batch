from __future__ import annotations

import asyncio
import subprocess
import sys

import pytest

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
