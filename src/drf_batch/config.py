"""Per-request configuration of a batch run.

:class:`BatchConfig` bundles everything a :class:`~drf_batch.BatchListSerializer`
needs beyond its child serializer: the operation, the write-path options, the
collection hook, and the authorization inputs supplied by the view. It is
immutable; :class:`~drf_batch.BatchMixin` builds one per request in
``get_batch_config()``.
"""

from __future__ import annotations

import enum
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from django.core.exceptions import ImproperlyConfigured
from rest_framework import status

if TYPE_CHECKING:
    from django.db.models import QuerySet

    from drf_batch.collectors import BatchCollector
    from drf_batch.retry import RetryPolicy


class BatchOperation(enum.StrEnum):
    """The three batch operations, served on one ``/batch/`` route."""

    CREATE = "create"
    UPDATE = "update"
    DESTROY = "destroy"


@dataclass(frozen=True)
class BatchConfig:
    """Options for one batch run.

    :param operation: create / update / destroy (a :class:`BatchOperation` or
        its string value).
    :param use_bulk: ``bulk_create`` / ``bulk_update`` / queryset delete
        (default) instead of per-row ``create`` / ``update`` / ``delete()``.
    :param bulk_size: chunk size for the bulk writes; ``None`` = one statement.
    :param collector: optional full-replacement collection hook.
    :param conflict_status: HTTP status of the conflict pass (default 409).
    :param retry_policy: wraps the write; ``None`` runs it once.
    :param queryset: the queryset update/destroy targets are resolved from.
        :class:`~drf_batch.BatchMixin` passes ``view.get_queryset()``, so
        ownership/tenant scoping applies; ``None`` falls back to the model's
        default manager (standalone serializer use).
    :param check_object: called with every update/destroy target; raising
        ``PermissionDenied`` / ``NotAuthenticated`` makes that row a 403.
        The mixin passes ``view.check_object_permissions``.
    :param lock_targets: re-fetch update/destroy targets with
        ``select_for_update()`` inside the write transaction.
    :param concurrent_collection: run the batch fields' fetches as a
        dependency graph on the event loop, each one as soon as its
        ``depends_on`` are done (sync fetches on worker threads). ``False``
        runs them one by one in plan order, sync fetches on the request
        thread (inside the request's transaction).
    :param partial: update rows are partial (``PATCH``, the default): a row's
        missing fields are left unchanged. ``False`` validates every update
        row as a full object (``PUT``): required fields must be present.
        Ignored by create and destroy.
    """

    operation: BatchOperation = BatchOperation.CREATE
    use_bulk: bool = True
    bulk_size: int | None = None
    collector: BatchCollector | None = None
    conflict_status: int = status.HTTP_409_CONFLICT
    retry_policy: RetryPolicy | None = None
    queryset: QuerySet[Any] | None = None
    check_object: Callable[[Any], None] | None = None
    lock_targets: bool = True
    concurrent_collection: bool = True
    partial: bool = True

    def __post_init__(self) -> None:
        try:
            operation = BatchOperation(self.operation)
        except ValueError:
            raise ImproperlyConfigured(
                f"BatchConfig: unknown operation '{self.operation}' "
                "(expected 'create', 'update', or 'destroy')."
            ) from None
        object.__setattr__(self, "operation", operation)
