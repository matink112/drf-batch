# Provenance

`drf-batch` was extracted from production. Its author designed and built the
mechanism inside the trading backend of a large fintech company, where it
validates and commits high-volume batches of orders against customers,
instruments and live risk services. In that system, a partial write or a
`500` in the middle of a batch is not an option.

This package is the generalized, open-source release of that mechanism.

## What was kept

Only the generic mechanism: the batch viewset actions, the collection step
(cached fields, the `depends_on` graph and the collector hook),
dictionary-backed relation fields, index-aligned row errors, whole-batch
conflict detection, and the all-or-none write.

## What was left out

Everything specific to the original system: domain models and business rules,
integrations with internal services, vendor-specific SQL, and private names.
Field, class and setting names were chosen for a general DRF audience.

## The public problem it answers

- [DRF issue #4828, "Bulk creation validation"](https://github.com/encode/django-rest-framework/issues/4828):
  list serializers validate each object on its own rather than as a group,
  which leads to partial saves and 500s where users expect transactional
  behaviour.
- [Django forum: nested serializer prefetching before validation](https://forum.djangoproject.com/t/django-nested-serializer-prefetching-before-validation/21774).

## Prior art

GraphQL's
[DataLoader](https://github.com/graphql/dataloader) established
request-scoped batching and caching. `drf-batch` applies the same
collect-then-validate approach to the DRF write path. It does not claim to
have invented batching.
