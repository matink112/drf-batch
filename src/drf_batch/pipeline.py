"""The batch validation pipeline: formal passes over a shared context.

- :class:`PipelineContext` — one mutable state container, constructed per
  ``to_internal_value`` call (never reused across calls, so a re-run of
  ``is_valid`` starts from a clean slate). The inputs are fixed at
  construction; the state fields are populated by the passes in pipeline
  order.
- :class:`PipelinePass` — one step of the pipeline. Passes only *record*
  state (survivors, targets, error buckets) on the context; they do not
  raise for row-level failures.
- :class:`BatchPipeline` — the coordinator and the **single place that
  raises**: after every pass it inspects the context's error buckets and
  raises the index-aligned exception of the first non-empty one.
- :func:`build_collection_plan` / :func:`build_pipeline` — the fail-fast
  configuration resolution and the per-operation pass assembly.

Pass order (and therefore error precedence):

- create:  extraction (400) -> collection -> row validation (400) -> conflict (409)
- update:  extraction (400) -> collection -> not found (404) -> permission (403)
  -> row validation (400) -> conflict (409)
- destroy: extraction (400) -> collection -> not found (404) -> permission (403)
  -> duplicate pk (409)

Update/destroy targets are resolved from the configured queryset (the view's
``get_queryset()``), so a pk outside the caller's scope is a 404 exactly like
a pk that does not exist. A row is never validated against a target the
caller may not modify.

The write phase lives in :mod:`drf_batch.writers`; the 409 conflict pass in
:mod:`drf_batch.conflicts`. This module is internal and may change without
notice before 1.0.
"""

from __future__ import annotations

import dataclasses
import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, cast

from django.core.exceptions import ImproperlyConfigured
from django.core.exceptions import (
    ValidationError as DjangoValidationError,
)
from django.db import models
from django.utils.translation import gettext_lazy
from rest_framework.exceptions import (
    APIException,
    NotAuthenticated,
    PermissionDenied,
    ValidationError,
)

try:  # DRF >= 3.18
    from rest_framework.fields import get_error_detail
except ImportError:  # DRF < 3.18 kept it in rest_framework.exceptions
    from rest_framework.exceptions import get_error_detail  # type: ignore[attr-defined,no-redef]

from drf_batch import loop
from drf_batch._utils import default_manager, non_field_key
from drf_batch.collectors import BatchCollector, BatchContext
from drf_batch.config import BatchConfig, BatchOperation
from drf_batch.exceptions import BatchConflict, BatchNotFound, BatchPermissionDenied
from drf_batch.fields import (
    NO_KEY,
    CachedField,
    CachedRelatedField,
    _coerce_lookup_key,
)
from drf_batch.settings import get_setting

if TYPE_CHECKING:
    from drf_batch.serializers import BatchSerializer


@dataclass
class PipelineContext:
    """Mutable per-request state shared by the pipeline passes."""

    # -- inputs (immutable after construction) --------------------------
    # The raw payload rows (a copy: ``data`` may be a list or a tuple).
    data: list[Any]
    operation: BatchOperation
    data_length: int
    model: type[models.Model] | None
    pk_field: models.Field | None
    pk_name: str
    # The mutable DRF context; the collection pass injects "collected"
    # into this same dict and hands it to fetches as a BatchContext.
    serializer_context: BatchContext

    # -- state (populated by the passes, in order) ----------------------
    # Extraction output: {map: coerced keys} for the collection pass.
    key_sets: dict[str, set[Any]] = dataclasses.field(default_factory=dict)
    # Extraction output: per-survivor {map: key} (the read-only miss check
    # consumes it in row validation).
    row_keys: dict[int, dict[str, Any]] = dataclasses.field(default_factory=dict)
    # Pass-1 survivors: (index, raw_row). For destroy the raw row is the
    # coerced pk scalar.
    survivors: list[tuple[int, Any]] = dataclasses.field(default_factory=list)
    # update/destroy: per-survivor coerced target pk, in index order.
    row_pks: dict[int, Any] = dataclasses.field(default_factory=dict)
    # Collection output: the request's collected maps.
    collected: dict[str, Any] | None = None
    # update/destroy: pk -> target instance, from the scoped queryset.
    targets: dict[Any, Any] = dataclasses.field(default_factory=dict)
    # Final survivors: (index, payload, pk_or_None). create:
    # (i, validated, None); update: (i, validated, pk); destroy:
    # (i, pk, pk) — the scalar pk is both payload and key.
    rows: list[tuple[int, Any, Any | None]] = dataclasses.field(default_factory=list)
    # Error buckets, keyed by row index: 400 / 404 / 403 / 409.
    row_errors: dict[int, dict[str, Any]] = dataclasses.field(default_factory=dict)
    not_found: dict[int, dict[str, Any]] = dataclasses.field(default_factory=dict)
    forbidden: dict[int, dict[str, Any]] = dataclasses.field(default_factory=dict)
    conflict_errors: dict[int, dict[str, Any]] = dataclasses.field(default_factory=dict)


