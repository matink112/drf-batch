"""``BatchMixin``: the batch pipeline for viewsets.

Adds one route, ``/<resource>/batch/``, with three DRF actions:

- ``POST``   -> ``batch_create``  (``self.action == "batch_create"``)
- ``PATCH``  -> ``batch_update``  (by primary key, partial)
- ``DELETE`` -> ``batch_destroy`` (by primary key)

Distinct action names let ``get_permissions()``, ``get_throttles()`` and
``ScopedRateThrottle`` treat the three operations differently, as for any
other viewset action. Operations not in ``batch_actions`` (and GET/PUT)
return ``405``.

The request flow:

1. the payload must be a JSON array                          -> 400
2. ``len(rows) > batch_max_size``                            -> 413
3. validation (see :mod:`drf_batch.pipeline`): malformed rows -> 400,
   update/destroy targets outside ``get_queryset()`` -> 404, targets failing
   ``check_object_permissions`` -> 403, invalid rows -> 400, conflicts -> 409
4. ``perform_batch_<operation>(serializer)`` -> ``serializer.save()``: the
   all-or-none write, retried per ``batch_retry_policy``
5. ``201`` (create) / ``200`` with ``get_batch_response(serializer)``
"""

from __future__ import annotations

from typing import Any, ClassVar, cast

from django.core.exceptions import ImproperlyConfigured
from django.utils.translation import gettext_lazy
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import MethodNotAllowed, ValidationError
from rest_framework.request import Request
from rest_framework.response import Response

from drf_batch.collectors import BatchCollector, BatchContext
from drf_batch.config import BatchConfig, BatchOperation
from drf_batch.exceptions import BatchSizeLimitExceeded
from drf_batch.retry import RetryPolicy
from drf_batch.serializers import BatchListSerializer, BatchSerializer
from drf_batch.settings import get_setting

#: ``self.action`` value -> batch operation.
BATCH_ACTIONS: dict[str, BatchOperation] = {
    "batch_create": BatchOperation.CREATE,
    "batch_update": BatchOperation.UPDATE,
    "batch_destroy": BatchOperation.DESTROY,
}


