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
three DRF actions:

| Method | `self.action` | Operation |
|---|---|---|
| `POST` | `batch_create` | create rows |
| `PATCH` | `batch_update` | partial update by primary key |
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
`perform_batch_update(serializer)` and `perform_batch_destroy(serializer)`,
mirroring DRF's `perform_create` & co. The default calls
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
    # Fetch order (topological, via depends_on):
    #   1. ticker/customer -> DB, auto (declaration order)
    #   2. band            -> service, consumes collected["ticker"]
    #   3. exchange_ticker -> DB again, scoped to the service's band "A"
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
  field, **all maps collected so far**, and the serializer context; return a
  `{key: value}` dict. A service fetch can therefore build its map from the
  database results (`collected["ticker"]`), and a (subclassed) database fetch
  can scope its query to the service results.
- Fetches run in topological order; a cycle (or an unknown `depends_on` name)
  is an `ImproperlyConfigured` at serializer construction.
- Read-only cached fields are excluded from `validated_data` (so bulk create
  is unaffected); a row may optionally supply their value, which is checked
  against the map like any other hit/miss (a `"band": "C"` that the service
  map does not list is a 400 row error, as is an `exchange_ticker` the scoped
  fetch did not return).


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

- **Mixed plans**: when any fetch is async, all fetches run inside one
  combined coroutine, bridged to the process-wide loop **once**, with one
  timeout. Sync fetches in a mixed plan run on worker threads.
- **Timeouts**: collection that does not finish in time is cancelled and the
  request gets a **504** (`BatchCollectionTimeout`). The limit is the
  `COLLECT_TIMEOUT` setting (default 10.0 s); a replacement collector
  overrides it with the `collect_timeout` class attribute. Any *other*
  exception collection raises propagates and becomes a **500**.
- **Sync stays free**: an all-sync plan runs inline on the request thread. No
  loop is ever started for it, and none exists at import time.
- **Loop lifecycle**: async work runs on one process-wide daemon thread
  (`drf-batch-loop`) with one event loop, created lazily on first use and
  reference-counted per request. Call `drf_batch.loop.shutdown()` to stop it
  (tests, long-running processes; it also runs at interpreter exit).
- **Database connections**: inside async fetches, run ORM calls through
  `drf_batch.loop.to_thread(...)` (or `BatchCollector.to_thread`). The call
  runs on a worker thread whose connection is **closed afterwards**, so pooled
  threads never hold connections. A synchronous ORM call made directly on the
  loop thread works but keeps that thread's connection open.
- **Transaction visibility**: worker-thread and async fetches use their own
  connections. They do **not** see uncommitted writes made earlier in the
  same request (for example under `ATOMIC_REQUESTS = True`). Update and
  delete targets are always fetched on the request thread, inside the
  request's transaction.
- **ASGI, honestly**: DRF's view dispatch is synchronous under WSGI *and*
  ASGI, so there is no running loop inside a viewset action in either case,
  and the bridge above is used in both. `drf_batch.loop.wait(coro, timeout)`
  is the await-directly path for genuinely async hosts.

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
/orders/batch/   [ rows × N ]    POST → batch_create, PATCH → batch_update, DELETE → batch_destroy
  1. payload must be a JSON array                         → 400
  2. len(rows) > batch_max_size                           → 413
  3. validation (BatchListSerializer, one pass after another):
     extraction   shape + key/pk coercion per row         → 400 (no collection runs)
     collection   update/destroy: targets = get_queryset().in_bulk(pks)
                  (request thread); then the replacement collector, or the
                  cached fields in topological order (depends_on); all sync →
                  inline; any async → one coroutine, bridged once
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
| `COLLECT_TIMEOUT` | `10.0` | seconds allowed for an async collection (504 above it) |
| `BULK_SIZE` | `1000` | rows per bulk statement |

Per-view attributes (a `None` falls back to the setting):

```python
class OrderViewSet(BatchMixin, GenericViewSet):
    batch_serializer_class = OrderSerializer  # falls back to serializer_class
    batch_collector_class = None  # a BatchCollector = full-replacement hook
    batch_max_size = None  # BATCH_MAX_SIZE
    batch_use_bulk = True  # False: per-row create / update / delete()
    batch_bulk_size = None  # BULK_SIZE
    batch_actions = ("create", "update", "destroy")  # others → 405
    batch_conflict_status = 409
    batch_retry_policy = None  # FixedRetryPolicy(...) to retry the write
    batch_lock_targets = True  # select_for_update() targets during the write
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
