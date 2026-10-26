"""Replacement-hook collectors (auto collection is the default).

Each ``collect`` receives the coerced key sets derived from the serializer's
cached fields (``{"ticker": {...}, "customer": {...}}``) and must return
every map the fields need.
"""

from __future__ import annotations

import asyncio
from typing import Any

from drf_batch import BatchCollector, BatchContext

from .models import Customer, Ticker


def _tickers_by_isin(isins: set[Any]) -> dict[Any, Any]:
    return Ticker.objects.filter(isin__in=isins).in_bulk(field_name="isin")


def _customers_by_isin(isins: set[Any]) -> dict[Any, Any]:
    return Customer.objects.filter(isin__in=isins).in_bulk(field_name="isin")


class SpyCollector(BatchCollector):
    """Replacement-hook test collector that records every call."""

    instances: list[SpyCollector] = []

    def __init__(self) -> None:
        super().__init__()
        self.collect_calls = 0
        self.seen_keys: dict[str, Any] | None = None
        self.seen_context: BatchContext | None = None
        type(self).instances.append(self)

    def collect(
        self, keys: dict[str, set[Any]], context: BatchContext
    ) -> dict[str, dict[Any, Any]]:
        self.collect_calls += 1
        self.seen_keys = keys
        self.seen_context = context
        return {
            "ticker": _tickers_by_isin(keys.get("ticker", set())),
            "customer": _customers_by_isin(keys.get("customer", set())),
        }


class AsyncSpyCollector(BatchCollector):
    """Async replacement collector that records every call."""

    instances: list[AsyncSpyCollector] = []

    def __init__(self) -> None:
        super().__init__()
        self.collect_calls = 0
        self.seen_keys: dict[str, Any] | None = None
        self.seen_context: BatchContext | None = None
        type(self).instances.append(self)

    async def collect(
        self, keys: dict[str, set[Any]], context: BatchContext
    ) -> dict[str, dict[Any, Any]]:
        self.collect_calls += 1
        self.seen_keys = keys
        self.seen_context = context
        tickers, customers = await asyncio.gather(
            self.to_thread(_tickers_by_isin, keys.get("ticker", set())),
            self.to_thread(_customers_by_isin, keys.get("customer", set())),
        )
        return {"ticker": tickers, "customer": customers}


class SlowAsyncCollector(BatchCollector):
    """Async collector that sleeps past its timeout (504 demo)."""

    collect_timeout = 0.05
    sleep_seconds = 0.5

    async def collect(
        self, keys: dict[str, set[Any]], context: BatchContext
    ) -> dict[str, dict[Any, Any]]:
        await asyncio.sleep(self.sleep_seconds)
        return {
            "ticker": _tickers_by_isin(keys.get("ticker", set())),
            "customer": _customers_by_isin(keys.get("customer", set())),
        }


class FailingAsyncCollector(BatchCollector):
    """Async collector that raises (500 demo)."""

    async def collect(
        self, keys: dict[str, set[Any]], context: BatchContext
    ) -> dict[str, dict[Any, Any]]:
        raise ValueError("collector exploded")
        return {}