class PipelinePass:
    """One step of the validation pipeline.

    Passes only *record* state on the :class:`PipelineContext`; they must
    not raise for row-level failures — the :class:`BatchPipeline`
    coordinator inspects the context after each pass and raises.
    """

    name: ClassVar[str] = ""

    def run(self, ctx: PipelineContext) -> None:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------


def _normalize_row_error(detail: Any) -> dict[str, Any]:
    """Normalize one row's ``ValidationError.detail`` to a row error dict.

    dict -> as-is; list -> ``{NON_FIELD_ERRORS_KEY: detail}``; other ->
    ``{NON_FIELD_ERRORS_KEY: [detail]}``.
    """
    if isinstance(detail, dict):
        return cast("dict[str, Any]", detail)
    if isinstance(detail, list):
        return {non_field_key(): detail}
    return {non_field_key(): [detail]}


def _detail_list(detail: Any) -> list[str]:
    if isinstance(detail, (list, tuple)):
        return [str(item) for item in detail]
    return [str(detail)]


def _non_field_error(message: str) -> dict[str, Any]:
    return {non_field_key(): [message]}


def _invalid_key_message(field: str, expected: str, value: Any) -> str:
    return str(
        gettext_lazy(
            "Invalid value for '{field}': expected a {expected}, got {actual}."
        )
    ).format(field=field, expected=expected, actual=type(value).__name__)


def _merge_key_error(
    row_error: dict[str, Any], source: str, exc: ValidationError
) -> None:
    detail = exc.detail
    if isinstance(detail, dict):
        for key, value in detail.items():
            row_error.setdefault(key, []).extend(_detail_list(value))
    else:
        row_error.setdefault(source, []).extend(_detail_list(detail))


def _required_message() -> str:
    return str(gettext_lazy("This field is required."))


# ---------------------------------------------------------------------------
# pass 1: extraction
# ---------------------------------------------------------------------------


