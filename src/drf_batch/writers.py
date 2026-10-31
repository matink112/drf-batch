"""The write-phase writers: atomic, retry-wrapped batch writes.

Each writer is the all-or-none write for one operation:

- :class:`CreateWriter` — ``bulk_create`` by default, else per-row
  ``child.create``.
- :class:`UpdateWriter` — ``bulk_update`` by default (one statement group per
  distinct changed-field set, so a row never rewrites fields it did not
  send), else per-row ``child.update`` (DRF's ``ModelSerializer.update``:
  ``save()`` overrides, signals, many-to-many).
- :class:`DestroyWriter` — queryset delete by default (cascades apply), else
  per-instance ``delete()``.

Update/destroy re-fetch their targets inside the write transaction — with
``select_for_update()`` when ``lock_targets`` is on — so every attempt works on
fresh instances and concurrent writers cannot interleave. The pks were
authorized during validation (resolved from the scoped queryset), so the
re-fetch uses the default manager.

A database ``IntegrityError`` (a constraint the conflict pass could not see,
or a concurrent insert racing it) becomes a non-index-aligned
:class:`~drf_batch.BatchConflict` instead of a 500.

Internal module; may change without notice before 1.0.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, models, transaction
from django.utils.translation import gettext_lazy

from drf_batch._utils import default_manager
from drf_batch.config import BatchConfig, BatchOperation
from drf_batch.exceptions import BatchConflict, BatchNotFound
from drf_batch.fields import _source_attribute
from drf_batch.pipeline import PipelineContext
from drf_batch.retry import RetryPolicy

if TYPE_CHECKING:
    from drf_batch.serializers import BatchSerializer


def check_bulk_fields(
    child: BatchSerializer, model: type[models.Model] | None, use_bulk: bool
) -> None:
    """Fail fast (bulk mode only) if a writable field is not on the model.

    Bulk create/update build ``Model(**row)`` / ``setattr`` directly, so a
    writable field whose name is not a concrete model field would raise
    ``TypeError`` at request time; reject it at construction instead.
    """
    if not use_bulk or model is None:
        return
    concrete = {field.name for field in model._meta.concrete_fields}
    # validated_data is keyed by each field's source, so the attribute that
    # must exist on the model is the source's first segment.
    unknown = sorted(
        name
        for name, field in child.fields.items()
        if not field.read_only and _source_attribute(field) not in concrete
    )
    if unknown:
        raise ImproperlyConfigured(
            f"BatchListSerializer: bulk mode is enabled but writable "
            f"field(s) {', '.join(unknown)} are not concrete fields on "
            f"{model.__name__}. Bulk mode supports flat model fields "
            "only; drop the field, make it read-only, or set "
            "use_bulk=False."
        )


class BatchWriter:
    """Base writer: the retry policy around a fresh transaction per attempt."""

    def __init__(
        self,
        config: BatchConfig,
        child: BatchSerializer,
        model: type[models.Model] | None,
    ) -> None:
        self._config = config
        self._child = child
        self._model = model

    def execute(
        self, ctx: PipelineContext, extra: dict[str, Any] | None = None
    ) -> list[Any]:
        if not ctx.rows:
            return []
        policy = self._config.retry_policy or RetryPolicy()
        try:
            return policy.run(lambda: self._write_atomic(ctx, extra or {}))
        except IntegrityError as exc:
            conflict = BatchConflict(
                detail={
                    "detail": str(
                        gettext_lazy(
                            "The batch violates a database constraint; "
                            "nothing was written."
                        )
                    )
                },
                code="batch_integrity_conflict",
            )
            conflict.status_code = self._config.conflict_status
            raise conflict from exc

    def _write_atomic(self, ctx: PipelineContext, extra: dict[str, Any]) -> list[Any]:
        with transaction.atomic():
            return self._write(ctx, extra)

    def _write(self, ctx: PipelineContext, extra: dict[str, Any]) -> list[Any]:
        raise NotImplementedError

    @property
    def _manager(self) -> Any:
        assert self._model is not None
        return default_manager(self._model)

    def _fresh_targets(self, ctx: PipelineContext, pks: list[Any]) -> dict[Any, Any]:
        """Re-fetch the targets inside the write transaction (locked if set)."""
        if not self._config.lock_targets:
            return ctx.targets
        fresh = cast(
            "dict[Any, Any]", self._manager.select_for_update().in_bulk(set(pks))
        )
        missing = {pk for pk in pks if pk not in fresh}
        if missing:
            # Deleted by a concurrent request after validation.
            pk_name = ctx.pk_name
            detail = [
                {pk_name: [str(gettext_lazy("Object no longer exists."))]}
                if pk in missing
                else {}
                for _index, _payload, pk in ctx.rows
            ]
            raise BatchNotFound(detail)
        return fresh


class CreateWriter(BatchWriter):
    """All-or-none batch create: ``bulk_create`` by default, else per-row."""

    def _write(self, ctx: PipelineContext, extra: dict[str, Any]) -> list[Any]:
        rows = [{**payload, **extra} for _index, payload, _pk in ctx.rows]
        if self._config.use_bulk:
            # Rows already contain FK instances (BatchRelatedField /
            # PrimaryKeyRelatedField), and every key is a model field by the
            # init check, so Model(**row) is safe.
            assert self._model is not None
            instances = [self._model(**row) for row in rows]
            self._manager.bulk_create(instances, batch_size=self._config.bulk_size)
            return instances
        return [self._child.create(row) for row in rows]


class UpdateWriter(BatchWriter):
    """All-or-none batch update: grouped ``bulk_update``, else ``child.update``."""

    def _write(self, ctx: PipelineContext, extra: dict[str, Any]) -> list[Any]:
        targets = self._fresh_targets(ctx, [pk for _i, _p, pk in ctx.rows])
        pk_name = ctx.pk_name
        results: list[Any] = []
        if not self._config.use_bulk:
            for _index, payload, pk in ctx.rows:
                row = {k: v for k, v in {**payload, **extra}.items() if k != pk_name}
                instance = targets[pk]
                results.append(self._child.update(instance, row) if row else instance)
            return results

        groups: dict[frozenset[str], list[Any]] = {}
        for _index, payload, pk in ctx.rows:
            instance = targets[pk]
            results.append(instance)
            row = {k: v for k, v in {**payload, **extra}.items() if k != pk_name}
            for field, value in row.items():
                setattr(instance, field, value)
            if row:
                groups.setdefault(frozenset(row), []).append(instance)
        for fields, instances in groups.items():
            self._manager.bulk_update(
                instances, fields=sorted(fields), batch_size=self._config.bulk_size
            )
        return results


class DestroyWriter(BatchWriter):
    """All-or-none batch destroy: queryset delete by default, else per-instance."""

    def _write(self, ctx: PipelineContext, extra: dict[str, Any]) -> list[Any]:
        pks = [pk for _index, _payload, pk in ctx.rows]
        targets = self._fresh_targets(ctx, pks)
        if self._config.use_bulk:
            self._manager.filter(pk__in=pks).delete()
        else:
            for pk in pks:
                targets[pk].delete()
        return pks


def make_writer(
    config: BatchConfig,
    child: BatchSerializer,
    model: type[models.Model] | None,
) -> BatchWriter:
    """Build the writer for ``config.operation``."""
    writer_class: type[BatchWriter] = {
        BatchOperation.CREATE: CreateWriter,
        BatchOperation.UPDATE: UpdateWriter,
        BatchOperation.DESTROY: DestroyWriter,
    }[config.operation]
    return writer_class(config, child, model)
