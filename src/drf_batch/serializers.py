"""Serializers for batch payloads.

``BatchSerializer`` is the per-row serializer; it exposes the request's
collected maps as ``self.collected`` and the declared cached fields
(:meth:`BatchSerializer.get_cached_fields`). ``BatchSerializer(many=True)``
builds a :class:`BatchListSerializer`, as DRF's ``many=True`` convention
expects.

``BatchListSerializer`` is a thin DRF facade over the formal pipeline in
:mod:`drf_batch.pipeline`: each ``to_internal_value`` call builds a
:class:`~drf_batch.pipeline.PipelineContext` and runs the operation's pass
sequence under the :class:`~drf_batch.pipeline.BatchPipeline` coordinator,
the single place that raises the index-aligned 400/404/403/409 errors.
``save`` delegates the all-or-none write to the matching writer in
:mod:`drf_batch.writers`, wrapped in the retry policy so each attempt runs
in a fresh transaction.

The ``pipeline``, ``conflicts`` and ``writers`` modules are internal and
may change without notice before 1.0.
"""

from __future__ import annotations

import dataclasses
from typing import Any, cast

from django.core.exceptions import ImproperlyConfigured
from django.db import models
from rest_framework import serializers

from drf_batch.collectors import BatchContext
from drf_batch.config import BatchConfig, BatchOperation
from drf_batch.conflicts import resolve_conflict_rules, strip_owned_unique_validators
from drf_batch.fields import CachedField
from drf_batch.pipeline import PipelineContext, build_collection_plan, build_pipeline
from drf_batch.writers import check_bulk_fields, make_writer

__all__ = ["BatchListSerializer", "BatchSerializer"]

# ListSerializer keyword arguments DRF forwards from ``many=True``.
_LIST_ONLY_KWARGS = ("allow_empty", "max_length", "min_length")


class BatchSerializer(serializers.ModelSerializer):
    """Per-row serializer for batch payloads.

    The maps produced by the request's collection (auto-collected from the
    declared cached fields, or returned by a replacement
    :class:`~drf_batch.collectors.BatchCollector`) are available as
    ``self.collected`` (a plain dict; ``{}`` when nothing was collected), so
    cached fields and user ``validate()`` methods can resolve lookups with
    zero I/O.

    Meta options:

    - ``batch_unique_together`` (default ``()``): extra cross-row/uniqueness
      rules for the 409 conflict pass — each a field name (``str``) or a
      composite (``tuple[str, ...]``) of concrete fields on ``Meta.model``.
    - ``batch_model_unique`` (default ``True``): also derive rules from the
      model's ``unique`` fields, ``unique_together`` and unconditional
      ``UniqueConstraint``s (replacing DRF's per-row unique validators).
    """

    @classmethod
    def many_init(cls, *args: Any, **kwargs: Any) -> BatchListSerializer:
        """``many=True`` -> :class:`BatchListSerializer`.

        Accepts ``batch_config=`` (a :class:`~drf_batch.BatchConfig`) and the
        config's fields as keyword shortcuts (``operation=``, ...).
        """
        config = kwargs.pop("batch_config", None)
        overrides = {
            name: kwargs.pop(name) for name in _CONFIG_FIELDS if name in kwargs
        }
        list_kwargs = {
            name: kwargs.pop(name) for name in _LIST_ONLY_KWARGS if name in kwargs
        }
        child = cls(*args, **kwargs)
        list_kwargs.update(
            {
                key: value
                for key, value in kwargs.items()
                if key in serializers.LIST_SERIALIZER_KWARGS
            }
        )
        return BatchListSerializer(
            *args, child=child, config=config, **overrides, **list_kwargs
        )

    @property
    def collected(self) -> dict[str, Any]:
        collected = self.context.get("collected")
        return collected if isinstance(collected, dict) else {}

    def get_cached_fields(self) -> list[CachedField]:
        """All declared :class:`CachedField` instances, in field order."""
        return [f for f in self.fields.values() if isinstance(f, CachedField)]


_CONFIG_FIELDS = tuple(f.name for f in dataclasses.fields(BatchConfig))