class CreateExtractionPass(PipelinePass):
    """Pass 1 for create: shape + cached key extraction.

    Malformed keys are row errors and the row is excluded from later passes
    (same precedent as non-dict rows).
    """

    name: ClassVar[str] = "create_extraction"

    def __init__(self, cached_fields: list[CachedField]) -> None:
        self._cached_fields = cached_fields

    def run(self, ctx: PipelineContext) -> None:
        for index, item in enumerate(ctx.data):
            if not isinstance(item, dict):
                message = str(
                    gettext_lazy("Invalid row: expected a dictionary, got {row_type}.")
                ).format(row_type=type(item).__name__)
                ctx.row_errors[index] = _non_field_error(message)
                continue
            row_error: dict[str, Any] = {}
            pk = self._extract_pk(item, row_error)
            row_key_map = self._extract_cached_keys(item, row_error, ctx.key_sets)
            if row_error:
                # Keys this row already added to key_sets are harmless: any
                # row error raises before collection runs.
                ctx.row_errors[index] = row_error
                continue
            ctx.survivors.append((index, item))
            ctx.row_keys[index] = row_key_map
            if pk is not NO_KEY:
                ctx.row_pks[index] = pk

    def _extract_pk(self, item: dict[str, Any], row_error: dict[str, Any]) -> Any:
        """Create rows carry no target pk."""
        return NO_KEY

    def _extract_cached_keys(
        self,
        item: dict[str, Any],
        row_error: dict[str, Any],
        key_sets: dict[str, set[Any]],
    ) -> dict[str, Any]:
        """Coerce every cached field's key; add each to its map's key set.

        Several fields may share one map, so keys go into ``key_sets`` per
        field (the returned per-row map keeps the last key per map).
        """
        row_key_map: dict[str, Any] = {}
        for field in self._cached_fields:
            # DRF reads the row's input key from the field name (the source
            # names the validated_data/output attribute).
            name = field.field_name
            assert name is not None  # set in bind()
            try:
                key = field.prepare_key(item.get(name))
            except ValidationError as exc:
                _merge_key_error(row_error, name, exc)
                continue
            row_key_map[field.map] = key
            if key is not NO_KEY:
                key_sets.setdefault(field.map, set()).add(key)
        return row_key_map


class UpdateExtractionPass(CreateExtractionPass):
    """Pass 1 for update: shape + target pk + cached key extraction."""

    name: ClassVar[str] = "update_extraction"

    def __init__(
        self,
        cached_fields: list[CachedField],
        pk_field: models.Field,
        pk_name: str,
    ) -> None:
        super().__init__(cached_fields)
        self._pk_field = pk_field
        self._pk_name = pk_name

    def _extract_pk(self, item: dict[str, Any], row_error: dict[str, Any]) -> Any:
        pk_raw = item.get(self._pk_name)
        if pk_raw is None:
            row_error[self._pk_name] = [_required_message()]
            return NO_KEY
        pk, expected = _coerce_lookup_key(pk_raw, self._pk_field)
        if expected is not None:
            row_error[self._pk_name] = [
                _invalid_key_message(self._pk_name, expected, pk_raw)
            ]
            return NO_KEY
        return pk


class DestroyExtractionPass(PipelinePass):
    """Pass 1 for destroy: each row is a scalar pk."""

    name: ClassVar[str] = "destroy_extraction"

    def __init__(self, pk_field: models.Field, pk_name: str) -> None:
        self._pk_field = pk_field
        self._pk_name = pk_name

    def run(self, ctx: PipelineContext) -> None:
        for index, item in enumerate(ctx.data):
            if item is None:
                ctx.row_errors[index] = {self._pk_name: [_required_message()]}
                continue
            if isinstance(item, (dict, list, tuple)):
                message = str(
                    gettext_lazy("Invalid row: expected a pk value, got {row_type}.")
                ).format(row_type=type(item).__name__)
                ctx.row_errors[index] = _non_field_error(message)
                continue
            key, expected = _coerce_lookup_key(item, self._pk_field)
            if expected is not None:
                ctx.row_errors[index] = {
                    self._pk_name: [_invalid_key_message(self._pk_name, expected, item)]
                }
                continue
            ctx.survivors.append((index, key))
            ctx.row_pks[index] = key
        # Destroy has no row-validation pass: the final rows are the pk
        # pairs, where the scalar pk is both payload and key.
        ctx.rows = [(index, key, key) for index, key in ctx.survivors]


# ---------------------------------------------------------------------------
# pass 2: collection
# ---------------------------------------------------------------------------


