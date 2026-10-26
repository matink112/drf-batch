# API reference

Everything below is importable from `drf_batch` unless noted. The
`pipeline`, `conflicts` and `writers` modules are internal and may change
without notice before 1.0.

## Viewset mixin

::: drf_batch.BatchMixin

## Serializers

::: drf_batch.BatchSerializer
    options:
      members: [collected, get_cached_fields, many_init]

::: drf_batch.BatchListSerializer
    options:
      members: [save]

## Configuration

::: drf_batch.BatchConfig

::: drf_batch.BatchOperation

## Cached fields

::: drf_batch.CachedField
    options:
      members: [prepare_key, fetch, to_representation]

::: drf_batch.CachedRelatedField
    options:
      members: [get_queryset, fetch, to_representation]

::: drf_batch.CachedServiceField

## Collector

::: drf_batch.BatchCollector

::: drf_batch.BatchContext

## Retry policies

::: drf_batch.RetryPolicy

::: drf_batch.FixedRetryPolicy

## Exceptions

::: drf_batch.BatchSizeLimitExceeded

::: drf_batch.BatchCollectionTimeout

::: drf_batch.BatchNotFound

::: drf_batch.BatchPermissionDenied

::: drf_batch.BatchConflict

## Event loop (`drf_batch.loop`)

::: drf_batch.loop
    options:
      members: [run_in_loop, wait, to_thread, session, shutdown]
      show_root_heading: false