class BatchMixin:
    """Batch create/update/destroy endpoint for a viewset.

    Attributes:
        batch_serializer_class: per-row serializer; falls back to the
            viewset's ``serializer_class``.
        batch_collector_class: optional **full-replacement** collection hook.
            ``None`` (default) auto-collects the serializer's cached fields.
        batch_max_size: per-view override of the ``BATCH_MAX_SIZE`` setting.
        batch_use_bulk: bulk writes (default) instead of per-row
            ``create`` / ``update`` / ``delete()`` (which preserve ``save()``
            overrides and model signals).
        batch_bulk_size: chunk size for the bulk writes; ``None`` (default)
            uses the ``BULK_SIZE`` setting.
        batch_actions: operations the endpoint enables (``"create"``,
            ``"update"``, ``"destroy"``); the others are a 405.
        batch_conflict_status: HTTP status for conflicts (default 409).
        batch_retry_policy: a :class:`~drf_batch.retry.RetryPolicy` instance
            or class, or ``None`` (run the write once).
        batch_lock_targets: lock update/destroy targets with
            ``select_for_update()`` during the write (default ``True``).
        batch_concurrent_collection: fetch the cached fields concurrently
            as a dependency graph (default ``True``); ``False`` fetches
            them one by one, sync fetches on the request thread.
        batch_response: ``"count"`` (default) returns ``{"count": N}``;
            ``"data"`` returns the serialized rows (create/update) or the
            deleted pks (destroy).
    """

    batch_serializer_class: type[BatchSerializer] | None = None
    batch_collector_class: type[BatchCollector] | None = None
    batch_max_size: int | None = None
    batch_use_bulk: bool = True
    batch_bulk_size: int | None = None
    batch_actions: tuple[str, ...] = ("create", "update", "destroy")
    batch_conflict_status: int = status.HTTP_409_CONFLICT
    batch_retry_policy: type[RetryPolicy] | RetryPolicy | None = None
    batch_lock_targets: bool = True
    batch_concurrent_collection: bool = True
    batch_response: str = "count"

    _SUCCESS_STATUS: ClassVar[dict[BatchOperation, int]] = {
        BatchOperation.CREATE: status.HTTP_201_CREATED,
        BatchOperation.UPDATE: status.HTTP_200_OK,
        BatchOperation.DESTROY: status.HTTP_200_OK,
    }

    # -- actions ------------------------------------------------------------

    @action(detail=False, methods=["post"], url_path="batch", url_name="batch")
    def batch_create(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """``POST /<resource>/batch/``: create rows (``201``)."""
        return self.run_batch(BatchOperation.CREATE)

    @batch_create.mapping.patch
    def batch_update(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """``PATCH /<resource>/batch/``: partial update by primary key (``200``)."""
        return self.run_batch(BatchOperation.UPDATE)

    @batch_create.mapping.delete
    def batch_destroy(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """``DELETE /<resource>/batch/``: delete by primary key (``200``)."""
        return self.run_batch(BatchOperation.DESTROY)

    def run_batch(self, operation: BatchOperation) -> Response:
        """Validate and write one batch: the body of the three actions."""
        request: Request = self.request  # type: ignore[attr-defined]
        if operation not in self.get_batch_actions():
            raise MethodNotAllowed(cast("str", request.method))
        rows = self.get_batch_rows(request)
        self.enforce_batch_size(rows)
        serializer = self.get_batch_serializer(data=rows, operation=operation)
        serializer.is_valid(raise_exception=True)
        perform = {
            BatchOperation.CREATE: self.perform_batch_create,
            BatchOperation.UPDATE: self.perform_batch_update,
            BatchOperation.DESTROY: self.perform_batch_destroy,
        }[operation]
        perform(serializer)
        return Response(
            self.get_batch_response(serializer),
            status=self._SUCCESS_STATUS[operation],
        )

    # -- hooks (mirror CreateModelMixin.perform_create & co.) ----------------

    def perform_batch_create(self, serializer: BatchListSerializer) -> None:
        """Write a validated create batch; override to pass ``save()`` kwargs."""
        serializer.save()

    def perform_batch_update(self, serializer: BatchListSerializer) -> None:
        """Write a validated update batch; override to pass ``save()`` kwargs."""
        serializer.save()

    def perform_batch_destroy(self, serializer: BatchListSerializer) -> None:
        """Delete a validated batch of targets."""
        serializer.save()

    def get_batch_response(self, serializer: BatchListSerializer) -> Any:
        """The success body. ``batch_response`` picks count or data."""
        mode = self.batch_response
        if mode == "count":
            return {"count": len(serializer.instance or [])}
        if mode == "data":
            return serializer.data
        raise ImproperlyConfigured(
            f"{type(self).__name__}.batch_response must be 'count' or 'data', "
            f"got {mode!r}."
        )

    # -- serializer resolution --------------------------------------------

    def get_batch_operation(self) -> BatchOperation | None:
        """The batch operation of the current action, or ``None``."""
        return BATCH_ACTIONS.get(getattr(self, "action", None) or "")

    def get_serializer_class(self) -> Any:
        """The batch serializer class for batch actions, else DRF's default."""
        # Batch actions use the batch serializer, so ``serializer_class`` is
        # optional and the browsable API / schema generators see the row
        # serializer.
        if self.get_batch_operation() is not None:
            return self.get_batch_serializer_class()
        return super().get_serializer_class()  # type: ignore[misc]

    def get_batch_serializer_class(self) -> type[BatchSerializer]:
        """``batch_serializer_class``, falling back to ``serializer_class``."""
        if self.batch_serializer_class is not None:
            return self.batch_serializer_class
        return cast(
            "type[BatchSerializer]",
            super().get_serializer_class(),  # type: ignore[misc]
        )

    def get_batch_serializer(
        self, *, data: Any, operation: BatchOperation
    ) -> BatchListSerializer:
        """The :class:`~drf_batch.BatchListSerializer` for one batch."""
        # ``many=True`` routes through BatchSerializer.many_init, which
        # accepts ``batch_config``.
        serializer_class: Any = self.get_batch_serializer_class()
        return cast(
            "BatchListSerializer",
            serializer_class(
                data=data,
                many=True,
                context=self.get_batch_context(),
                batch_config=self.get_batch_config(operation),
            ),
        )

    def get_batch_config(self, operation: BatchOperation) -> BatchConfig:
        """The :class:`~drf_batch.BatchConfig` for one request.

        Update/destroy get ``get_queryset()`` (scoping) and every target is
        checked with :meth:`check_batch_object_permissions`.
        """
        return BatchConfig(
            operation=operation,
            use_bulk=self.get_batch_use_bulk(),
            bulk_size=self.get_batch_bulk_size(),
            collector=self.get_batch_collector(),
            conflict_status=self.get_batch_conflict_status(),
            retry_policy=self.get_batch_retry_policy(),
            # Targets are resolved from the view's queryset (scoping), and
            # every target passes the view's object permissions.
            queryset=(
                None if operation == BatchOperation.CREATE else self.get_queryset()  # type: ignore[attr-defined]
            ),
            check_object=self.check_batch_object_permissions,
            lock_targets=self.batch_lock_targets,
            concurrent_collection=self.batch_concurrent_collection,
        )

    def check_batch_object_permissions(self, obj: Any) -> None:
        """Object-permission check for one update/destroy target."""
        view: Any = self
        view.check_object_permissions(view.request, obj)

    def get_batch_context(self) -> BatchContext:
        """The serializer context (``get_serializer_context()``)."""
        return cast("BatchContext", self.get_serializer_context())  # type: ignore[attr-defined]

    # -- options -------------------------------------------------------------

    def get_batch_rows(self, request: Request) -> list[Any]:
        """The payload rows; anything but a JSON array is a ``400``."""
        data = request.data
        if not isinstance(data, list):
            raise ValidationError(
                gettext_lazy("Batch payload must be a JSON array of row objects.")
            )
        return data

    def get_batch_max_size(self) -> int:
        """``batch_max_size``, else the ``BATCH_MAX_SIZE`` setting."""
        if self.batch_max_size is not None:
            return self.batch_max_size
        return int(get_setting("BATCH_MAX_SIZE"))

    def get_batch_use_bulk(self) -> bool:
        """Whether to use bulk writes (``batch_use_bulk``)."""
        return self.batch_use_bulk

    def get_batch_bulk_size(self) -> int | None:
        """``batch_bulk_size``, else the ``BULK_SIZE`` setting."""
        if self.batch_bulk_size is not None:
            return self.batch_bulk_size
        return int(get_setting("BULK_SIZE"))

    def get_batch_actions(self) -> tuple[str, ...]:
        """The enabled operations (``batch_actions``)."""
        return tuple(self.batch_actions)

    def get_batch_conflict_status(self) -> int:
        """The status of the conflict pass (``batch_conflict_status``)."""
        return self.batch_conflict_status

    def get_batch_retry_policy(self) -> RetryPolicy | None:
        """``batch_retry_policy``, instantiated if it is a class."""
        policy = self.batch_retry_policy
        if policy is None:
            return None
        if isinstance(policy, type):
            return policy()
        return policy

    def get_batch_collector(self) -> BatchCollector | None:
        """The full-replacement collection hook instance, or ``None``."""
        if self.batch_collector_class is None:
            return None
        return self.batch_collector_class()

    def enforce_batch_size(self, rows: list[Any]) -> None:
        """Raise :class:`~drf_batch.BatchSizeLimitExceeded` (``413``) if too large."""
        limit = self.get_batch_max_size()
        if len(rows) > limit:
            raise BatchSizeLimitExceeded(
                detail=str(
                    gettext_lazy("Batch size of {size} exceeds the maximum of {limit}.")
                ).format(size=len(rows), limit=limit)
            )
