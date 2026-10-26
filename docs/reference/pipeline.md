# How it works

```
/orders/batch/   [ rows × N ]    POST → batch_create, PATCH → batch_update, DELETE → batch_destroy
  1. payload must be a JSON array                         → 400
  2. len(rows) > batch_max_size                           → 413
  3. validation (BatchListSerializer, one pass after another):
     extraction   shape + key/pk coercion per row         → 400 (no collection runs)
     collection   update/destroy: targets = get_queryset().in_bulk(pks)
                  (request thread); then the replacement collector, or the
                  cached fields in topological order (depends_on); all sync →
                  inline; any async → one coroutine, bridged once
                                                          → 504 timeout, 500 error
     not found    target pk missing or out of scope       → 404
     permission   check_object_permissions(target)        → 403
     row validation  child.run_validation per row, zero I/O
                  (update: serializer.instance = target)  → 400
     conflicts    model + batch_unique_together rules, in payload and vs DB
                                                          → 409
  4. perform_batch_<op>(serializer) → serializer.save():
     retry policy → transaction.atomic() → (targets re-fetched FOR UPDATE)
     → bulk or per-row write; IntegrityError              → 409 (rolled back)
  5. 201 / 200 with get_batch_response(serializer)
```
