# Hooks and responses

The write runs through `perform_batch_create(serializer)`,
`perform_batch_update(serializer)` and `perform_batch_destroy(serializer)`,
mirroring DRF's `perform_create` & co. The default calls
`serializer.save()`. Keyword arguments to `save()` are merged into every row
(`serializer.save(owner=request.user)`). To run work after commit (events,
cache invalidation), use `transaction.on_commit` inside the hook.

The success body is `{"count": N}` by default (`201` for create, `200`
otherwise). Set `batch_response = "data"` to return the serialized rows
(create/update) or the deleted pks (destroy). Override
`get_batch_response(serializer)` for anything else.

`OrderSerializer(data=rows, many=True)` builds a `BatchListSerializer`, so the
pipeline also works outside a viewset:

```python
serializer = OrderSerializer(data=rows, many=True, context={"request": request})
serializer.is_valid(raise_exception=True)
serializer.save()
```

Pass `batch_config=BatchConfig(...)`, or any `BatchConfig` field as a keyword
(`operation="update"`, `use_bulk=False`, `queryset=...`), to configure it.
