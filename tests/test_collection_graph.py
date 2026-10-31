"""The auto plan as a dependency graph: concurrency, order, errors, threads.

Fields 1..7 with 2 and 3 depending on 1, 4 on 3 and 6 on 5 must run as
``TG(1 -> TG(2, 3 -> 4), 5 -> 6, 7)``: every fetch starts as soon as its own
``depends_on`` are done. The fetches prove it with events that only a
concurrent schedule can satisfy (a sequential one times out).
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest
from django.test import override_settings

from drf_batch import (
    BatchListSerializer,
    BatchRelatedField,
    BatchSerializer,
    BatchServiceField,
    loop,
)
from drf_batch.exceptions import BatchCollectionTimeout
from tests.apps.demo.models import Ticker

WAIT = 2.0  # generous: a wrong schedule fails by timing out, not by flaking


@pytest.fixture(autouse=True)
def _shutdown_batch_loop():
    yield
    loop.shutdown()


class _Base(BatchSerializer):
    # Only `name` is writable; the batch fields under test are read-only.
    class Meta:
        model = Ticker
        exclude = ("isin",)


def _svc(fetch: Any, *depends_on: str) -> BatchServiceField:
    return BatchServiceField(fetch=fetch, depends_on=depends_on, read_only=True)


def _validate(child: BatchSerializer, rows: list[dict[str, Any]], **kwargs: Any):
    context: dict[str, Any] = {}
    serializer = BatchListSerializer(
        child=child.__class__(context=context), data=rows, context=context, **kwargs
    )
    serializer.is_valid(raise_exception=True)
    return serializer, context


class _Recorder:
    """Shared events and an ordered log of fetch starts/ends."""

    def __init__(self, names: str) -> None:
        self.started = {n: asyncio.Event() for n in names}
        self.log: list[str] = []
        self.threads: dict[str, str] = {}

    def fetch(self, name: str, body: Any = None) -> Any:
        async def fetch(keys, collected, context):
            self.log.append(f"start {name}")
            self.started[name].set()
            if body is not None:
                await body(collected)
            self.log.append(f"end {name}")
            return {name: f"value-{name}"}

        return fetch


def _wait_for(event: asyncio.Event) -> Any:
    return asyncio.wait_for(event.wait(), WAIT)


def test_fetches_start_as_soon_as_their_own_dependencies_are_done():
    rec = _Recorder("1234567")

    async def roots_together(collected):
        # 1, 5 and 7 have no dependencies: all three must be running.
        await asyncio.gather(*(_wait_for(rec.started[n]) for n in "157"))

    async def two_waits_for_four(collected):
        # 4 depends on 3 only, so it must start while 2 is still running.
        assert "f1" in collected  # its dependency is in the snapshot
        await _wait_for(rec.started["4"])

    class Graph(_Base):
        f1 = _svc(rec.fetch("1", roots_together))
        f2 = _svc(rec.fetch("2", two_waits_for_four), "f1")
        f3 = _svc(rec.fetch("3"), "f1")
        f4 = _svc(rec.fetch("4"), "f3")
        f5 = _svc(rec.fetch("5", roots_together))
        f6 = _svc(rec.fetch("6"), "f5")
        f7 = _svc(rec.fetch("7", roots_together))

    _, context = _validate(Graph(), [{"name": "x"}])

    log = rec.log
    for dependent, dependency in (("2", "1"), ("3", "1"), ("4", "3"), ("6", "5")):
        assert log.index(f"start {dependent}") > log.index(f"end {dependency}")
    assert log.index("start 4") < log.index("end 2")
    # Plan order, not completion order.
    assert list(context["collected"]) == ["f1", "f2", "f3", "f4", "f5", "f6", "f7"]
    assert context["collected"]["f4"] == {"4": "value-4"}


def test_a_fetch_gets_a_snapshot_that_includes_its_dependencies():
    seen: dict[str, Any] = {}

    async def first(keys, collected, context):
        return {"a": 1}

    async def second(keys, collected, context):
        seen.update(collected)
        collected["junk"] = True  # a snapshot: never leaks into the result
        return {"b": 2}

    class S(_Base):
        one = BatchServiceField(fetch=first, read_only=True)
        two = BatchServiceField(fetch=second, depends_on=("one",), read_only=True)

    _, context = _validate(S(), [{"name": "x"}])

    assert seen["one"] == {"a": 1}
    assert "junk" not in context["collected"]


def test_sync_fetches_run_concurrently_on_worker_threads():
    # Outside a transaction: both sync fetches must be running at once.
    barrier = threading.Barrier(2, timeout=WAIT)
    threads: set[str] = set()

    def sync_fetch(keys, collected, context):
        threads.add(threading.current_thread().name)
        barrier.wait()
        return {}

    class S(_Base):
        a = BatchServiceField(fetch=sync_fetch, read_only=True)
        b = BatchServiceField(fetch=sync_fetch, read_only=True)

    _validate(S(), [{"name": "x"}])

    assert len(threads) == 2
    assert threading.current_thread().name not in threads


def test_mixed_sync_and_async_fetches_share_one_graph():
    order: list[str] = []

    def sync_first(keys, collected, context):
        order.append("sync")
        return {"x": 1}

    async def async_after(keys, collected, context):
        order.append("async")
        return {"y": collected["s"]["x"] + 1}

    class S(_Base):
        s = BatchServiceField(fetch=sync_first, read_only=True)
        a = BatchServiceField(fetch=async_after, depends_on=("s",), read_only=True)

    _, context = _validate(S(), [{"name": "x"}])

    assert order == ["sync", "async"]
    assert context["collected"]["a"] == {"y": 2}


def test_a_failing_fetch_raises_its_own_exception_and_cancels_the_others():
    cancelled = threading.Event()

    async def boom(keys, collected, context):
        await asyncio.sleep(0.01)
        raise ValueError("service down")

    async def slow(keys, collected, context):
        try:
            await asyncio.sleep(WAIT)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return {}

    class S(_Base):
        a = BatchServiceField(fetch=boom, read_only=True)
        b = BatchServiceField(fetch=slow, read_only=True)
        c = BatchServiceField(fetch=slow, depends_on=("a",), read_only=True)

    with pytest.raises(ValueError, match="service down"):
        _validate(S(), [{"name": "x"}])
    assert cancelled.wait(WAIT)


@override_settings(DRF_BATCH={"COLLECT_TIMEOUT": 0.05})
def test_the_graph_shares_one_collection_timeout():
    async def slow(keys, collected, context):
        await asyncio.sleep(WAIT)
        return {}

    class S(_Base):
        a = BatchServiceField(fetch=slow, read_only=True)

    with pytest.raises(BatchCollectionTimeout):
        _validate(S(), [{"name": "x"}])


def test_a_map_shared_by_two_fields_is_fetched_once_and_awaited_by_both():
    calls: list[str] = []

    async def shared(keys, collected, context):
        calls.append("shared")
        await asyncio.sleep(0.01)
        return {"v": 1}

    async def after(keys, collected, context):
        return {"seen": "data" in collected}

    class S(_Base):
        first = BatchServiceField("data", fetch=shared, read_only=True)
        second = BatchServiceField("data", fetch=shared, read_only=True)
        later = BatchServiceField(fetch=after, depends_on=("second",), read_only=True)

    _, context = _validate(S(), [{"name": "x"}])

    assert calls == ["shared"]
    assert context["collected"]["later"] == {"seen": True}


def test_a_plan_with_nothing_to_fetch_starts_no_loop_thread():
    class S(_Base):
        ticker = BatchRelatedField(model=Ticker, lookup="isin", read_only=True)

    # No row supplies a ticker: the lookup is provably empty, so it is skipped.
    _, context = _validate(S(), [{"name": "x"}])

    assert loop._loop_thread() is None
    assert context["collected"] == {"ticker": {}}


@pytest.mark.django_db
def test_inside_a_transaction_sync_fetches_run_on_the_request_thread(tickers):
    # pytest-django wraps the test in a transaction: a worker thread could
    # not see the fixture rows, so the request thread runs the sync fetches.
    threads: list[str] = []

    def sync_fetch(keys, collected, context):
        threads.append(threading.current_thread().name)
        return {t.isin: t for t in Ticker.objects.all()}

    async def async_fetch(keys, collected, context):
        return {"n": len(collected["db"])}

    class S(_Base):
        db = BatchServiceField(fetch=sync_fetch, read_only=True)
        count = _svc(async_fetch, "db")

    _, context = _validate(S(), [{"name": "x"}])

    assert threads == [threading.current_thread().name]
    assert set(context["collected"]["db"]) == {"1101", "1102"}
    assert context["collected"]["count"] == {"n": 2}


@pytest.mark.django_db
@override_settings(DRF_BATCH={"COLLECT_TIMEOUT": 0.05})
def test_inside_a_transaction_the_timeout_still_applies(tickers):
    async def slow(keys, collected, context):
        await asyncio.sleep(WAIT)
        return {}

    class S(_Base):
        a = BatchServiceField(fetch=slow, read_only=True)

    with pytest.raises(BatchCollectionTimeout):
        _validate(S(), [{"name": "x"}])


def test_sequential_mode_runs_sync_inline_and_bridges_async_one_by_one():
    threads: list[str] = []

    def sync_fetch(keys, collected, context):
        threads.append(threading.current_thread().name)
        return {"x": 1}

    async def async_fetch(keys, collected, context):
        threads.append(threading.current_thread().name)
        return {"y": collected["s"]["x"]}

    class S(_Base):
        s = BatchServiceField(fetch=sync_fetch, read_only=True)
        a = BatchServiceField(fetch=async_fetch, read_only=True)

    _, context = _validate(S(), [{"name": "x"}], concurrent_collection=False)

    assert threads == [threading.current_thread().name, "drf-batch-loop"]
    assert context["collected"]["a"] == {"y": 1}


@override_settings(DRF_BATCH={"COLLECT_TIMEOUT": 0.05})
def test_sequential_mode_shares_one_timeout_across_fetches():
    async def slow(keys, collected, context):
        await asyncio.sleep(0.04)
        return {}

    class S(_Base):
        a = BatchServiceField(fetch=slow, read_only=True)
        b = BatchServiceField(fetch=slow, read_only=True)

    with pytest.raises(BatchCollectionTimeout):
        _validate(S(), [{"name": "x"}], concurrent_collection=False)


@pytest.mark.parametrize(
    ("vendor", "name", "private"),
    [
        ("sqlite", ":memory:", True),
        ("sqlite", "file:memdb1?mode=memory", True),
        ("sqlite", "file::memory:?cache=shared", False),
        ("sqlite", "file:memdb1?mode=memory&cache=shared", False),
        ("sqlite", "/var/db/app.sqlite3", False),
        ("postgresql", ":memory:", False),
    ],
)
def test_private_in_memory_sqlite_keeps_sync_fetches_on_the_request_thread(
    vendor, name, private
):
    # Every connection to a private in-memory database is a new, empty one.
    from types import SimpleNamespace

    from drf_batch.pipeline import _is_private_memory_db

    conn = SimpleNamespace(vendor=vendor, settings_dict={"NAME": name})
    assert _is_private_memory_db(conn) is private
