# How it works

```
/orders/batch/   [ rows × N ]    POST → batch_create, PATCH → batch_update, PUT → batch_replace, DELETE → batch_destroy
  1. payload must be a JSON array                         → 400
  2. len(rows) > batch_max_size                           → 413
  3. validation (BatchListSerializer, one pass after another):
     extraction   shape + key/pk coercion per row         → 400 (no collection runs)
     targets      update/destroy: get_queryset().in_bulk(pks) (request thread)
     not found    target pk missing or out of scope       → 404
     permission   check_object_permissions(target)        → 403
     keys         keys of the cached fields a row did not send (read_only
                  fields never read it): derived_from, else the stored value
     collection   the replacement collector, or the cached fields as one
                  dependency graph (TaskGroup: each fetch starts when its
                  depends_on are done) on the EVENT_LOOP loop; sync fetches on
                  worker threads (request thread inside a transaction);
                  nothing to fetch → no loop; destroy: nothing
                                                          → 504 timeout, 500 error
     row validation  child.run_validation per row, zero I/O
                  (update: serializer.instance = target)  → 400
     conflicts    model + batch_unique_together rules, in payload and vs DB
                                                          → 409
  4. perform_batch_<op>(serializer) → serializer.save():
     retry policy → transaction.atomic() → (targets re-fetched FOR UPDATE)
     → bulk or per-row write; IntegrityError              → 409 (rolled back)
  5. 201 / 200 with get_batch_response(serializer)
```
