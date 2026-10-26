# Retry policy

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
