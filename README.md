# drf-batch

Batch-aware validation for Django REST Framework: collect every lookup a
request needs **once**, validate the whole payload against memory, and commit
all-or-none.

`drf-batch` is to the write path what `drf-serializer-prefetch` is to the read
path, and what DataLoader is to GraphQL.

**Documentation:** guides and the API reference are in
[`docs/`](docs/index.md) (build with `nox -s docs`).

```json
POST /orders/batch/   →   201 {"count": 2}
POST /orders/batch/   →   400 [{}, {"ticker": ["Ticker 'XYZ' not found."]}]
```

## Highlights

```text
request latency ≈ longest fetch chain  +  in-memory validation  +  bulk write
```

- **Every lookup is fetched once, concurrently.** Each cached field is one
  query or one service call for the whole batch, no matter how many rows.
  All fetches run as one `asyncio.TaskGroup`: `async def` fetches on the
  event loop, sync ORM fetches on worker threads
  ([caveats](#transactions)). Ten FK fields plus three
  slow services cost about as much as the slowest of them, not the sum.
- **`depends_on`: fetches as a dependency graph**
  ([details](#concurrency-and-depends_on)). Declare which maps a fetch
  reads, and it starts the moment those are ready, while unrelated fetches
  keep running. A service call can be built from database rows, and a
  database query can be scoped by a service result, with no collector class
  and no ordering code. Cycles and unknown names fail at serializer
  construction, not mid-request.
- **Database and services in one graph.** `CachedRelatedField` and
  `CachedServiceField` (sync or `async`) are both nodes in the same graph,
  and both are validated the same way (a miss is a 400 row error).
- **Validation does no I/O.** Rows are validated against the fetched maps, so
  `validate()` receives real model instances and can apply cross-row rules
  through `self.collected` at CPU speed.
- **Whole-batch uniqueness.** Duplicates inside the payload and clashes with
  existing rows are a 409 before anything is written.
- **All-or-none bulk writes.** `bulk_create` and grouped `bulk_update` by
  default, per-row when you need signals and `save()` overrides, optionally
  retried. One invalid row rejects the whole batch.
- **DRF's authorization rules on every row.** Update and delete targets come
  from `get_queryset()` and pass `check_object_permissions()`; lookups can be
  scoped with `queryset=` / `get_queryset()`.
- **One route, four DRF actions.** `BatchMixin` adds `/<resource>/batch/`
  with `POST` (create), `PATCH` (partial update by pk), `PUT` (full update by
  pk, opt-in) and `DELETE` (delete by pk), so permissions and throttles can
  treat each one separately.
- **Index-aligned errors** that existing `many=True` clients already
  understand.
- **WSGI and ASGI, typed, with OpenAPI** through drf-spectacular.

## The gap

DRF's list serializers validate each row individually, then save one by one:
an invalid row late in the payload still leaves earlier rows committed, and
every row re-runs the same lookups. This gap is documented in
[DRF #4828](https://github.com/encode/django-rest-framework/issues/4828) and in
the unresolved forum thread on
[prefetching before validation](https://forum.djangoproject.com/t/django-nested-serializer-prefetching-before-validation/21774).

Per-request batched loading is **not new**. GraphQL's
[DataLoader](https://www.apollographql.com/tutorials/dataloaders-dgs/03-data-loaders-under-the-hood)
established batching plus request-scoped caching to kill N+1 in 2015. What is
missing from DRF is a write-path hook: a place to collect lookups *before*
validation, plus cross-row semantics and a single transaction. That is all this
package adds.

## What this is not

| In scope | Explicitly out of scope |
|---|---|
| Batch create/update/delete viewset actions | Read-path query optimisation (`drf-serializer-prefetch`) |
| Serializer-driven collection + full-replacement collector hook | Any specific data source (market data, gRPC, …) |
| Memory-backed relation fields during validation | Event emission / Kafka integrations |
| Sync **and** async collection, all-or-none writes | Bulk insert with vendor-specific SQL |
| Bulk create by default (`bulk_create`, no vendor SQL) | Auto-generated bulk CRUD routes |

## Requirements

- Python 3.11+
- Django 5.2+
- Django REST Framework 3.15+
- Optional: `drf-spectacular` 0.27+ for OpenAPI schemas
  (`pip install "drf-batch[spectacular]"`)

## Quickstart

```bash
pip install drf-batch
```

### 1. Models

```python
# quickstart_app/models.py
from django.db import models


class Ticker(models.Model):
    isin = models.CharField(max_length=32, unique=True)
    name = models.CharField(max_length=256)


class Customer(models.Model):
    isin = models.CharField(max_length=32, unique=True)
    name = models.CharField(max_length=256)


class Order(models.Model):
    ticker = models.ForeignKey(Ticker, on_delete=models.CASCADE)
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE)
    volume = models.PositiveIntegerField()
    reference_id = models.CharField(max_length=64)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["ticker", "customer"], name="unique_ticker_customer"
            ),
        ]
```

### 2. Serializer — declare the cached fields, nothing else

```python
# quickstart_app/serializers.py
from drf_batch import BatchSerializer, CachedRelatedField
from rest_framework import serializers

from .models import Order


class OrderSerializer(BatchSerializer):
    ticker = CachedRelatedField(lookup="isin", error="Ticker '{value}' not found.")
    customer = CachedRelatedField(lookup="isin", error="Customer '{value}' not found.")
    volume = serializers.IntegerField(min_value=1)
    reference_id = serializers.CharField(max_length=64)

    class Meta:
        model = Order
        fields = ("id", "ticker", "customer", "volume", "reference_id")
```

**No collector class is needed.** The list serializer auto-collects: it
derives the lookup model from the same-named ForeignKey on `Meta.model`,
coerces every row's key to the lookup's type (`1101` and `"1101"` both
resolve a `CharField` lookup; a malformed key is a 400 row error, not a 500),
fetches one query per field, and validates all rows against the results.
The map is named after the field (`collected["ticker"]`); pass `map=` to
override, `model=`/`lookup=` for non-FK lookups, and `depends_on=(...)` to
order fetches (see below).

A hit returns the **model instance**, so `create()` receives an FK instance
and your own `validate()` can inspect it (`data["ticker"].isin`) without
touching the database.


Like DRF's `RelatedField`, a `CachedRelatedField` can be scoped so a client
cannot reference rows it may not see: pass `queryset=` or override
`get_queryset()` (`self.context["request"]` is available). The same field
renders the lookup value (`"1101"`) on the read path, so one serializer can
serve `list`/`retrieve` and the batch route.

### 3. View and URLs

```python
# quickstart_app/views.py
from drf_batch import BatchMixin
from rest_framework.authentication import BasicAuthentication, SessionAuthentication
from rest_framework.permissions import IsAuthenticated
from rest_framework.viewsets import GenericViewSet

from .models import Order
from .serializers import OrderSerializer


class OrderViewSet(BatchMixin, GenericViewSet):
    queryset = Order.objects.all()
    batch_serializer_class = OrderSerializer
    authentication_classes = [BasicAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]
```

```python
# urls.py
from rest_framework.routers import DefaultRouter

from quickstart_app.views import OrderViewSet

router = DefaultRouter()
router.register("orders", OrderViewSet, basename="order")

urlpatterns = router.urls
```

`BatchMixin` adds one route, `/orders/batch/` (URL name `order-batch`), with
DRF actions:

| Method | `self.action` | Operation |
|---|---|---|
| `POST` | `batch_create` | create rows |
| `PATCH` | `batch_update` | partial update by primary key |
| `PUT` | `batch_replace` | full update by primary key (opt-in, see [Batch update](#batch-update)) |
| `DELETE` | `batch_destroy` | delete by primary key |

The distinct action names mean `get_permissions()`, `get_throttles()` and
`ScopedRateThrottle` can treat them differently, as with any other action.
Other methods on the route return `405`. The mixin also works next to DRF's
own mixins (`ModelViewSet`, `ReadOnlyModelViewSet`, …). Setting
`serializer_class = OrderSerializer` instead of `batch_serializer_class`
serves both the read path and the batch route.

### 4. Call it

```bash
curl -X POST http://localhost:8000/orders/batch/ \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Basic ...' \
  -d '[
    {"ticker": "1101", "customer": "112001", "volume": 10, "reference_id": "REF-1"},
    {"ticker": 1102,   "customer": "112001", "volume": 5,  "reference_id": "REF-2"}
  ]'
```

```json
{"count": 2}
```

An invalid row rejects the **whole** batch, with errors aligned to the input
indexes:

```json
[
  {},
  {"ticker": ["Ticker 'XYZ' not found."]}
]
```

## Permissions and scoping

The batch route follows the same authorization rules as DRF's detail routes:

- **Update and delete targets come from `get_queryset()`.** A pk outside the
  view's queryset is a `404`, exactly like a pk that does not exist, so
  ownership and tenant filters in `get_queryset()` apply to batches too.
- **Every target passes `check_object_permissions()`.** A target that fails a
  permission's `has_object_permission()` makes the batch a `403`, with the
  permission's message on that row:

  ```json
  [{}, {"non_field_errors": ["Orders with volume 10 are frozen."]}]
  ```

- Rows are validated only after both checks pass, so a caller never learns
  validation details about rows it cannot modify.
- Cached lookups are scoped with `CachedRelatedField(queryset=...)` (see the
  serializer section).

```python
class OwnedOrderViewSet(BatchMixin, GenericViewSet):
    batch_serializer_class = OrderSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Order.objects.filter(owner=self.request.user)

    def perform_batch_create(self, serializer):
        serializer.save(owner=self.request.user)
```

## Hooks and responses

The write runs through `perform_batch_create(serializer)`,
`perform_batch_update(serializer)`, `perform_batch_replace(serializer)` and
`perform_batch_destroy(serializer)`, mirroring DRF's `perform_create` & co.
`perform_batch_replace` (PUT) calls `perform_batch_update` by default, so one
update hook covers both. The default calls
`serializer.save()`. Keyword arguments to `save()` are merged into every row
(`serializer.save(owner=request.user)`). To run work after commit (events,
cache invalidation), use `transaction.on_commit` inside the hook.

The success body is `{"count": N}` by default (`201` for create, `200`
otherwise). Set `batch_response = "data"` to return the serialized rows
(create/update) or the deleted pks (destroy). Override
`get_batch_response(serializer)` for anything else.

`OrderSerializer(data=rows, many=True)` builds a `BatchListSerializer`, so the
pipeline also works outside a viewset:

```python
serializer = OrderSerializer(data=rows, many=True, context={"request": request})
serializer.is_valid(raise_exception=True)
serializer.save()
```

Pass `batch_config=BatchConfig(...)`, or any `BatchConfig` field as a keyword
(`operation="update"`, `use_bulk=False`, `queryset=...`), to configure it.

## Service cache fields, together with model cache fields

Not every lookup is a database row. `CachedServiceField` fetches its map with
your own callable (sync or `async def`), and `depends_on` orders fetches so a
service fetch can consume database results — and a database fetch can consume
service results. Both directions, no collector class:

```python
# quickstart_app/serializers.py
from drf_batch import BatchSerializer, CachedRelatedField, CachedServiceField
from rest_framework import serializers

from .models import Order, Ticker

# A mocked third-party "exchange listing" service — in a real app the fetch
# would be an HTTP/gRPC call; a dict plays the external system.
_LISTINGS = {"Alpha Corp": "A", "Beta Corp": "B"}  # ticker name -> band


def fetch_bands(keys, collected, context):
    # Service-after-DB: build the {band: [isins]} map from the ticker query.
    bands = {}
    for isin, ticker in collected.get("ticker", {}).items():
        band = _LISTINGS.get(ticker.name)
        if band is not None:
            bands.setdefault(band, []).append(isin)
    return bands


class ExchangeScopedTickerField(CachedRelatedField):
    # DB-after-service: only fetch tickers the service listed on exchange A.
    def fetch(self, keys, collected, context):
        allowed = set(collected.get("band", {}).get("A", ()))
        return Ticker.objects.filter(isin__in=(keys & allowed)).in_bulk("isin")


class ServiceOrderSerializer(BatchSerializer):
    # Fetch order (via depends_on):
    #   1. ticker, customer -> DB, concurrently (no dependencies)
    #   2. band             -> service, as soon as ticker is done
    #   3. exchange_ticker  -> DB again, scoped to the service's band "A"
    ticker = CachedRelatedField(lookup="isin", error="Ticker '{value}' not found.")
    customer = CachedRelatedField(lookup="isin", error="Customer '{value}' not found.")
    band = CachedServiceField(fetch=fetch_bands, depends_on=("ticker",), read_only=True)
    exchange_ticker = ExchangeScopedTickerField(
        model=Ticker,
        lookup="isin",
        depends_on=("band",),
        read_only=True,
        error="Ticker '{value}' is not listed on exchange A.",
    )
    volume = serializers.IntegerField(min_value=1)
    reference_id = serializers.CharField(max_length=64)

    class Meta:
        model = Order
        fields = (
            "id",
            "ticker",
            "customer",
            "band",
            "exchange_ticker",
            "volume",
            "reference_id",
        )

    def validate(self, attrs):
        # Cross-row rule that consumes the service result (zero I/O):
        # band "B" tickers are suspended.
        if attrs["ticker"].isin in self.collected.get("band", {}).get("B", []):
            raise serializers.ValidationError(
                {"ticker": ["Ticker is suspended on exchange B."]}
            )
        return attrs
```

```bash
curl -X POST http://localhost:8000/service-orders/batch/ \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Basic ...' \
  -d '[
    {"ticker": "1101", "customer": "112002", "band": "A",
     "exchange_ticker": "1101", "volume": 3, "reference_id": "SVC-1"}
  ]'
```

```json
{"count": 1}
```

- `fetch(keys, collected, context)` receives the coerced key set for this
  field, the maps of its `depends_on` (and anything else already done), and
  the serializer context; return a `{key: value}` dict. A service fetch can
  therefore build its map from the database results (`collected["ticker"]`),
  and a (subclassed) database fetch can scope its query to the service
  results.
- A cycle (or an unknown `depends_on` name) is an `ImproperlyConfigured` at
  serializer construction.
- Read-only cached fields are excluded from `validated_data` (so bulk create
  is unaffected); a row may optionally supply their value, which is checked
  against the map like any other hit/miss (a `"band": "C"` that the service
  map does not list is a 400 row error, as is an `exchange_ticker` the scoped
  fetch did not return).


## Concurrency and `depends_on`

The cached fields' fetches run as **one dependency graph** inside an
`asyncio.TaskGroup`: every fetch starts as soon as **its own** `depends_on`
are done, and fetches without a dependency between them run at the same time.
`async def` fetches run on the event loop ([which one](#which-loop-runs-collection));
sync fetches run on worker threads (or on the request thread inside a
transaction, see [below](#transactions)).

Seven fetches, where `f2` and `f3` need `f1`, `f4` needs `f3`, and `f6` needs
`f5`:

```python
class PortfolioSerializer(BatchSerializer):
    f1 = CachedServiceField(fetch=fetch_1, read_only=True)
    f2 = CachedServiceField(fetch=fetch_2, depends_on=("f1",), read_only=True)
    f3 = CachedServiceField(fetch=fetch_3, depends_on=("f1",), read_only=True)
    f4 = CachedServiceField(fetch=fetch_4, depends_on=("f3",), read_only=True)
    f5 = CachedServiceField(fetch=fetch_5, read_only=True)
    f6 = CachedServiceField(fetch=fetch_6, depends_on=("f5",), read_only=True)
    f7 = CachedServiceField(fetch=fetch_7, read_only=True)
```

They run as `TaskGroup(f1 → TaskGroup(f2, f3 → f4), f5 → f6, f7)`:

```
time ──────────────────────────────────────────────▶
f1  ██████
f2        ████████████
f3        ████
f4            ██████        ← starts when f3 ends; does not wait for f2
f5  ████████
f6          ██████          ← starts when f5 ends
f7  ██████████
```

The request waits for the longest path (`f1 → f3 → f4` or `f1 → f2` here),
not for the sum of all seven fetches. `collected` (and `self.collected`)
still lists the maps in plan order (`f1`, `f2`, … `f7`), whatever order they
finished in.

### Every dependency is a wait

!!! tip "Long chains slow collection down"
    `depends_on` serializes fetches: each edge makes one fetch wait for
    another, so a deep chain gives up the concurrency. Declare a dependency
    only when the fetch really reads that map, and keep the graph shallow.

```python
# A chain: four round-trips one after another (latency = a + b + c + d).
a = CachedServiceField(fetch=fetch_a, read_only=True)
b = CachedServiceField(fetch=fetch_b, depends_on=("a",), read_only=True)
c = CachedServiceField(fetch=fetch_c, depends_on=("b",), read_only=True)
d = CachedServiceField(fetch=fetch_d, depends_on=("c",), read_only=True)

# Only d really needs the others: three concurrent fetches, then one
# (latency = max(a, b, c) + d).
a = CachedServiceField(fetch=fetch_a, read_only=True)
b = CachedServiceField(fetch=fetch_b, read_only=True)
c = CachedServiceField(fetch=fetch_c, read_only=True)
d = CachedServiceField(fetch=fetch_d, depends_on=("a", "b", "c"), read_only=True)
```

### What a fetch sees in `collected`

A fetch receives a **snapshot** of the maps finished when it starts. It
always holds its `depends_on` maps (and update/delete targets); a map that
is not a declared dependency may or may not be there yet, so read only what
you declared:

```python
def fetch_band(keys, collected, context):
    tickers = collected["ticker"]  # guaranteed: declared below
    # collected.get("customer") may be missing: it is not a dependency
    return {isin: band_of(ticker) for isin, ticker in tickers.items()}


band = CachedServiceField(fetch=fetch_band, depends_on=("ticker",), read_only=True)
```

Writing to the snapshot has no effect on the result; return the map instead.

### Transactions

Worker threads use their own database connections, which cannot see a
transaction's uncommitted writes. So when the request's connection is in a
transaction (`ATOMIC_REQUESTS = True`, an outer `atomic()`, Django's
`TestCase`), or a database is a private in-memory SQLite database, sync
fetches run **on the request thread**, one at a time, while it waits for the
graph. They see the transaction; `async def` fetches
stay concurrent. Details: [Database connections](#database-connections).

Collection only reads, but those reads must see what the request's
transaction sees. A worker thread's connection is a **separate
transaction**, so it would:

- miss rows written earlier in the same transaction (by the view, a hook,
  middleware, or a `TestCase` fixture): a valid key becomes a "not found"
  row error;
- block on rows the request locked with `select_for_update()`, while the
  request waits for it: a deadlock;
- read a different snapshot under `REPEATABLE READ` / `SERIALIZABLE`.

One connection cannot run queries in parallel, so seeing the transaction
means running sync fetches one after another.

**What it costs.** `scripts/bench_collection.py` (100 rows, N sync fetches,
each one query plus a simulated round-trip):

| Sync fetches | Round-trip | Outside a transaction | Inside |
|---|---|---|---|
| 8 | 1 ms (local, indexed DB) | 13.6 ms | 13.8 ms |
| 4 | 5 ms | 10.7 ms | 31.2 ms |
| 8 | 20 ms | 33.6 ms | 180.5 ms |

Fast database lookups cost the same either way; slow **sync** I/O adds up.
If that matters:

- **Slow services: write the fetch as `async def`** with an async client
  ([Async clients](#async-clients)). Async fetches stay concurrent inside a
  transaction.
- **Or take the batch view out of `ATOMIC_REQUESTS`**, when nothing in the
  request writes or locks before collection. Decorating an action method
  has no effect; mark `dispatch`, on a viewset that serves only the batch
  route (it applies to every action of the viewset):

  ```python
  from django.db import transaction


  class OrderBatchViewSet(BatchMixin, GenericViewSet):
      batch_serializer_class = OrderSerializer

      @transaction.non_atomic_requests
      def dispatch(self, *args, **kwargs):
          return super().dispatch(*args, **kwargs)
  ```

  The batch write stays all-or-none: it runs in its own `atomic()`.

### Turning concurrency off

`batch_concurrent_collection = False` runs the fetches one by one in plan
order: sync fetches inline on the request thread, each `async def` fetch
bridged to the loop on its own, all under one `COLLECT_TIMEOUT`. No loop is
started for an all-sync serializer.

```python
class OrderViewSet(BatchMixin, GenericViewSet):
    batch_serializer_class = OrderSerializer
    batch_concurrent_collection = False
```

Outside a viewset, pass it as a keyword:

```python
serializer = OrderSerializer(data=rows, many=True, concurrent_collection=False)
```

## Full control: BatchCollector

Auto collection covers the common case. When the collection itself is
unusual (one query across sources, cache-first, …), set
`batch_collector_class`. The collector is a **full-replacement hook**: its
`collect(keys, context)` runs *instead of* auto collection.

```python
# quickstart_app/collectors.py
from drf_batch import BatchCollector, BatchContext

from .models import Customer, Ticker


class OrderCollector(BatchCollector):
    def collect(self, keys: dict[str, set], context: BatchContext) -> dict:
        # keys are the coerced key sets derived from the serializer's cached
        # fields ({"ticker": {...}, "customer": {...}}), never raw rows.
        # Return EVERY map the cached fields need; a missing map means every
        # row gets a miss error for it.
        return {
            "ticker": Ticker.objects.filter(isin__in=keys.get("ticker", set())).in_bulk(
                "isin"
            ),
            "customer": Customer.objects.filter(
                isin__in=keys.get("customer", set())
            ).in_bulk("isin"),
        }


class OrderViewSet(BatchMixin, GenericViewSet):
    batch_collector_class = OrderCollector
    ...
```

Malformed keys never reach your code: they are rejected in the extraction
pass before `collect` runs, so a malformed batch is a 400 with zero
collection work. Update and delete targets are **not** the collector's job.
The pipeline resolves them from the view's queryset first and exposes them as
`collected["id"]` (the pk field name).

## Async collection

Any fetch can be `async def`. It is detected automatically and needs no view
changes:

```python
# quickstart_app/serializers.py
import asyncio


async def fetch_bands(keys, collected, context):
    await asyncio.sleep(0)  # your async driver call here
    return {t.isin: "A" for t in collected.get("ticker", {}).values()}


class OrderSerializer(BatchSerializer):
    ticker = CachedRelatedField(lookup="isin", error="Ticker '{value}' not found.")
    customer = CachedRelatedField(lookup="isin", error="Customer '{value}' not found.")
    band = CachedServiceField(fetch=fetch_bands, depends_on=("ticker",), read_only=True)
    volume = serializers.IntegerField(min_value=1)
    reference_id = serializers.CharField(max_length=64)

    class Meta:
        model = Order
        fields = ("id", "ticker", "customer", "band", "volume", "reference_id")
```

Sync and async fetches run together as one dependency graph on an event loop
(see [Concurrency and `depends_on`](#concurrency-and-depends_on)):
`ticker` and `customer` start together, and `band` starts as soon as `ticker`
is done.

### Timeouts

Collection that does not finish within `COLLECT_TIMEOUT` (default 10.0 s) is
cancelled and the request gets a **504** (`BatchCollectionTimeout`). The
whole graph shares one timeout; a replacement collector overrides it with
its `collect_timeout` class attribute. Any *other* exception a fetch raises
propagates unchanged (the remaining fetches are cancelled) and becomes a
**500**.

```python
# settings.py
DRF_BATCH = {"COLLECT_TIMEOUT": 2.5}
```

!!! note
    An `async def` fetch is cancelled at its next `await`. A sync fetch
    already running on a thread cannot be interrupted: the request still gets
    its 504 on time, and the call finishes in the background.

### Which loop runs collection

DRF dispatches views synchronously under WSGI *and* ASGI, so the request
thread hands the collection graph to an event loop running in **another**
thread and waits for it. The `EVENT_LOOP` setting picks the loop:

| `EVENT_LOOP` | Under ASGI | Anywhere else |
|---|---|---|
| `"auto"` (default) | the **ASGI server's loop** | thread mode |
| `"thread"` | thread mode | thread mode |

**Thread mode** uses a loop your application already runs in its own thread
(registered with `use_loop()` or `THREAD_LOOP`, below) or, by default, the
built-in `drf-batch-loop` thread: one daemon thread with one event loop for
the process, created on first use (no thread exists at import time).

```python
# settings.py
DRF_BATCH = {
    "EVENT_LOOP": "auto",  # or "thread": never use the ASGI server's loop
}
```

#### Under ASGI

A sync view under ASGI runs in an `asgiref` worker thread, which knows the
server loop that spawned it. With `"auto"`, collection runs **on that loop**,
the same one your async views and their clients use. Nothing to configure.

!!! warning "A blocking fetch stalls the whole server"
    On the server's loop, an `async def` fetch that blocks (a sync HTTP
    call, `time.sleep`, a sync ORM query) stops *every* request the server
    is handling. Choose `EVENT_LOOP = "thread"` to keep collection on its own
    loop if you cannot rule that out. See [Don't block the loop](#dont-block-the-loop).

#### Your own loop, in your own thread

If the application already runs a loop in a background thread (with its
clients and pools created on it), register it and collection uses it in
thread mode. drf-batch never starts, stops or closes it.

```python
# quickstart_app/runtime.py
import asyncio
import threading

import aiohttp

io_loop = asyncio.new_event_loop()
threading.Thread(target=io_loop.run_forever, name="app-io", daemon=True).start()


async def _make_session() -> aiohttp.ClientSession:
    # A ClientSession must be created on the loop that will use it.
    return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2))


http = asyncio.run_coroutine_threadsafe(_make_session(), io_loop).result()
```

```python
# quickstart_app/apps.py
from django.apps import AppConfig


class QuickstartConfig(AppConfig):
    name = "quickstart_app"

    def ready(self):
        from drf_batch.loop import use_loop

        from .runtime import io_loop

        use_loop(io_loop)  # use_loop(None) goes back to the built-in thread
```

```python
# quickstart_app/serializers.py
from .runtime import http


async def fetch_bands(keys, collected, context):
    async with http.get("https://bands.example/v1", params={"isin": sorted(keys)}) as r:
        r.raise_for_status()
        return await r.json()
```

Or name a zero-argument callable that returns the loop in settings. A lazy
getter like this one also starts a fresh loop in each forked worker process:

```python
# quickstart_app/runtime.py
import asyncio
import os
import threading

_lock = threading.Lock()
_loop: asyncio.AbstractEventLoop | None = None
_pid = 0


def get_io_loop() -> asyncio.AbstractEventLoop:
    global _loop, _pid
    with _lock:
        if _loop is None or _pid != os.getpid():  # first use, or a forked child
            _loop, _pid = asyncio.new_event_loop(), os.getpid()
            threading.Thread(
                target=_loop.run_forever, name="app-io", daemon=True
            ).start()
    return _loop
```

```python
# settings.py
DRF_BATCH = {"THREAD_LOOP": "quickstart_app.runtime.get_io_loop"}
```

`use_loop()` takes precedence over `THREAD_LOOP`. A registered loop that is
not running is an `ImproperlyConfigured` on the first request, not a 504.
`drf_batch.loop.current_loop()` returns the loop collection would use from the
calling thread.

### Async clients

An async client (`aiohttp.ClientSession`, `httpx.AsyncClient`, an `asyncpg`
pool) is **bound to the loop it was created on**: using it from another loop
fails or hangs. Create it on the loop that runs your fetches, reuse it across
requests, and never share one across loops. A per-loop cache works in every
mode (ASGI server loop, your own loop, the built-in thread):

```python
# quickstart_app/clients.py
import asyncio

import httpx

_clients: dict[asyncio.AbstractEventLoop, httpx.AsyncClient] = {}


def client() -> httpx.AsyncClient:
    """The client for the running loop, created on first use."""
    loop = asyncio.get_running_loop()
    found = _clients.get(loop)
    if found is None or found.is_closed:
        found = _clients[loop] = httpx.AsyncClient(timeout=2.0)
    return found


async def close_clients() -> None:
    """Await on each loop at shutdown (e.g. from your own loop's thread)."""
    loop = asyncio.get_running_loop()
    if (found := _clients.pop(loop, None)) is not None:
        await found.aclose()
```

```python
# quickstart_app/serializers.py
from .clients import client


async def fetch_prices(keys, collected, context):
    response = await client().post("https://prices.example/v1/batch", json=sorted(keys))
    response.raise_for_status()
    return response.json()  # {isin: price}
```

!!! tip
    Do not create a client per fetch call (`async with httpx.AsyncClient()`
    inside the fetch): it works, but every batch pays for new connections
    and TLS handshakes.

### Don't block the loop

Everything that shares a loop waits while one coroutine blocks it: with the
built-in thread that is every batch request in the process, under ASGI the
whole server.

```python
import requests


# Bad: requests.get blocks the event loop thread.
async def fetch_bands(keys, collected, context):
    return requests.get(
        "https://bands.example/v1", params={"isin": sorted(keys)}
    ).json()


# Good: a plain sync fetch. The graph runs it on a worker thread for you.
def fetch_bands(keys, collected, context):
    return requests.get(
        "https://bands.example/v1", params={"isin": sorted(keys)}
    ).json()


# Good: blocking work inside an async fetch goes to a thread explicitly.
from drf_batch.loop import run_sync


async def fetch_bands(keys, collected, context):
    tickers = await run_sync(lambda: list(Ticker.objects.filter(isin__in=keys)))
    prices = await client().post("https://prices.example/v1/batch", json=sorted(keys))
    return {t.isin: prices.json()[t.isin] for t in tickers}
```

### Database connections

Django connections are per thread. Where a sync fetch (or a `run_sync(...)`
call) runs depends on the request's connection:

- **Not in a transaction**: on a worker thread, concurrently with the other
  fetches, on that thread's own connection.
- **In a transaction** (`ATOMIC_REQUESTS = True`, an outer `atomic()`, Django's
  `TestCase`): on the **request thread**, which runs these calls one at a
  time while it waits for the graph, so they see the transaction's
  uncommitted writes. Async fetches stay concurrent. The same applies when a
  database is a private in-memory SQLite database (`NAME = ":memory:"`),
  where every connection would be a separate, empty database.

```python
# Inside a transaction a worker thread could not see this row; the
# request thread runs the sync ticker fetch, so the batch resolves it.
with transaction.atomic():
    Ticker.objects.create(isin="1103", name="Gamma Corp")
    rows = [
        {"ticker": "1103", "customer": "112001", "volume": 1, "reference_id": "TX-1"}
    ]
    serializer = OrderSerializer(data=rows, many=True)
    serializer.is_valid(raise_exception=True)
```

Worker threads handle their connections like a request does
(`close_old_connections()` before and after each call): with the default
`CONN_MAX_AGE = 0` a connection is closed after the call; with a positive
value it is kept and reused by that worker thread until it expires or
breaks.

```python
# settings.py: reuse worker-thread connections for up to 60 s
DATABASES = {
    "default": {
        ...,
        "CONN_MAX_AGE": 60,
        "CONN_HEALTH_CHECKS": True,
    }
}
```

With persistent connections each worker thread of the loop's thread pool
(`min(32, os.cpu_count() + 4)` threads by default) may keep one connection
per database, so count them against the server's connection limit.

In your own code inside an async fetch or collector:

| Helper | Runs on | Sees the request's transaction |
|---|---|---|
| `drf_batch.loop.run_sync(func, ...)` / `BatchCollector.run_sync` | request thread inside a transaction, else a worker thread | yes |
| `drf_batch.loop.to_thread(func, ...)` / `BatchCollector.to_thread` | always a worker thread | no |

### Calling the serializer from async code

`is_valid()` is synchronous and must not run on an event loop thread (that is
a `RuntimeError`). From an async view or task, call it through
`sync_to_async`; with `EVENT_LOOP = "auto"` collection then runs on your loop:

```python
from asgiref.sync import sync_to_async


async def import_orders(rows):
    serializer = OrderSerializer(data=rows, many=True)
    await sync_to_async(serializer.is_valid)(raise_exception=True)
    await sync_to_async(serializer.save)()
```

### Loop lifecycle

- The built-in loop starts lazily on first use and keeps running between
  requests. `drf_batch.loop.shutdown()` stops it (tests, long-running
  processes); it also runs at interpreter exit, bounded to a few seconds.
- After `fork()` (gunicorn `--preload`, Celery prefork) the built-in loop is
  recreated in the child on first use. A loop you register yourself is not:
  start it lazily per process, as `get_io_loop()` above does.
- Sequential collection (`batch_concurrent_collection = False`, see
  [Concurrency](#turning-concurrency-off)) starts no loop at all
  unless a fetch is `async def`.

## Write path

Every write runs in one `transaction.atomic()`: all-or-none.

| Operation | Bulk mode (default) | `batch_use_bulk = False` |
|---|---|---|
| create | `bulk_create` (chunked by `batch_bulk_size`) | per-row `serializer.create()` |
| update | `bulk_update`, one statement group per distinct set of changed fields | per-row `serializer.update()` |
| destroy | one queryset `delete()` (cascades apply) | per-instance `delete()` |

Trade-offs of bulk mode:

- no `pre_save`/`post_save` signals, no per-row `save()` overrides, no
  `auto_now` updates on `bulk_update`;
- every writable field must be a concrete model field (nested/writable
  non-model fields are rejected up front with `ImproperlyConfigured`);
- PKs are set on created instances on SQLite and PostgreSQL (both run in CI);
  other backends may not set them.

`batch_bulk_size` (default: the `BULK_SIZE` setting, 1000) is the number of
rows per statement. Each row binds one parameter per column, so lower it for
very wide models (PostgreSQL allows 65,535 parameters per statement).

Update and delete re-fetch their targets inside the write transaction with
`select_for_update()` (`batch_lock_targets = True`). Concurrent batches
cannot interleave, every retry attempt works on fresh instances, and a
target deleted by someone else after validation is a `404`. Set
`batch_lock_targets = False` to skip the lock and the extra query.

A database constraint the batch could not check up front (a `CheckConstraint`,
a conditional unique constraint, `on_delete=PROTECT`, or a concurrent insert
racing the conflict check) rolls the batch back and returns a **409**
`{"detail": "The batch violates a database constraint; nothing was written."}`
(code `batch_integrity_conflict`) instead of a 500.

## Batch update

`PATCH /orders/batch/` updates existing rows by primary key (any pk type:
int, UUID, slug; string values are coerced). Rows are
`{"id": <pk>, ...changed fields}` with **partial** semantics: only the
supplied writable fields change, everything else is preserved. A row with
only `id` is a valid no-op (counted, nothing written).

```bash
curl -X PATCH http://localhost:8000/orders/batch/ \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Basic ...' \
  -d '[
    {"id": 1, "volume": 50}
  ]'
```

```json
{"count": 1}
```

- Cached fields work exactly as in create: `"ticker": "1102"` is coerced,
  collected once, and validated against the map.
- A pk that does not exist, or is outside `get_queryset()`, is a **404** with
  the index-aligned body. A target that fails the object permissions is a
  **403**. Nothing is written in either case.
- Rows are validated with their target as `serializer.instance` (DRF's
  update pattern), so your `validate()` can compare against current values.

### Full rows with `PUT`

`PUT /orders/batch/` is the same update with DRF's `PUT` semantics: every row
is validated as a full object, so a missing required field is a 400 on that
row (`{"volume": ["This field is required."]}`). As with DRF's own `PUT`, an
optional field a row leaves out keeps its current value. Scoping, object
permissions, conflicts and the all-or-none write are the same as `PATCH`.

`PUT` is **off by default**: an existing `get_permissions()` that restricts
`batch_update` would not cover a new `batch_replace` action. Enable it
explicitly:

```python
class OrderViewSet(BatchMixin, GenericViewSet):
    batch_serializer_class = OrderSerializer
    batch_actions = ("create", "update", "replace", "destroy")
```

Outside a viewset, pass `partial=False`:

```python
serializer = OrderSerializer(data=rows, many=True, operation="update", partial=False)
```

## Batch delete

`DELETE /orders/batch/` takes a list of **scalar pks** (a dict or nested row
is a 400 "expected a pk value"):

```bash
curl -X DELETE http://localhost:8000/orders/batch/ \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Basic ...' \
  -d '[1, 2]'
```

```json
{"count": 2}
```

- Missing or out-of-scope pks are a **404**, forbidden targets a **403**, and
  the same pk twice in the payload is a **409**.
- An `on_delete=PROTECT` child makes the whole batch a 409 (nothing deleted).

## Cross-row conflicts (409)

The conflict pass checks uniqueness for the **whole batch** before writing:
duplicates inside the payload and clashes with existing rows are a 409, not a
per-row query or a 500 at commit time. Rules come from two places:

1. **The model, automatically.** Non-pk `unique=True` fields,
   `Meta.unique_together`, and unconditional field-only `UniqueConstraint`s
   become rules whenever all their fields are writable on the serializer.
   DRF's per-row `UniqueValidator`/`UniqueTogetherValidator` for those rules
   are removed, so validation does no per-row queries. Opt out with
   `Meta.batch_model_unique = False`.
2. **Explicitly**, for rules the model does not declare:

```python
# quickstart_app/serializers.py
class ConflictOrderSerializer(OrderSerializer):
    class Meta(OrderSerializer.Meta):
        # one rule per tuple entry: a single field name, or a composite
        # (tuple of field names). Every name must be a concrete field, an
        # FK, or the pk on Meta.model (ImproperlyConfigured otherwise).
        batch_unique_together = ("reference_id",)
```

```bash
curl -X POST http://localhost:8000/conflict-orders/batch/ \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Basic ...' \
  -d '[
    {"ticker": "1101", "customer": "112002", "volume": 1, "reference_id": "DUP"},
    {"ticker": "1102", "customer": "112003", "volume": 2, "reference_id": "DUP"}
  ]'
```

```json
[
  {"reference_id": ["Conflicts with another row in this batch."]},
  {"reference_id": ["Conflicts with another row in this batch."]}
]
```

- Two rows in the same payload with the same rule value are a **409** on
  every row of the group ("Conflicts with another row in this batch.").
- A row whose rule value matches an existing row (other than its own target,
  for updates) is a **409** ("Conflicts with an existing record.").
- `NULL` values never conflict, matching SQL `UNIQUE`.
- The check is one query per rule, against the whole table (uniqueness is not
  tenant-scoped).
- An update row that omits a rule field is checked against the field's
  current value, so a row keeping its own value never conflicts with itself.
- The status is overridable per view: `batch_conflict_status = 422`.

## Retry policy

`batch_retry_policy` wraps the write's transaction, so a transient database
failure (connection dropped, lock timeout) is retried in a **fresh
transaction**:

```python
# quickstart_app/views.py
from drf_batch import FixedRetryPolicy


class FlakyOrderViewSet(OrderViewSet):
    # 3 attempts, 0.1 s apart, retrying on django.db.DatabaseError
    # (the default retry_on), never on IntegrityError (never_retry).
    batch_retry_policy = FixedRetryPolicy(attempts=3, delay=0.1)
```

- `None` (the default) runs the write once.
- The attribute may be a class (instantiated per request) or an instance.
- Subclass `RetryPolicy` and override `run(func)` for custom logic (backoff,
  per-error decisions). The policy receives the zero-argument atomic-write
  callable and must return its result.
- **Inside an outer transaction** (`ATOMIC_REQUESTS = True`) each attempt is
  only a savepoint, and a lost connection or serialization failure poisons the
  outer transaction. `FixedRetryPolicy` then runs the write once and emits a
  `RuntimeWarning`.
- With `batch_use_bulk = False`, a `save()` override with side effects
  outside the database may run more than once.

## OpenAPI (drf-spectacular)

Install the extra and add the (otherwise optional) app:

```python
INSTALLED_APPS = [..., "drf_spectacular", "drf_batch"]
```

Every `BatchMixin` viewset is then documented accurately:

- the request bodies are arrays: create rows, update rows with a required pk
  and all other fields optional, and the delete pk array (described in the
  operation, since OpenAPI tooling drops DELETE bodies);
- the success body (`BatchCount` or the data, per `batch_response`) and the
  index-aligned 400/403/404/409 bodies plus 413/504 are included;
- operations not in `batch_actions` are excluded;
- cached fields are typed by their lookup field (`lookup="isin"` is a string,
  a UUID pk a `uuid`).

## How it works

```
/orders/batch/   [ rows × N ]    POST → batch_create, PATCH → batch_update, PUT → batch_replace, DELETE → batch_destroy
  1. payload must be a JSON array                         → 400
  2. len(rows) > batch_max_size                           → 413
  3. validation (BatchListSerializer, one pass after another):
     extraction   shape + key/pk coercion per row         → 400 (no collection runs)
     collection   update/destroy: targets = get_queryset().in_bulk(pks)
                  (request thread); then the replacement collector, or the
                  cached fields as one dependency graph (TaskGroup: each fetch
                  starts when its depends_on are done) on the EVENT_LOOP loop;
                  sync fetches on worker threads (request thread inside a
                  transaction); nothing to fetch → no loop
                                                          → 504 timeout, 500 error
     not found    target pk missing or out of scope       → 404
     permission   check_object_permissions(target)        → 403
     row validation  child.run_validation per row, zero I/O
                  (update: serializer.instance = target)  → 400
     conflicts    model + batch_unique_together rules, in payload and vs DB
                                                          → 409
  4. perform_batch_<op>(serializer) → serializer.save():
     retry policy → transaction.atomic() → (targets re-fetched FOR UPDATE)
     → bulk or per-row write; IntegrityError              → 409 (rolled back)
  5. 201 / 200 with get_batch_response(serializer)
```

## Settings

```python
# settings.py
DRF_BATCH = {"BATCH_MAX_SIZE": 1000}
```

`DRF_BATCH` must be a dictionary (any other type is an `ImproperlyConfigured`
at first use). Every key has a default:

| Key | Default | Meaning |
|---|---|---|
| `BATCH_MAX_SIZE` | `1000` | maximum rows per batch (413 above it) |
| `COLLECT_TIMEOUT` | `10.0` | seconds allowed for collection (504 above it) |
| `BULK_SIZE` | `1000` | rows per bulk statement |
| `EVENT_LOOP` | `"auto"` | `"auto"`: the ASGI server's loop under ASGI, else thread mode; `"thread"`: always thread mode ([loops](#which-loop-runs-collection)) |
| `THREAD_LOOP` | `None` | thread mode: dotted path to a callable returning a loop your app runs in its own thread; `None` = the built-in `drf-batch-loop` thread |

Per-view attributes (a `None` falls back to the setting):

```python
class OrderViewSet(BatchMixin, GenericViewSet):
    batch_serializer_class = OrderSerializer  # falls back to serializer_class
    batch_collector_class = None  # a BatchCollector = full-replacement hook
    batch_max_size = None  # BATCH_MAX_SIZE
    batch_use_bulk = True  # False: per-row create / update / delete()
    batch_bulk_size = None  # BULK_SIZE
    batch_actions = ("create", "update", "destroy")  # + "replace" for PUT; others → 405
    batch_conflict_status = 409
    batch_retry_policy = None  # FixedRetryPolicy(...) to retry the write
    batch_lock_targets = True  # select_for_update() targets during the write
    batch_concurrent_collection = True  # False: fetch one by one, sync inline
    batch_response = "count"  # or "data"
```

A replacement collector's `collect_timeout` overrides `COLLECT_TIMEOUT` for
that collector.

## Error contract

Row errors are a single list aligned to the request array, using `{}` for
rows without errors. A row's non-field error is reported under DRF's
`NON_FIELD_ERRORS_KEY` (`non_field_errors` by default). Field errors include
malformed keys (`Invalid value for 'ticker': expected a string, got list.`,
raised before any collection runs) and lookup misses (`Ticker 'XYZ' not
found.`: the field's `error=` template or `Object '{value}' not found.`, with
`{value}` formatted from the **coerced** key).

| Status | Code | When | Body |
|---|---|---|---|
| 400 | `invalid` | malformed rows, validation errors | index-aligned list |
| 403 | `batch_permission_denied` | a target fails the object permissions | index-aligned list |
| 404 | `batch_target_not_found` | a target pk is missing or out of scope | index-aligned list |
| 409 | `batch_conflict` | a uniqueness rule is violated | index-aligned list |
| 409 | `batch_integrity_conflict` | the database rejected the write | `{"detail": ...}` |
| 413 | `batch_size_limit_exceeded` | more than `batch_max_size` rows | `{"detail": ...}` |
| 504 | `batch_collection_timeout` | async collection timed out | `{"detail": ...}` |

The checks run in this order, and the first failing one answers: malformed
rows (400) → not found (404) → permission (403) → validation (400) →
conflicts (409). Any error writes nothing. This format is stable and
independent of DRF's own `LIST_SERIALIZER_ERRORS_AS_DICT` transition.

## Development

```bash
uv sync
nox -s all                 # lint + typecheck + tests (coverage gate: 80%)
nox -s matrix              # oldest/newest Django × DRF cells, like CI
python scripts/run_quickstart.py   # README quickstart against the built wheel
```

See [CONTRIBUTING.md](CONTRIBUTING.md); report vulnerabilities as described in
[SECURITY.md](SECURITY.md).

## Provenance

Designed from public problem statements; see [docs/PROVENANCE.md](docs/PROVENANCE.md).
BSD-3-Clause.