class CollectionPass(PipelinePass):
    """Pass 2: resolve targets, then run the collector or the auto plan.

    update/destroy targets are fetched **first, on the request thread**,
    from the scoped target queryset (one ``in_bulk``) — never by a
    collector — so scoping cannot be bypassed and the lookup runs in the
    request's transaction. The target map is also exposed as
    ``collected[pk_name]``. Cached fields are then collected (not for
    destroy, which has no row payload).
    """

    name: ClassVar[str] = "collection"

    def __init__(
        self,
        plan: list[CachedField],
        operation: BatchOperation,
        pk_name: str,
        collector: BatchCollector | None,
        target_queryset: Callable[[], Any] | None,
    ) -> None:
        self._plan = plan
        self._operation = operation
        self._pk_name = pk_name
        self._collector = collector
        self._target_queryset = target_queryset

    def run(self, ctx: PipelineContext) -> None:
        seed: dict[str, Any] = {}
        if self._target_queryset is not None:
            keys = set(ctx.row_pks.values())
            ctx.targets = (
                cast("dict[Any, Any]", self._target_queryset().in_bulk(keys))
                if keys
                else {}
            )
            seed[self._pk_name] = ctx.targets
        if self._operation == BatchOperation.DESTROY:
            collected = seed
        elif self._collector is not None:
            collected = {**self._run_collector(ctx), **seed}
        else:
            collected = self._run_plan(ctx, seed)
        ctx.collected = collected
        ctx.serializer_context["collected"] = collected

    def _run_collector(self, ctx: PipelineContext) -> dict[str, Any]:
        assert self._collector is not None
        result = self._collector.collect(ctx.key_sets, ctx.serializer_context)
        if inspect.isawaitable(result):
            timeout = self._collector.collect_timeout
            if timeout is None:
                timeout = get_setting("COLLECT_TIMEOUT")
            result = loop.run_in_loop(result, timeout=timeout)
        return dict(cast("dict[str, Any]", result))

    def _run_plan(self, ctx: PipelineContext, seed: dict[str, Any]) -> dict[str, Any]:
        plan = self._plan
        if not any(field.is_async for field in plan):
            # All fetches sync: run inline on the request thread — no loop
            # thread is ever started (sync-only deployments stay loop-free).
            collected = dict(seed)
            for field in plan:
                if field.map in collected:
                    continue
                keys = ctx.key_sets.get(field.map, set())
                if not keys and field.skip_fetch_on_empty_keys:
                    collected[field.map] = {}
                else:
                    collected[field.map] = field.fetch(
                        keys, collected, ctx.serializer_context
                    )
            return collected

        async def _run_all() -> dict[str, Any]:
            collected = dict(seed)
            for field in plan:
                if field.map in collected:
                    continue
                keys = ctx.key_sets.get(field.map, set())
                if not keys and field.skip_fetch_on_empty_keys:
                    result: Any = {}
                elif field.is_async:
                    result = await field.fetch(keys, collected, ctx.serializer_context)
                else:
                    # The loop thread has a running event loop, so a sync
                    # fetch (notably ORM) goes to a worker thread whose
                    # connection is closed afterwards.
                    result = await loop.to_thread(
                        field.fetch, keys, collected, ctx.serializer_context
                    )
                    if inspect.isawaitable(result):
                        result = await result
                collected[field.map] = result
            return collected

        timeout = get_setting("COLLECT_TIMEOUT")
        return cast("dict[str, Any]", loop.run_in_loop(_run_all(), timeout=timeout))


# ---------------------------------------------------------------------------
# 404 / 403 passes (update/destroy)
# ---------------------------------------------------------------------------


class NotFoundPass(PipelinePass):
    """404: a target pk missing from the (scoped) target map."""

    name: ClassVar[str] = "not_found"

    def __init__(self, pk_name: str) -> None:
        self._pk_name = pk_name

    def run(self, ctx: PipelineContext) -> None:
        for index, pk in ctx.row_pks.items():
            if pk not in ctx.targets:
                message = str(
                    gettext_lazy("Object with {name} '{value}' not found.")
                ).format(name=self._pk_name, value=pk)
                ctx.not_found[index] = {self._pk_name: [message]}


