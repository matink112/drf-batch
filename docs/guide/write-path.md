# Write path

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

### Full rows with `PUT`

`PUT /orders/batch/` is the same update with DRF's `PUT` semantics: every row
is validated as a full object, so a missing required field is a 400 on that
row (`{"volume": ["This field is required."]}`). As with DRF's own `PUT`, an
optional field a row leaves out keeps its current value. Scoping, object
permissions, conflicts and the all-or-none write are the same as `PATCH`.

`PUT` is **off by default**: an existing `get_permissions()` that restricts
`batch_update` would not cover a new `batch_replace` action. Enable it
explicitly:

```python
class OrderViewSet(BatchMixin, GenericViewSet):
    batch_serializer_class = OrderSerializer
    batch_actions = ("create", "update", "replace", "destroy")
```

Outside a viewset, pass `partial=False`:

```python
serializer = OrderSerializer(data=rows, many=True, operation="update", partial=False)
```

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
