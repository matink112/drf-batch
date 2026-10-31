# API reference

Everything below is importable from `drf_batch` unless noted. The
`pipeline`, `conflicts` and `writers` modules are internal and may change
without notice before 1.0.

## Viewset mixin

::: drf_batch.BatchMixin

## Serializers

::: drf_batch.BatchSerializer
    options:
      members: [collected, fetched, get_batch_fields, many_init]

::: drf_batch.BatchListSerializer
    options:
      members: [save]

## Configuration

::: drf_batch.BatchConfig

::: drf_batch.BatchOperation

## Batch fields

::: drf_batch.BatchField
    options:
      members: [prepare_key, fetch, derived_key, stored_key_path, to_representation]

::: drf_batch.BatchRelatedField
    options:
      members: [get_queryset, fetch, to_representation]

::: drf_batch.BatchServiceField

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
      members: [use_loop, current_loop, run_sync, to_thread, run_in_loop, wait, session, shutdown]
      show_root_heading: false
