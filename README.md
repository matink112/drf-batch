# drf-batch

**The missing write-path DataLoader for Django REST Framework.**

Collect every database and external service lookup **once**, validate the entire payload in memory, and commit all-or-none in a single transaction.

`drf-batch` is to the DRF write path what `prefetch_related` is to the read path, and what DataLoader is to GraphQL.

```
POST /orders/batch/   →   201 {"count": 500}
POST /orders/batch/   →   400 [{}, {"ticker": ["Ticker 'XYZ' not found."]}]

```

## Why `drf-batch`? (100-Row Request Showcase)

When validating a 100-item payload with 2 ForeignKeys and 1 external service check per row:

* **Standard DRF (`many=True`):** $200$ SQL `SELECT` queries + $100$ network calls + $100$ sequential `INSERT` statements ($O(N)$ I/O). If row 99 fails, rows 1–98 may remain partially committed unless manually wrapped.

* **Manual `View` + `serializer.context` hack:** Breaks `RelatedField` abstractions, corrupts OpenAPI schemas, forces duplicate read/write serializers, and crashes with a `500 DataError` if a single row sends a malformed key into `.filter(id__in=...)`.

* **`drf-batch`:** **2 SQL queries + 1 batched service call (executed concurrently) + 1 `bulk_create` (**$O(1)$ **I/O).** Malformed keys are coerced and isolated as `400` row errors *before* any query hits the database.

## How It Compares

| Feature | DRF `ListSerializer` | Manual `context` Hack | `djangorestframework-bulk` | `drf-batch` | 
| ----- | ----- | ----- | ----- | ----- | 
| **Validation Lookups** | $O(N)$ per field | $O(1)$ manual | $O(N)$ per field | $O(1)$ **automatic** | 
| **Malformed Key Safety** | `400` per row | `500` DB crash | `400` per row | **`400` pre-query coercion** | 
| **External Services (Redis/HTTP/gRPC)** | $N$ sequential calls | Manual View glue | Not supported | **Native DAG (`depends_on`)** | 
| **Async Concurrency** | No | Manual boilerplate | No | **Native sync & async** | 
| **OpenAPI Schema** | Accurate | Broken relations | Outdated | **Native `drf-spectacular`** | 
| **Maintenance Status** | Active (Core) | Custom tech debt | Abandoned | **Active (Python 3.11+ / Django 5.2+)** | 

## Core Capabilities

* **Pre-Query Type Coercion:** `CachedRelatedField` validates and coerces raw input keys against the target model field *before* querying (`1101` and `"1101"` both resolve cleanly; unhashable lists or invalid types produce a clean `400` error on that specific row index instead of crashing the batch query with a `500` error).

* **Unified Dependency DAG (`depends_on`):** Combine database lookups (`CachedRelatedField`) and external Redis/HTTP/gRPC calls (`CachedServiceField`) in a single declarative graph. `drf-batch` topologically sorts dependencies and initiates dependent fetches **immediately as soon as their prerequisites resolve**.

* **One Serializer for Read & Write:** Keep standard DRF `ModelSerializer` ergonomics. `CachedRelatedField` resolves to actual model instances during validation, scopes queries via `get_queryset()`, and renders the lookup value on read paths.

* **All-or-None Atomic Writes:** Built-in `BatchMixin` adds batch `POST`, `PUT`, `PATCH`, and `DELETE` actions to any `ModelViewSet` with `bulk_create` / `bulk_update` execution and indexed DRF error responses.

## Installation

```
pip install drf-batch

```

With optional OpenAPI (`drf-spectacular`) support:

```
pip install "drf-batch[spectacular]"

```

**Requirements:** Python 3.11+, Django 5.2+, Django REST Framework 3.15+

## Quickstart

### 1. Models

```
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

No custom collector class is needed. `CachedRelatedField` inspects `Order.ticker` and `Order.customer`, deduplicates keys across the entire payload, fetches matching rows in one query per table, and hands resolved model instances to `validate()` and `create()`:

```
from drf_batch import BatchSerializer, CachedRelatedField
from rest_framework import serializers
from .models import Order


class OrderSerializer(BatchSerializer):
    ticker = CachedRelatedField(
        lookup="isin",
        map="ticker",
        error="Ticker '{value}' not found.",
    )
    customer = CachedRelatedField(
        lookup="isin",
        map="customer",
        error="Customer '{value}' not found.",
    )
    volume = serializers.IntegerField(min_value=1)

    class Meta:
        model = Order
        fields = ("id", "ticker", "customer", "volume")

```

### 3. ViewSet & Router

Add `BatchMixin` to a standard `ModelViewSet` and register it with DRF's `DefaultRouter`:

```
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

