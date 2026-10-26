# Provenance

`drf-batch` was designed from public problem statements and public prior art,
not from any proprietary codebase.

- **Clean-room date:** 2026-10-20.
- **Primary problem statement:**
  [DRF issue #4828 — "Bulk creation validation"](https://github.com/encode/django-rest-framework/issues/4828):
  list serializers validate each object individually but not as a group,
  producing partial saves and 500s where users expect transactional behaviour.
- **Related public discussion:**
  [Django forum — nested serializer prefetching before validation](https://forum.djangoproject.com/t/django-nested-serializer-prefetching-before-validation/21774).
- **Prior art acknowledged:** GraphQL's
  [DataLoader](https://www.apollographql.com/tutorials/dataloaders-dgs/03-data-loaders-under-the-hood)
  (request-scoped batching and caching). `drf-batch` applies the
  collect-then-validate shape to a DRF write path; it does not claim to have
  invented batching.

## What this package contains

Only the generic mechanism: a viewset action, a collector hook, a
dictionary-backed relation field, index-aligned row errors, and an atomic
create. There is no domain logic, no external service integration, and no
vendor-specific SQL.

The Python expression here is original: the field abstraction, the pipeline,
the error normalization, and the transaction handling were written for this
package. Names from any private implementation were deliberately avoided.
