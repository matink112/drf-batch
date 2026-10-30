# Collection and cached fields

Collection is **serializer-driven**: a `BatchSerializer` declares which of its
fields are *cached*, and before any row is validated the list serializer

1. extracts and **coerces** each row's key for every cached field (the value
   the row sends, or a [derived or stored key](#where-a-key-comes-from)),
2. fetches **one map per field** for the whole batch (`{key: value}`),
3. validates every row against those maps, with no further I/O.

The maps are available as `self.collected` in the serializer (and as
`context["collected"]`), keyed by the field's map name.

!!! tip "Learning by example"
    The [drf-batch cookbook](https://github.com/matink112/drf-batch-cookbook) shows each option below in a runnable
    scenario, with the batches it accepts and rejects.

## `CachedRelatedField`

A relation resolved from the database. A hit returns the **model instance**,
so `create()` receives an FK instance and your `validate()` can inspect it
(`attrs["ticker"].isin`) without a query.

```python
ticker = CachedRelatedField(lookup="isin", error="Ticker '{value}' not found.")
```

| Parameter | Default | Meaning |
|---|---|---|
| `lookup` | `"pk"` | field on the related model that row values match |
| `queryset` | all rows | rows the lookup may resolve to ([scoping](#scoping-lookups)) |
| `model` | the same-named FK's model | explicit model for non-FK lookups (or `queryset.model`) |
| `map` | the field name | key in `collected` (two fields may share one map) |
| `error` | `"Object '{value}' not found."` | miss message; `{value}` is the coerced key |
| `depends_on` | `()` | cached fields whose fetches must finish first ([concurrency](#concurrency-and-depends_on)) |
| `derived_from` | `()` | field name(s) the key is built from when the row does not send it ([keys](#where-a-key-comes-from)) |
| `key` | `None` | callable building the key from the `derived_from` values |
| `read_only` | `False` | the row's value is **ignored**; excluded from `validated_data` |

The lookup model is derived from the ForeignKey (or OneToOneField) on
`Meta.model` named by the field's `source`. A non-FK lookup needs `model=` or
`queryset=`; anything else is an `ImproperlyConfigured` when the serializer is
built.

### Scoping lookups

Like DRF's `RelatedField`, restrict the rows a client may reference with
`queryset=`, or per request with `get_queryset()`:

```python
class TenantTickerField(CachedRelatedField):
    def get_queryset(self):
        return Ticker.objects.filter(tenant=self.context["request"].user.tenant)
```

A key outside the queryset is an ordinary miss (400 for that row).

### Key coercion

Row values are coerced to the lookup field's type before collection, so
`1101` and `"1101"` resolve the same `CharField` lookup. A value that cannot
be coerced is a **400 row error** raised before anything is fetched, never a
500.

| Lookup field | Accepted | Rejected (`expected a …`) |
|---|---|---|
| `CharField`, `TextField` (and subclasses) | strings, integers, integral floats | lists, dicts, booleans, … |
| integer and auto fields | integers, integral floats, numeric strings within the column's range | other strings, out-of-range numbers, booleans |
| `FloatField`, `DecimalField` | numbers, numeric strings | other strings, booleans |
| `UUIDField` | UUIDs, UUID strings | anything else |
| `BooleanField` | booleans | anything else |
| dates, times, other types | passed through unchanged | — |

### The read path

`CachedRelatedField` renders the lookup value (`"1101"`), so the same
serializer works for `list` and `retrieve`. Use `select_related` in
`get_queryset()` for those, as with any related field.

## `CachedServiceField`

A map fetched by your own callable, sync or `async def`:

```python
def fetch_bands(keys, collected, context):
    return {key: band_service.lookup(key) for key in keys}


band = CachedServiceField(fetch=fetch_bands, read_only=True)
```

- `fetch(keys, collected, context)` receives the coerced key set for this
  field, a snapshot of the maps collected so far (always including every
  [`depends_on`](#concurrency-and-depends_on) map), and the serializer
  context, and returns a `{key: value}` dict.
- Unlike a database lookup, the fetch runs even when no row supplied a key,
  because its map may be derived from the other maps. Return `{}` when nothing
  applies.
- `prepare_key` is the identity. Subclass and override `prepare_key(value)`
  to coerce service keys. It must be idempotent, map `None` to
  `drf_batch.fields.NO_KEY` ("no key"), and raise `ValidationError` for a
  malformed value.
- Subclass `CachedServiceField` (or `CachedField`) and override `fetch`
  instead of passing `fetch=`.

## Combining service and model fields

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
_LISTINGS = {"1101": "A", "1102": "B"}  # ticker ISIN -> band


def fetch_bands(keys, collected, context):
    # Service-after-DB: {isin: band}, only for the tickers the ticker query
    # found.
    tickers = collected.get("ticker", {})
    return {
        isin: _LISTINGS[isin]
        for isin in keys
        if isin in tickers and isin in _LISTINGS
    }


class ExchangeScopedTickerField(CachedRelatedField):
    # DB-after-service: only fetch tickers the service listed on exchange A.
    def fetch(self, keys, collected, context):
        bands = collected.get("band", {})
        allowed = {isin for isin, band in bands.items() if band == "A"}
        return Ticker.objects.filter(isin__in=(keys & allowed)).in_bulk("isin")


class ServiceOrderSerializer(BatchSerializer):
    # Fetch order (via depends_on):
    #   1. ticker, customer -> DB, concurrently (no dependencies)
    #   2. band             -> service, as soon as ticker is done
    #   3. exchange_ticker  -> DB again, scoped to the service's band "A"
    # band and exchange_ticker are read-only and keyed by the row's ticker
    # (derived_from): the one it sends, or the stored one on a PATCH.
    ticker = CachedRelatedField(lookup="isin", error="Ticker '{value}' not found.")
    customer = CachedRelatedField(lookup="isin", error="Customer '{value}' not found.")
    band = CachedServiceField(
        fetch=fetch_bands,
        derived_from="ticker",
        depends_on=("ticker",),
        read_only=True,
    )
    exchange_ticker = ExchangeScopedTickerField(
        model=Ticker,
        lookup="isin",
        derived_from="ticker",
        depends_on=("band",),
        read_only=True,
    )
    volume = serializers.IntegerField(min_value=1)

    class Meta:
        model = Order
        fields = ("id", "ticker", "customer", "band", "exchange_ticker", "volume")

    def validate(self, attrs):
        # Rules that consume the service results (zero I/O). self.cached()
        # works on a PATCH that omits ticker too: attrs["ticker"] would not.
        if self.cached("band") == "B":
            raise serializers.ValidationError(
                {"ticker": ["Ticker is suspended on exchange B."]}
            )
        if self.cached("exchange_ticker") is None:
            raise serializers.ValidationError(
                {"exchange_ticker": ["Ticker is not listed on exchange A."]}
            )
        return attrs
```

```bash
curl -X POST http://localhost:8000/service-orders/batch/ \
  -H 'Content-Type: application/json' \
  -d '[
    {"ticker": "1101", "customer": "112001", "volume": 3}
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
- Read-only cached fields never read the row and are excluded from
  `validated_data` (so bulk create is unaffected). Their keys come from
  `derived_from` here; a key the row did not send is never a "not found"
  error, so `validate()` checks `self.cached(...)` itself.

## Where a key comes from

Each row gets a key for every cached field, in this order:

| Field | Row sends it | Row omits it, `POST` | Row omits it, `PATCH` / `PUT` |
|---|---|---|---|
| writable | the row's value (`null`: no key) | `derived_from`, else no key | `derived_from`, else the stored value |
| `read_only` | **ignored** | `derived_from`, else no key | `derived_from`, else the stored value |

`DELETE` rows are bare pks: nothing is collected.

**The stored value** is read from the update target, from the model field the
cached field maps to (its `source`):

- a column: its value (`volume = CachedServiceField(...)` on a `PATCH`
  without `volume` fetches the stored `volume`);
- a ForeignKey / OneToOneField on a `CachedServiceField`: the FK column
  (`customer_id`);
- a `CachedRelatedField`: the stored object's lookup value. A `pk` lookup reads
  the FK column; any other (`isin`) is read across the relation, which the
  target query joins (one query, no N+1);
- not a model field: no key.

A service field therefore sees a key for **every** row on `PATCH`, with no
opt-in. A writable `CachedRelatedField` the row omits only adds its stored
key when another cached field reads it (`derived_from` or `depends_on`), so a
plain `PATCH` costs no extra query.

**`derived_from`** builds the key from other fields, for a row that does not
send the field (always, for `read_only`):

```python
class OrderSerializer(BatchSerializer):
    ticker = CachedRelatedField(lookup="isin")
    volume = serializers.IntegerField(min_value=1)
    # key: the row's ticker isin (sent, or stored on a PATCH)
    band = CachedServiceField(fetch=fetch_bands, derived_from="ticker", read_only=True)
    # key: (isin, volume), each sent or stored
    limit = CachedServiceField(
        fetch=fetch_limits, derived_from=("ticker", "volume"), read_only=True
    )
    # key built by a callable; return None for no key
    tier = CachedServiceField(
        fetch=fetch_tiers,
        derived_from="volume",
        key=lambda volume: None if volume is None else volume // 1000,
        read_only=True,
    )
```

- Each name is a serializer field or a model field:
  - a cached field gives its own key for the row (chains are fine);
  - a plain field gives the row's value converted by the field
    (`to_internal_value`), or the stored value on update;
  - a model field not on the serializer gives the stored value.
- Without `key=`, one name gives its value and several names give a tuple.
  Any `None` among them means no key.
- `derived_from` works on keys, before any fetch, so it adds no waiting. Add
  `depends_on` only if the fetch reads another field's **results**
  (`collected["ticker"]` above).

**Misses.** Only a value the row sent can be a "not found" error. A derived
or stored key that the fetch does not return is not an error: read it in
`validate()` with **`self.cached(name)`**, which gives the field's value for
the current row (`None` when there is no key or no hit), and reject the row
yourself.

Configuration errors fail when the serializer is built
(`ImproperlyConfigured`). These are an unknown name, the field itself, a plain
DRF related field (declare it as a `CachedRelatedField`), a `derived_from`
cycle, and `key=` without `derived_from`.

## Concurrency and `depends_on`

The cached fields' fetches run as **one dependency graph** inside an
`asyncio.TaskGroup`: every fetch starts as soon as **its own** `depends_on`
are done, and fetches without a dependency between them run at the same time.
`async def` fetches run on the event loop ([which one](async.md#which-loop-runs-collection));
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
stay concurrent. Details: [Database connections](async.md#database-connections).

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
  ([Async clients](async.md#async-clients)). Async fetches stay concurrent inside a
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