`BatchMixin` exposes `POST`, `PATCH`, `PUT`, and `DELETE` on `/orders/batch/` alongside standard single-item endpoints (`list`, `retrieve`, `create`, `update`, `destroy`).

## External Services & Dependency Graphs (`depends_on`)

Real-world validation often spans databases, caches, and internal microservices. Declare `CachedServiceField` alongside `CachedRelatedField` and wire dependencies in either direction (`DB -> Service` or `Service -> DB`).

### Execution Timeline (Exact Time-Based Resolution)

```
Time ──────────────────────────────────────────────────────────────────────────►
f1 (Redis Price Bands)     ██████
f2 (DB Customer Table)           ████████████
f3 (DB Filtered Tickers)                     ████
f4 (HTTP Risk Limits)                            ██████        ← starts when f3 ends; does not wait for f2
f5 (Internal Auth Check)   ████████
f6 (Token Validation)              ██████          ← starts when f5 ends
f7 (Metadata Lookup)       ██████████

```

### Implementation Example

```
import httpx
from django.core.cache import cache
from drf_batch import BatchSerializer, CachedRelatedField, CachedServiceField
from rest_framework import serializers
from .models import Order


class PriceBandServiceField(CachedServiceField):
    """Independent Task: Fetch active price bands from Redis."""

    def fetch(self, keys, collected, context):
        raw = cache.get_many([f"band:{isin}" for isin in keys])
        return {k.removeprefix("band:"): v for k, v in raw.items() if v is not None}


class TradableTickerField(CachedRelatedField):
    """Dependent Task (Service -> DB): Starts immediately as soon as 'band' data is resolved."""

    def fetch(self, keys, collected, context):
        bands = collected.get("band", {})
        allowed_isins = {isin for isin in keys if isin in bands}
        if not allowed_isins:
            return {}
        return super().fetch(allowed_isins, collected, context)


class CreditLimitServiceField(CachedServiceField):
    """Dependent Task (DB -> Service): Starts immediately as soon as 'customer' data is resolved."""

    def fetch(self, keys, collected, context):
        customers = collected.get("customer", {})
        active_isins = [isin for isin in keys if isin in customers]
        if not active_isins:
            return {}
        response = httpx.post(
            "http://risk.internal/api/v1/limits/batch",
            json={"customers": active_isins},
            timeout=1.5,
        )
        response.raise_for_status()
        return response.json()["limits"]


class OrderWithRiskSerializer(BatchSerializer):
    # Independent Tasks (Started immediately at T=0)
    band = PriceBandServiceField(lookup="ticker", map="band", write_only=True)
    customer = CachedRelatedField(lookup="isin", map="customer")

    # Dependent Tasks (Triggered precisely when their prerequisites resolve)
    ticker = TradableTickerField(
        lookup="isin",
        map="ticker",
        depends_on=("band",),
    )
    credit_limit = CreditLimitServiceField(
        lookup="customer",
        map="credit_limit",
        depends_on=("customer",),
        write_only=True,
    )
    volume = serializers.IntegerField(min_value=1)

    class Meta:
        model = Order
        fields = ("id", "ticker", "customer", "band", "credit_limit", "volume")

```

Unknown dependency names or circular references in `depends_on` raise a clear error at **serializer class construction time**, never mid-request.

## Real-World Recipes (`drf-batch-cookbook`)

Explore [**drf-batch-cookbook**](https://github.com/matink112/drf-batch-cookbook) for **19 runnable, production-grade scenarios across 6 domains**, complete with tests and `curl` commands:

* **Cross-Boundary Dependency Graphs:** Filter database queries using Redis price bands (`Service -> DB`), or batch-query external HTTP risk engines using database attributes resolved as soon as prerequisites are met (`DB -> Service`).

* **Cross-Row Payload Validation:** Enforce in-payload uniqueness, aggregate volume caps, and batch-wide invariants entirely in memory.

* **Request-Scoped Multi-Tenancy:** Restrict `CachedRelatedField` lookups per authenticated tenant using `queryset=` or `get_queryset()`.

* **Custom Sync & Async Collectors:** Plug in full-replacement concurrent I/O collectors for advanced multi-service pipelines.

## Scope & Design Boundaries

| In Scope | Explicitly Out of Scope | 
| ----- | ----- | 
| Batch-level lookup collection (sync & async) | Vendor-specific query optimizations | 
| In-memory row and cross-row validation | Domain-specific business rules | 
| `CachedRelatedField` with pre-query coercion | Custom storage engines | 
| `CachedServiceField` with `depends_on` DAG | Opinionated HTTP / gRPC / Redis client wrappers | 
| All-or-none atomic commit (`POST`/`PUT`/`PATCH`/`DELETE`) | Partial-commit or background queue semantics | 
| `drf-spectacular` OpenAPI schema extension | Read-path (`GET`) query optimization | 
