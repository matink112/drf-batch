# Permissions and scoping

The batch route follows the same authorization rules as DRF's detail routes:

- **Update and delete targets come from `get_queryset()`.** A pk outside the
  view's queryset is a `404`, exactly like a pk that does not exist, so
  ownership and tenant filters in `get_queryset()` apply to batches too.
- **Every target passes `check_object_permissions()`.** A target that fails a
  permission's `has_object_permission()` makes the batch a `403`, with the
  permission's message on that row:

  ```json
  [{}, {"non_field_errors": ["Orders with volume 10 are frozen."]}]
  ```

- Rows are validated only after both checks pass, so a caller never learns
  validation details about rows it cannot modify.
- Cached lookups are scoped with `CachedRelatedField(queryset=...)` (see [Scoping lookups](collection.md#scoping-lookups)).

```python
class OwnedOrderViewSet(BatchMixin, GenericViewSet):
    batch_serializer_class = OrderSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Order.objects.filter(owner=self.request.user)

    def perform_batch_create(self, serializer):
        serializer.save(owner=self.request.user)
```
