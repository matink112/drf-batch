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

## What you get

- **One route, three operations.** `BatchMixin` adds `/<resource>/batch/`
  with `POST` (create), `PATCH` (partial update by pk) and `DELETE` (delete by
  pk), as three ordinary DRF actions.
- **Collect once, validate in memory.** Cached fields declare their lookups;
  each lookup is one query (or one service call) for the whole batch, sync or
  `async`.
- **DRF's authorization rules.** Update and delete targets come from
  `get_queryset()` and pass `check_object_permissions()`.
- **Whole-batch uniqueness.** Duplicates inside the payload and clashes with
  existing rows are a 409 before anything is written.
- **All-or-none writes.** Bulk by default, per-row when you need signals and
  `save()` overrides, optionally retried.
- **Index-aligned errors** that existing `many=True` clients already
  understand.
- **OpenAPI** through drf-spectacular.

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

Next: the [Quickstart](quickstart.md).
