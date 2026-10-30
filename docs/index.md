# drf-batch

**Batch-aware validation for Django REST Framework.** Collect every lookup a
request needs **once**, validate the whole payload against memory, and commit
all-or-none.

`drf-batch` is to the write path what `drf-serializer-prefetch` is to the read
path, and what DataLoader is to GraphQL.

```text
POST /orders/batch/   →   201 {"count": 2}
POST /orders/batch/   →   400 [{}, {"ticker": ["Ticker 'XYZ' not found."]}]
```

!!! success "Extracted from production"
    `drf-batch` comes from the trading backend of a large fintech company,
    where its author designed and built it to validate and commit
    high-volume batches of orders against customers, instruments and live
    risk services. This package is the generalized, open-source release of
    that mechanism, with the domain logic removed
    ([provenance](PROVENANCE.md)).

## Highlights

```text
request latency ≈ longest fetch chain  +  in-memory validation  +  bulk write
```

- **Every lookup is fetched once, concurrently.** Each cached field is one
  query or one service call for the whole batch, no matter how many rows.
  All fetches run as one `asyncio.TaskGroup`: `async def` fetches on the
  event loop, sync ORM fetches on worker threads
  ([caveats](guide/collection.md#transactions)). Ten FK fields plus three
  slow services cost about as much as the slowest of them, not the sum.
- **`depends_on`: fetches as a dependency graph**
  ([details](guide/collection.md#concurrency-and-depends_on)). Declare which maps a fetch
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

## The gap it fills

DRF's list serializers validate each row individually, then save one by one.
An invalid row late in the payload still leaves earlier rows committed, and
every row re-runs the same lookups. This is documented in
[DRF #4828](https://github.com/encode/django-rest-framework/issues/4828) and in
the unresolved forum thread on
[prefetching before validation](https://forum.djangoproject.com/t/django-nested-serializer-prefetching-before-validation/21774).

Per-request batched loading is **not new**: GraphQL's
[DataLoader](https://www.apollographql.com/tutorials/dataloaders-dgs/03-data-loaders-under-the-hood)
established batching plus request-scoped caching in 2015. What DRF lacks is a
write-path hook: a place to collect lookups *before* validation, plus
cross-row semantics and a single transaction. That is all this package adds.

## What it is not

| In scope | Explicitly out of scope |
|---|---|
| Batch create/update/delete viewset actions | Read-path query optimisation (`drf-serializer-prefetch`) |
| Serializer-driven collection + a full-replacement collector hook | Any specific data source (market data, gRPC, …) |
| Memory-backed relation fields during validation | Event emission / Kafka integrations |
| Sync **and** async collection, all-or-none writes | Bulk insert with vendor-specific SQL |
| Bulk writes by default, no vendor SQL | Auto-generated bulk CRUD routes |

## Requirements

- Python 3.11+
- Django 5.2+
- Django REST Framework 3.15+
- Optional: drf-spectacular 0.27+ for OpenAPI schemas

```bash
pip install drf-batch
pip install "drf-batch[spectacular]"   # with OpenAPI support
```

Next: the [Quickstart](quickstart.md), then the [cookbook](https://github.com/matink112/drf-batch-cookbook) for a
runnable scenario per option.

## Contributing

Contributions are welcome: cookbook recipes from your own domain, bug reports
with a failing test, docs, benchmarks and integrations. Start with the
[contributing guide](contributing.md), and find the source and issue tracker
on [GitHub](https://github.com/matink112/drf-batch). If you run `drf-batch` in
production, please
[open an issue](https://github.com/matink112/drf-batch/issues) and tell us
about it.