class PermissionPass(PipelinePass):
    """403: the configured object-permission check rejects a target."""

    name: ClassVar[str] = "permission"

    def __init__(self, check_object: Callable[[Any], None]) -> None:
        self._check_object = check_object

    def run(self, ctx: PipelineContext) -> None:
        for index, pk in ctx.row_pks.items():
            try:
                self._check_object(ctx.targets[pk])
            except (PermissionDenied, NotAuthenticated) as exc:
                ctx.forbidden[index] = {non_field_key(): _detail_list(exc.detail)}


# ---------------------------------------------------------------------------
# row validation (create/update)
# ---------------------------------------------------------------------------


class RowValidationPass(PipelinePass):
    """Per-row ``child.run_validation`` against the collected maps.

    Update rows are validated with their target instance set (DRF's update
    pattern). Plus the read-only cached fields' miss check (they do not run
    through ``run_validation``).
    """

    name: ClassVar[str] = "row_validation"

    def __init__(
        self,
        child: BatchSerializer,
        operation: BatchOperation,
        read_only_fields: list[CachedField],
    ) -> None:
        self._child = child
        self._operation = operation
        self._read_only_fields = read_only_fields

    def run(self, ctx: PipelineContext) -> None:
        is_update = self._operation == BatchOperation.UPDATE
        try:
            for index, item in ctx.survivors:
                pk = ctx.row_pks.get(index) if is_update else None
                if is_update:
                    self._child.instance = ctx.targets[pk]
                try:
                    validated = self._child.run_validation(item)
                except ValidationError as exc:
                    ctx.row_errors[index] = _normalize_row_error(exc.detail)
                    continue
                except DjangoValidationError as exc:
                    ctx.row_errors[index] = _normalize_row_error(get_error_detail(exc))
                    continue
                row_error = self._check_read_only(ctx, ctx.row_keys[index])
                if row_error is None:
                    ctx.rows.append((index, validated, pk))
                else:
                    ctx.row_errors[index] = row_error
        finally:
            if is_update:
                self._child.instance = None

    def _check_read_only(
        self, ctx: PipelineContext, row_key_map: dict[str, Any]
    ) -> dict[str, Any] | None:
        """A read-only cached field: extracted key absent from the map -> row error."""
        collected = ctx.collected or {}
        for field in self._read_only_fields:
            key = row_key_map.get(field.map, NO_KEY)
            if key is NO_KEY:
                continue
            mapping = collected.get(field.map)
            if not isinstance(mapping, dict) or key not in mapping:
                template = (
                    field.error
                    if field.error is not None
                    else field.default_error_messages["not_found"]
                )
                name = field.field_name
                assert name is not None  # set in bind()
                return {name: [str(template).format(value=key)]}
        return None


# ---------------------------------------------------------------------------
# coordinator
# ---------------------------------------------------------------------------


# Error buckets in raise order; only one can be non-empty at a time, since
# the coordinator raises as soon as a pass fills one.
_BUCKETS: tuple[tuple[str, type[APIException]], ...] = (
    ("row_errors", ValidationError),
    ("not_found", BatchNotFound),
    ("forbidden", BatchPermissionDenied),
    ("conflict_errors", BatchConflict),
)


class BatchPipeline:
    """The coordinator: run the passes, raising index-aligned errors."""

    def __init__(self, passes: list[PipelinePass], conflict_status: int) -> None:
        self._passes = passes
        self._conflict_status = conflict_status

    def run_validation(self, ctx: PipelineContext) -> None:
        for pipeline_pass in self._passes:
            pipeline_pass.run(ctx)
            for attr, exc_class in _BUCKETS:
                bucket: dict[int, dict[str, Any]] = getattr(ctx, attr)
                if bucket:
                    detail = [bucket.get(i, {}) for i in range(ctx.data_length)]
                    exc = exc_class(detail)
                    if exc_class is BatchConflict:
                        exc.status_code = self._conflict_status
                    raise exc