class BatchListSerializer(serializers.ListSerializer):
    """List serializer with index-aligned errors and atomic writes.

    :param config: the :class:`~drf_batch.BatchConfig` for this run
        (default: create with the defaults).

    Any :class:`~drf_batch.BatchConfig` field may also be passed as a keyword
    (``operation="update"``, ``use_bulk=False``, ...); it replaces that field
    of ``config``.

    Bulk create/update is flat-fields-only: every writable field must be a
    concrete model field (checked up front).
    """

    child: BatchSerializer | None

    def __init__(
        self, *args: Any, config: BatchConfig | None = None, **kwargs: Any
    ) -> None:
        overrides = {
            name: kwargs.pop(name) for name in _CONFIG_FIELDS if name in kwargs
        }
        config = config or BatchConfig()
        if overrides:
            config = dataclasses.replace(config, **overrides)
        self.config = config
        super().__init__(*args, **kwargs)
        # DRF types ``context`` as a read-only Mapping; the pipeline mutates
        # it (injects "collected") and hands it to fetches as a BatchContext.
        self._context: BatchContext = cast("BatchContext", self.context)
        self._model: type[models.Model] | None = getattr(
            getattr(self._child, "Meta", None), "model", None
        )
        if config.operation != BatchOperation.CREATE and self._model is None:
            raise ImproperlyConfigured(
                f"BatchListSerializer: the '{config.operation}' operation "
                "requires a Meta.model on the child serializer."
            )
        # Update is partial: a row's missing writable fields are skipped
        # (DRF reads ``self.root.partial`` for each child field), so only the
        # supplied fields change.
        if config.operation == BatchOperation.UPDATE:
            self.partial = True
        # Fail-fast configuration checks, delegated to the pipeline modules.
        check_bulk_fields(self._child, self._model, config.use_bulk)
        plan = build_collection_plan(self._child)
        rules = resolve_conflict_rules(self._child)
        strip_owned_unique_validators(self._child, rules)
        self._pipeline = build_pipeline(
            config=config,
            child=self._child,
            model=self._model,
            plan=plan,
            conflict_rules=rules,
        )
        self._writer = make_writer(config, self._child, self._model)
        # The per-call context, built in to_internal_value (fresh on every
        # call, so a re-run of is_valid starts from a clean slate).
        self._ctx: PipelineContext | None = None

    @property
    def operation(self) -> BatchOperation:
        return self.config.operation

    @property
    def _child(self) -> BatchSerializer:
        assert self.child is not None
        return self.child

    def to_internal_value(self, data: Any) -> list[Any]:
        if isinstance(data, dict) and not self.allow_empty:
            self.fail("not_a_list", input_type="dict")
        if not isinstance(data, (list, tuple)):
            self.fail("not_a_list", input_type=type(data).__name__)
        if self.allow_empty is False and not data:
            self.fail("empty")

        model = self._model
        pk_field = model._meta.pk if model is not None else None
        ctx = PipelineContext(
            data=list(data),
            operation=self.operation,
            data_length=len(data),
            model=model,
            pk_field=pk_field,
            pk_name=pk_field.name if pk_field is not None else "id",
            serializer_context=self._context,
        )
        self._pipeline.run_validation(ctx)
        self._ctx = ctx
        return [payload for _index, payload, _pk in ctx.rows]

    def save(self, **kwargs: Any) -> Any:
        """Run the all-or-none write; ``kwargs`` are merged into every row.

        For destroy ``instance`` is the list of deleted pks.
        """
        assert self._ctx is not None, "call is_valid() before save()"
        self.instance = self._writer.execute(self._ctx, extra=kwargs)
        return self.instance

    def to_representation(self, data: Any) -> list[Any]:
        if self.operation == BatchOperation.DESTROY:
            return list(data)  # the deleted pks
        return cast("list[Any]", super().to_representation(data))

    def create(self, validated_data: list[dict[str, Any]]) -> list[Any]:
        # Override compatibility; save() writes through the writer directly.
        assert self._ctx is not None, "call is_valid() before create()"
        return self._writer.execute(self._ctx)

    def update(self, instance: Any, validated_data: Any) -> list[Any]:
        # Override compatibility; save() writes through the writer directly.
        assert self._ctx is not None, "call is_valid() before update()"
        return self._writer.execute(self._ctx)
