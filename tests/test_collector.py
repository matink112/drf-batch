from __future__ import annotations

import asyncio
from typing import Any

import pytest
from django.test import override_settings
from rest_framework.test import APIClient

from drf_batch import BatchCollector, BatchListSerializer, loop
from drf_batch.exceptions import BatchCollectionTimeout
from tests.apps.demo.collectors import AsyncSpyCollector, SpyCollector
from tests.apps.demo.models import Customer, Order
from tests.apps.demo.serializers import OrderSerializer

BATCH_URL = "/spy-orders/batch/"
RAW_BATCH_URL = "/raw-orders/batch/"
ASYNC_BATCH_URL = "/async-orders/batch/"
ASYNC_SPY_BATCH_URL = "/async-spy-orders/batch/"
SLOW_ASYNC_BATCH_URL = "/slow-async-orders/batch/"
FAILING_ASYNC_BATCH_URL = "/failing-async-orders/batch/"


def test_base_collector_collect_raises_not_implemented_and_extract_is_gone():
    collector = BatchCollector()

    with pytest.raises(NotImplementedError):
        collector.collect({}, {})
    assert not hasattr(BatchCollector, "extract")


def test_taskgroup_helper_returns_an_asyncio_taskgroup():
    assert isinstance(BatchCollector().taskgroup(), asyncio.TaskGroup)


def test_collect_timeout_defaults_to_none():
    assert BatchCollector().collect_timeout is None


@pytest.fixture(autouse=True)
def _reset_spy():
    SpyCollector.instances.clear()
    yield
    SpyCollector.instances.clear()


@pytest.fixture(autouse=True)
def _reset_async_spy():
    AsyncSpyCollector.instances.clear()
    yield
    AsyncSpyCollector.instances.clear()


@pytest.fixture(autouse=True)
def _shutdown_batch_loop():
    # Stop the shared loop after every test so the module's sync tests
    # cannot inherit loop state from the async ones (and proving clean
    # teardown + re-creation between tests).
    yield
    loop.shutdown()


@pytest.fixture
def spy_customers(db) -> list[Customer]:
    # Distinct (ticker, customer) pairs per row: the demo model has a
    # unique (ticker, customer) constraint.
    return [
        Customer.objects.create(isin=f"11200{i}", name=f"C{i}") for i in range(1, 6)
    ]


def _spy_rows(n: int) -> list[dict[str, object]]:
    # Alternating int/str ticker values: the collector must see coerced
    # (str) key sets either way.
    return [
        {
            "ticker": 1101 if i % 2 == 0 else "1102",
            "customer": f"11200{i + 1}",
            "volume": i + 1,
            "reference_id": f"SPY_{i}",
        }
        for i in range(n)
    ]


def _async_rows() -> list[dict[str, object]]:
    return [
        {
            "ticker": "1101",
            "customer": "112001",
            "volume": 10,
            "reference_id": "ASYNC_1",
        },
        {
            "ticker": "1102",
            "customer": "112001",
            "volume": 5,
            "reference_id": "ASYNC_2",
        },
    ]


def test_collect_runs_once_per_request_and_persists(client, tickers, spy_customers):
    rows = _spy_rows(5)
    response = client.post(BATCH_URL, rows)

    assert response.status_code == 201
    assert response.json() == {"count": 5}
    (spy,) = SpyCollector.instances
    assert spy.collect_calls == 1
    assert spy.seen_context is not None
    assert "request" in spy.seen_context


def test_collect_receives_coerced_key_sets(client, tickers, spy_customers):
    rows = _spy_rows(3)
    response = client.post(BATCH_URL, rows)

    assert response.status_code == 201
    (spy,) = SpyCollector.instances
    assert spy.seen_keys == {
        "ticker": {"1101", "1102"},
        "customer": {"112001", "112002", "112003"},
    }


def test_no_collector_means_plain_batch_create(client, tickers, customers):
    t = tickers[0]
    c = customers[0]
    rows = [
        {
            "ticker": t.pk,
            "customer": c.pk,
            "volume": 4,
            "reference_id": "RAW_1",
        },
    ]
    response = client.post(RAW_BATCH_URL, rows)

    assert response.status_code == 201
    assert response.json() == {"count": 1}
    order = Order.objects.get(reference_id="RAW_1")
    assert order.ticker == t
    assert order.customer == c


def _serializer_with_collector(collector, rows=None):
    context: dict[str, Any] = {}
    return BatchListSerializer(
        child=OrderSerializer(context=context),
        data=rows or _async_rows()[:1],
        context=context,
        collector=collector,
    )


def test_collect_timeout_overrides_the_setting():
    class Slow(BatchCollector):
        collect_timeout = 0.05

        async def collect(self, keys, context):
            await asyncio.sleep(0.3)
            return {}

    with (
        override_settings(DRF_BATCH={"COLLECT_TIMEOUT": 30}),
        pytest.raises(BatchCollectionTimeout),
    ):
        _serializer_with_collector(Slow()).is_valid(raise_exception=True)


def test_setting_collect_timeout_applies_when_collect_timeout_is_unset():
    class Slow(BatchCollector):
        async def collect(self, keys, context):
            await asyncio.sleep(0.3)
            return {}

    with (
        override_settings(DRF_BATCH={"COLLECT_TIMEOUT": 0.05}),
        pytest.raises(BatchCollectionTimeout),
    ):
        _serializer_with_collector(Slow()).is_valid(raise_exception=True)


@pytest.mark.django_db(transaction=True)
class TestAsyncCollector:
    """Async end-to-end tests.

    ``transaction=True`` commits the fixture data (no rollback transaction),
    so the drf-batch loop thread's own database connection — the shared
    in-memory test database — can see the tickers/customers the sync
    collectors resolve.
    """

    def test_async_collector_end_to_end_persists_rows(self, client, tickers, customers):
        rows = _async_rows()
        response = client.post(ASYNC_BATCH_URL, rows)

        assert response.status_code == 201
        assert response.json() == {"count": 2}
        assert Order.objects.count() == 2
        order = Order.objects.get(reference_id="ASYNC_1")
        assert order.ticker.isin == "1101"
        assert order.customer.isin == "112001"

    def test_async_spy_collector_runs_once_per_request(
        self, client, tickers, spy_customers
    ):
        rows = _spy_rows(3)
        response = client.post(ASYNC_SPY_BATCH_URL, rows)

        assert response.status_code == 201
        (spy,) = AsyncSpyCollector.instances
        assert spy.collect_calls == 1
        assert spy.seen_keys == {
            "ticker": {"1101", "1102"},
            "customer": {"112001", "112002", "112003"},
        }

    def test_async_collector_timeout_is_504_and_loop_stays_usable(
        self, client, tickers, customers
    ):
        rows = _async_rows()
        before = Order.objects.count()

        response = client.post(SLOW_ASYNC_BATCH_URL, rows)

        assert response.status_code == 504
        assert Order.objects.count() == before

        response = client.post(ASYNC_BATCH_URL, rows)

        assert response.status_code == 201
        assert Order.objects.count() == before + 2

    def test_async_collector_exception_is_500_and_loop_stays_usable(
        self, client, user, tickers, customers
    ):
        quiet_client = APIClient(raise_request_exception=False)
        quiet_client.force_authenticate(user=user)
        rows = _async_rows()

        response = quiet_client.post(FAILING_ASYNC_BATCH_URL, rows)

        assert response.status_code == 500
        assert Order.objects.count() == 0

        response = client.post(ASYNC_BATCH_URL, rows)

        assert response.status_code == 201
        assert Order.objects.count() == 2
