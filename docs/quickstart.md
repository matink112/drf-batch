# Quickstart

```bash
pip install drf-batch
```

## 1. Models

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

## 2. Serializer — declare the cached fields, nothing else

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
order fetches (see [Collection and cached fields](guide/collection.md)).

A hit returns the **model instance**, so `create()` receives an FK instance
and your own `validate()` can inspect it (`data["ticker"].isin`) without
touching the database.


Like DRF's `RelatedField`, a `CachedRelatedField` can be scoped so a client
cannot reference rows it may not see: pass `queryset=` or override
`get_queryset()` (`self.context["request"]` is available). The same field
renders the lookup value (`"1101"`) on the read path, so one serializer can
serve `list`/`retrieve` and the batch route.

## 3. View and URLs

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
| `PUT` | `batch_replace` | full update by primary key (opt-in, see [Batch update](guide/write-path.md#batch-update)) |
| `DELETE` | `batch_destroy` | delete by primary key |

The distinct action names mean `get_permissions()`, `get_throttles()` and
`ScopedRateThrottle` can treat them differently, as with any other action.
Other methods on the route return `405`. The mixin also works next to DRF's
own mixins (`ModelViewSet`, `ReadOnlyModelViewSet`, …). Setting
`serializer_class = OrderSerializer` instead of `batch_serializer_class`
serves both the read path and the batch route.

## 4. Call it

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

## Next steps

- [Collection and cached fields](guide/collection.md): lookups, service
  fields, `depends_on`, replacement collectors.
- [Permissions and scoping](guide/permissions.md): how update and delete
  respect `get_queryset()` and object permissions.
- [Write path](guide/write-path.md): batch update and delete, bulk versus
  per-row writes, locking.
- [Error contract](reference/errors.md): every status code and body.