# ---------------------------------------------------------------------------
# factories
# ---------------------------------------------------------------------------


def build_collection_plan(child: BatchSerializer) -> list[CachedField]:
    """Resolve the collection plan; fail fast on bad configuration.

    Resolves related models and lookup fields, validates that every
    ``depends_on`` name names a declared cached field, and orders the plan
    topologically (declaration order as tie-break). Unknown names or cycles
    raise ``ImproperlyConfigured``.
    """
    fields = child.get_cached_fields()
    names = {f.field_name for f in fields if f.field_name is not None}
    for field in fields:
        if isinstance(field, CachedRelatedField):
            field._resolve_model()
            field._resolve_lookup_field()
        for dep in field.depends_on:
            if dep not in names:
                raise ImproperlyConfigured(
                    f"BatchListSerializer: cached field "
                    f"'{field.field_name}' depends_on unknown cached "
                    f"field '{dep}'. Declared cached fields: "
                    f"{', '.join(sorted(names)) or 'none'}."
                )
    ordered: list[CachedField] = []
    scheduled: set[str] = set()
    remaining = list(fields)
    while remaining:
        progressed = False
        for field in list(remaining):
            name = field.field_name
            if name is not None and all(dep in scheduled for dep in field.depends_on):
                ordered.append(field)
                scheduled.add(name)
                remaining.remove(field)
                progressed = True
        if not progressed:
            raise ImproperlyConfigured(
                "BatchListSerializer: circular depends_on among cached "
                "fields: " + " -> ".join(f.field_name or "?" for f in remaining)
            )
    return ordered


def build_pipeline(
    *,
    config: BatchConfig,
    child: BatchSerializer,
    model: type[models.Model] | None,
    plan: list[CachedField],
    conflict_rules: list[tuple[str, ...]],
) -> BatchPipeline:
    """Assemble the per-operation pass sequence (see the module docstring)."""
    # Local import: drf_batch.conflicts imports the pass base and context
    # from this module, so a module-level import would cycle.
    from drf_batch.conflicts import ConflictPass

    operation = config.operation
    cached_fields = child.get_cached_fields()
    read_only_fields = [f for f in cached_fields if f.read_only]

    if operation == BatchOperation.CREATE:
        pk_name = model._meta.pk.name if model is not None else "id"
        return BatchPipeline(
            [
                CreateExtractionPass(cached_fields),
                CollectionPass(plan, operation, pk_name, config.collector, None),
                RowValidationPass(child, operation, read_only_fields),
                ConflictPass(conflict_rules, model, pk_name, operation, db_check=True),
            ],
            config.conflict_status,
        )

    assert model is not None  # the facade requires Meta.model for update/destroy
    pk_field = model._meta.pk
    pk_name = pk_field.name
    configured = config.queryset

    def target_queryset() -> Any:
        if configured is not None:
            return configured.all()
        return default_manager(model).all()

    passes: list[PipelinePass]
    if operation == BatchOperation.UPDATE:
        passes = [
            UpdateExtractionPass(cached_fields, pk_field, pk_name),
            CollectionPass(plan, operation, pk_name, config.collector, target_queryset),
            NotFoundPass(pk_name),
        ]
    else:
        passes = [
            DestroyExtractionPass(pk_field, pk_name),
            CollectionPass([], operation, pk_name, None, target_queryset),
            NotFoundPass(pk_name),
        ]
    if config.check_object is not None:
        passes.append(PermissionPass(config.check_object))
    if operation == BatchOperation.UPDATE:
        passes += [
            RowValidationPass(child, operation, read_only_fields),
            ConflictPass(conflict_rules, model, pk_name, operation, db_check=True),
        ]
    else:
        passes.append(
            ConflictPass([(pk_name,)], model, pk_name, operation, db_check=False)
        )
    return BatchPipeline(passes, config.conflict_status)
