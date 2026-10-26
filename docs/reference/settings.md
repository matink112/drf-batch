# Settings and view options

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
