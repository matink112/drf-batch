# Collection and cached fields

Collection is **serializer-driven**: a `BatchSerializer` declares which of its
fields are *cached*, and before any row is validated the list serializer

1. extracts and **coerces** each row's key for every cached field,
2. fetches **one map per field** for the whole batch (`{key: value}`),
3. validates every row against those maps, with no further I/O.

The maps are available as `self.collected` in the serializer (and as
`context["collected"]`), keyed by the field's map name.

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
| `depends_on` | `()` | cached fields whose maps must be collected first |
| `read_only` | `False` | excluded from `validated_data`; a supplied value is still checked |

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
  field, **all maps collected so far**, and the serializer context, and
  returns a `{key: value}` dict.
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
