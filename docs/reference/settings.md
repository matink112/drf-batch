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
| `COLLECT_TIMEOUT` | `10.0` | seconds allowed for collection (504 above it) |
| `BULK_SIZE` | `1000` | rows per bulk statement |
| `EVENT_LOOP` | `"auto"` | `"auto"`: the ASGI server's loop under ASGI, else thread mode; `"thread"`: always thread mode ([loops](../guide/async.md#which-loop-runs-collection)) |
| `THREAD_LOOP` | `None` | thread mode: dotted path to a callable returning a loop your app runs in its own thread; `None` = the built-in `drf-batch-loop` thread |

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
    batch_concurrent_collection = True  # False: fetch one by one, sync inline
    batch_response = "count"  # or "data"
```

A replacement collector's `collect_timeout` overrides `COLLECT_TIMEOUT` for
that collector.

```python
# settings.py: every key, with its default
DRF_BATCH = {
    "BATCH_MAX_SIZE": 1000,
    "BULK_SIZE": 1000,
    "COLLECT_TIMEOUT": 10.0,
    "EVENT_LOOP": "auto",
    "THREAD_LOOP": None,  # e.g. "myapp.runtime.get_io_loop"
}
```
