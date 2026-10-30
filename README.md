# drf-batch

**The missing write-path DataLoader for Django REST Framework.**

[![PyPI](https://img.shields.io/pypi/v/drf-batch.svg)](https://pypi.org/project/drf-batch/)
[![Python](https://img.shields.io/pypi/pyversions/drf-batch.svg)](https://pypi.org/project/drf-batch/)
[![CI](https://github.com/matink112/drf-batch/actions/workflows/ci.yml/badge.svg)](https://github.com/matink112/drf-batch/actions/workflows/ci.yml)
[![Docs](https://github.com/matink112/drf-batch/actions/workflows/docs.yml/badge.svg)](https://matink112.github.io/drf-batch/)
[![License](https://img.shields.io/badge/license-BSD--3--Clause-blue.svg)](LICENSE)

**[Documentation](https://matink112.github.io/drf-batch/)** ·
**[Cookbook](https://github.com/matink112/drf-batch-cookbook)** ·
[Changelog](CHANGELOG.md) ·
[Contributing](#contributing)

`drf-batch` collects every database and service lookup a request needs
**once**, validates the whole payload in memory, and commits it all-or-none in
a single transaction, using ordinary DRF serializers and viewsets.

It is to the DRF write path what `prefetch_related` is to the read path, and
what DataLoader is to GraphQL.

```text
POST /orders/batch/   →   201 {"count": 500}
POST /orders/batch/   →   400 [{}, {"ticker": ["Ticker 'XYZ' not found."]}]
```

> **Extracted from production.** `drf-batch` comes from the trading backend
> of a large fintech company, where its author designed and built it to
> validate and commit high-volume batches of orders against customers,
> instruments and live risk services. Those were batches where a partial
> write or a 500 in the middle of a batch was not acceptable. This package is
> the generalized, open-source release of that mechanism, with the domain
> logic removed.

## Why drf-batch

Take a 100-row payload with 2 foreign keys and 1 external service check per
row:

| | Lookups | Writes | One bad row |
|---|---|---|---|
| **DRF `many=True`** | 200 `SELECT`s + 100 service calls | 100 `INSERT`s | earlier rows may already be committed, unless you wrap it yourself |
| **Hand-rolled prefetch in `context`** | 2 `SELECT`s + 1 call, wired per view | up to you | a malformed key in `filter(id__in=...)` can turn into a `500` |
| **`drf-batch`** | **2 `SELECT`s + 1 batched call, run concurrently** | **1 `bulk_create`** | **index-aligned `400`, nothing written** |

The cost of I/O stops depending on the number of rows. Request latency
becomes:

```text
latency ≈ longest fetch chain  +  in-memory validation  +  bulk write
```

This is the gap described in
[DRF #4828](https://github.com/encode/django-rest-framework/issues/4828) and
in the Django forum thread on
[prefetching before validation](https://forum.djangoproject.com/t/django-nested-serializer-prefetching-before-validation/21774):
list serializers validate rows one at a time and save them one at a time.
Request-scoped batching itself is not new (GraphQL's
[DataLoader](https://github.com/graphql/dataloader) made it standard). DRF
was missing a hook to collect lookups *before* validation, along with
cross-row semantics and a single transaction. `drf-batch` adds that hook and
nothing else.

## How it compares

| | DRF `ListSerializer` | Hand-rolled `context` prefetch | `djangorestframework-bulk` | **`drf-batch`** |
|---|---|---|---|---|
| Lookups per field | one per row | one per batch, by hand | one per row | **one per batch, automatic** |
| Malformed keys | `400` per row | possible `500` | `400` per row | **`400` per row, before any query** |
| External services | N sequential calls | glue code in the view | not supported | **declarative `depends_on` graph** |
| Concurrency | none | by hand | none | **sync and `async` fetches, concurrent** |
| Atomicity | per row | by hand | per row | **all-or-none, one transaction** |
| OpenAPI | accurate | relations often lost | outdated | **`drf-spectacular` extension** |
| Status | core | your tech debt | unmaintained | **active, Python 3.11+ / Django 5.2+** |

## What you get

- **Every lookup fetched once, concurrently.** Each cached field is one query
  or one service call for the whole batch. All fetches run in one
  `asyncio.TaskGroup`: `async def` fetches run on the event loop and sync ORM
  fetches run on worker threads. Ten FK fields plus three slow services cost
  about as much as the slowest of them, not the sum.
- **`depends_on`: database and services in one dependency graph.** A service
  call can be built from database rows, and a query can be scoped by a
  service result. Each fetch starts as soon as its prerequisites finish. You
  write no collector class and no ordering code. Cycles and unknown names
  fail when the serializer class is built, not in the middle of a request.
- **Pre-query key coercion.** `CachedRelatedField` coerces raw keys to the
  lookup field's type (`1101` and `"1101"` both resolve). An unhashable or
  invalid key becomes a `400` on its own row instead of a `500` from the
  batch query.
- **One serializer for reads and writes.** You keep normal `ModelSerializer`
  ergonomics. `validate()` and `create()` receive real model instances. The
  read path renders the lookup value.
- **No I/O during validation.** Rows are validated against the fetched maps,
  and cross-row rules read `self.collected` at CPU speed.
- **Whole-batch uniqueness.** Duplicates inside the payload, and clashes with
  existing rows, return `409` before anything is written.
- **DRF authorization on every row.** Update and delete targets come from
  `get_queryset()` and must pass `check_object_permissions()`. Lookups can be
  scoped per tenant with `queryset=` or `get_queryset()`.
- **All-or-none bulk writes.** `bulk_create` and grouped `bulk_update` by
  default. Switch to per-row saves when you need signals or `save()`
  overrides. Retries are optional.
- **One route, four DRF actions.** `/<resource>/batch/` with `POST`, `PATCH`,
  `DELETE` and opt-in `PUT`. Each has its own action name, so permissions and
  throttles can tell them apart.
- **Errors your clients already understand.** Index-aligned, in the same
  shape as `many=True`.
- **Production-grade project.** Supports WSGI and ASGI, ships type hints
  (`py.typed`, checked by mypy) and an OpenAPI extension for
  `drf-spectacular`. CI runs Hypothesis property tests, a coverage gate,
  PostgreSQL, and a matrix of Python 3.11–3.14 × Django 5.2–6.1 × DRF 3.15+.

## Installation

```bash
pip install drf-batch
pip install "drf-batch[spectacular]"   # with OpenAPI support
```

**Requirements:** Python 3.11+, Django 5.2+, Django REST Framework 3.15+.

## Quickstart

### 1. Models

```python
from django.db import models


class Ticker(models.Model):
    isin = models.CharField(max_length=32, unique=True)


class Customer(models.Model):
    isin = models.CharField(max_length=32, unique=True)


class Order(models.Model):
    ticker = models.ForeignKey(Ticker, on_delete=models.CASCADE)
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE)
    volume = models.IntegerField()
```

### 2. Serializer

No collector class is needed. `CachedRelatedField` reads `Order.ticker` and
`Order.customer`, deduplicates the keys across the whole payload, fetches each
table in one query, and passes model instances to `validate()` and `create()`:

```python
from drf_batch import BatchSerializer, CachedRelatedField
from rest_framework import serializers

from .models import Order


class OrderSerializer(BatchSerializer):
    ticker = CachedRelatedField(lookup="isin", error="Ticker '{value}' not found.")
    customer = CachedRelatedField(lookup="isin", error="Customer '{value}' not found.")
    volume = serializers.IntegerField(min_value=1)

    class Meta:
        model = Order
        fields = ("id", "ticker", "customer", "volume")
```

### 3. ViewSet and router

```python
from drf_batch import BatchMixin
from rest_framework.routers import DefaultRouter
from rest_framework.viewsets import ModelViewSet

from .models import Order
from .serializers import OrderSerializer


class OrderViewSet(BatchMixin, ModelViewSet):
    queryset = Order.objects.all()
    serializer_class = OrderSerializer


router = DefaultRouter()
router.register("orders", OrderViewSet, basename="order")
urlpatterns = router.urls
```

`/orders/batch/` now accepts `POST` (create), `PATCH` (partial update by pk)
and `DELETE` (delete by pk), next to the usual single-item routes. Full-row
`PUT` is opt-in: `batch_actions = ("create", "update", "replace", "destroy")`.

The [Quickstart guide](https://matink112.github.io/drf-batch/quickstart/)
continues with updates, conflicts and permissions.

## External services and `depends_on`

Real validation spans databases, caches and internal services. Declare
`CachedServiceField` next to `CachedRelatedField` and wire them together in
either direction (DB → service or service → DB). Every fetch starts as soon
as the fetches it depends on have finished:

```text
Time ─────────────────────────────────────────────────────────────►
f1 (Redis price bands)     ██████
f2 (DB customers)          ████████████
f3 (DB tradable tickers)         ████                 ← waits for f1 only
f4 (HTTP credit limits)                ██████         ← waits for f2 only
f5 (auth check)            ████████
f6 (token validation)              ██████             ← waits for f5 only
```

```python
import httpx
from django.core.cache import cache
from drf_batch import BatchSerializer, CachedRelatedField, CachedServiceField
from rest_framework import serializers

from .models import Order


class PriceBandField(CachedServiceField):
    """Service: price bands from Redis, keyed by the row's ticker ISIN."""

    def fetch(self, keys, collected, context):
        raw = cache.get_many([f"band:{isin}" for isin in keys])
        return {key.removeprefix("band:"): band for key, band in raw.items()}


class TradableTickerField(CachedRelatedField):
    """Service -> DB: only load tickers that have a price band."""

    def fetch(self, keys, collected, context):
        bands = collected.get("band", {})
        return super().fetch(
            {isin for isin in keys if isin in bands}, collected, context
        )


class CreditLimitField(CachedServiceField):
    """DB -> service: one HTTP call for every known customer in the batch."""

    def fetch(self, keys, collected, context):
        customers = collected.get("customer", {})
        known = [isin for isin in keys if isin in customers]
        if not known:
            return {}
        response = httpx.post(
            "http://risk.internal/api/v1/limits/batch",
            json={"customers": known},
            timeout=1.5,
        )
        response.raise_for_status()
        return response.json()["limits"]


class OrderWithRiskSerializer(BatchSerializer):
    band = PriceBandField(derived_from="ticker", read_only=True)
    customer = CachedRelatedField(lookup="isin", error="Customer '{value}' not found.")
    ticker = TradableTickerField(
        lookup="isin",
        depends_on=("band",),
        error="Ticker '{value}' is not tradable.",
    )
    credit_limit = CreditLimitField(
        derived_from="customer",
        depends_on=("customer",),
        read_only=True,
    )
    volume = serializers.IntegerField(min_value=1)

    class Meta:
        model = Order
        fields = ("id", "ticker", "customer", "band", "credit_limit", "volume")

    def validate(self, attrs):
        # Zero I/O: the limit was fetched once for the whole batch.
        if attrs["volume"] > self.cached("credit_limit"):
            raise serializers.ValidationError({"volume": ["Exceeds the credit limit."]})
        return attrs
```

Fetches can also be `async def`. The full rules for threads, transactions and
timeouts are in the
[collection guide](https://matink112.github.io/drf-batch/guide/collection/).

## Documentation

The full documentation is at **https://matink112.github.io/drf-batch/**.

| Guide | Reference |
|---|---|
| [Quickstart](https://matink112.github.io/drf-batch/quickstart/) | [How it works (pipeline)](https://matink112.github.io/drf-batch/reference/pipeline/) |
| [Collection and cached fields](https://matink112.github.io/drf-batch/guide/collection/) | [Settings and view options](https://matink112.github.io/drf-batch/reference/settings/) |
| [Async collection](https://matink112.github.io/drf-batch/guide/async/) | [Error contract](https://matink112.github.io/drf-batch/reference/errors/) |
| [Permissions and scoping](https://matink112.github.io/drf-batch/guide/permissions/) | [API reference](https://matink112.github.io/drf-batch/reference/api/) |
| [Write path](https://matink112.github.io/drf-batch/guide/write-path/) · [Conflicts](https://matink112.github.io/drf-batch/guide/conflicts/) | [Changelog](CHANGELOG.md) |
| [Hooks](https://matink112.github.io/drf-batch/guide/hooks/) · [Retry](https://matink112.github.io/drf-batch/guide/retry/) · [OpenAPI](https://matink112.github.io/drf-batch/guide/openapi/) | [Provenance](docs/PROVENANCE.md) |

## Cookbook

[**drf-batch-cookbook**](https://github.com/matink112/drf-batch-cookbook) has
**19 runnable scenarios across 6 domains**, each with tests and `curl`
commands:

- **Dependency graphs across boundaries:** scope database queries by Redis
  price bands (service → DB), or batch-call an HTTP risk engine with database
  attributes (DB → service).
- **Cross-row validation:** uniqueness inside the payload, aggregate volume
  caps, and batch-wide invariants, all checked in memory.
- **Request-scoped multi-tenancy:** restrict lookups to the authenticated
  tenant with `queryset=` or `get_queryset()`.
- **Custom sync and async collectors:** full-replacement collectors for
  multi-service pipelines.

## Scope

| In scope | Out of scope, by design |
|---|---|
| Batch-level lookup collection (sync and async) | Vendor-specific SQL |
| In-memory row and cross-row validation | Domain-specific business rules |
| `CachedRelatedField` with pre-query coercion | Custom storage engines |
| `CachedServiceField` and the `depends_on` graph | Opinionated HTTP / gRPC / Redis clients |
| All-or-none `POST` / `PATCH` / `PUT` / `DELETE` | Partial commits, background queues |
| `drf-spectacular` schema extension | Read-path (`GET`) query optimization |

## Contributing

`drf-batch` is young and actively maintained, and contributions are very
welcome. These help most:

- **Cookbook recipes** from your own domain: e-commerce, logistics,
  healthcare, anything with batch writes.
- **Bug reports with a failing test**, especially edge cases on PostgreSQL,
  MySQL or under ASGI.
- **Docs and benchmarks:** clearer explanations and reproducible numbers.
- **Integrations:** collectors and examples for other services and clients.

Getting started takes three commands:

```bash
uv sync          # dev environment
nox -s all       # lint, type-check and tests, the same as CI
make help        # every other shortcut
```

Commits follow [Conventional Commits](https://www.conventionalcommits.org/).
See [CONTRIBUTING.md](CONTRIBUTING.md) for the full workflow.

**Running `drf-batch` in production?** Please
[open an issue](https://github.com/matink112/drf-batch/issues) and tell us
about it. Real-world feedback decides the roadmap.

## License

BSD-3-Clause. See [LICENSE](LICENSE).
