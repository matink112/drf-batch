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

- create:  extraction (400) -> keys -> collection -> row validation (400)
  -> conflict (409)
- update:  extraction (400) -> targets -> not found (404) -> permission (403)
  -> keys -> collection -> row validation (400) -> conflict (409)
- destroy: extraction (400) -> targets -> not found (404) -> permission (403)
  -> collection -> duplicate pk (409)

No fetch runs for a request that fails with 404 or 403. The keys pass fills
in the keys of cached fields the row did not send (``read_only`` fields
never read the row): derived from other fields (``derived_from``) or, on
update, the target's stored value.

Update/destroy targets are resolved from the configured queryset (the view's
``get_queryset()``), so a pk outside the caller's scope is a 404 exactly like
a pk that does not exist. A row is never validated against a target the
caller may not modify.

The write phase lives in :mod:`drf_batch.writers`; the 409 conflict pass in
:mod:`drf_batch.conflicts`. This module is internal and may change without
notice before 1.0.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, cast

from django.core.exceptions import ImproperlyConfigured, ObjectDoesNotExist
from django.core.exceptions import (
    ValidationError as DjangoValidationError,
)
from django.db import connections, models
from django.utils.translation import gettext_lazy
from rest_framework.exceptions import (
    APIException,
    NotAuthenticated,
    PermissionDenied,
    ValidationError,
)
from rest_framework.fields import get_attribute
from rest_framework.relations import ManyRelatedField, RelatedField

try:  # DRF >= 3.18
    from rest_framework.fields import get_error_detail
except ImportError:  # DRF < 3.18 kept it in rest_framework.exceptions
    from rest_framework.exceptions import get_error_detail  # type: ignore[attr-defined,no-redef]

from drf_batch import loop
from drf_batch._utils import default_manager, non_field_key
from drf_batch.collectors import BatchCollector, BatchContext
from drf_batch.config import BatchConfig, BatchOperation
from drf_batch.exceptions import (
    BatchCollectionTimeout,
    BatchConflict,
    BatchNotFound,
    BatchPermissionDenied,
)
from drf_batch.fields import (
    NO_KEY,
    CachedField,
    CachedRelatedField,
    _coerce_lookup_key,
    _model_field,
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
    # Extraction + keys output: {map: coerced keys} for the collection pass.
    key_sets: dict[str, set[Any]] = dataclasses.field(default_factory=dict)
    # Per-survivor {cached field name: key}: extraction records the keys the
    # row sent, the keys pass the rest (stored or derived; NO_KEY for none).
    # BatchSerializer.cached() reads it during row validation.
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
        """Coerce the key of every writable cached field the row sends.

        ``read_only`` fields never read the row; omitted fields get their
        key in the keys pass. Returns ``{field name: key}`` (a sent ``null``
        is :data:`NO_KEY`: the row wins, nothing is filled in for it).
        """
        row_key_map: dict[str, Any] = {}
        for field in self._cached_fields:
            # DRF reads the row's input key from the field name (the source
            # names the validated_data/output attribute).
            name = field.field_name
            assert name is not None  # set in bind()
            if field.read_only or name not in item:
                continue
            try:
                key = field.prepare_key(item[name])
            except ValidationError as exc:
                _merge_key_error(row_error, name, exc)
                continue
            row_key_map[name] = key
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


class TargetPass(PipelinePass):
    """update/destroy: fetch the targets from the scoped queryset.

    One ``in_bulk`` **on the request thread**, from the configured target
    queryset — never by a collector — so scoping cannot be bypassed and the
    lookup runs in the request's transaction.
    """

    name: ClassVar[str] = "targets"

    def __init__(self, target_queryset: Callable[[], Any]) -> None:
        self._target_queryset = target_queryset

    def run(self, ctx: PipelineContext) -> None:
        keys = set(ctx.row_pks.values())
        ctx.targets = (
            cast("dict[Any, Any]", self._target_queryset().in_bulk(keys))
            if keys
            else {}
        )


class _RowValues:
    """One row's values for ``derived_from``, read lazily and memoized.

    - a cached field: its key for the row (already resolved: key order);
    - a plain serializer field the row sends (not read-only): the field's
      ``to_internal_value``, ``None`` if invalid (row validation reports it);
    - otherwise the target's stored value (``None`` on create).
    """

    def __init__(
        self,
        plan: KeyPlan,
        item: dict[str, Any],
        target: Any,
        keys: dict[str, Any],
    ) -> None:
        self._plan = plan
        self._item = item
        self._target = target
        self._keys = keys
        self._memo: dict[str, Any] = {}

    def get(self, name: str) -> Any:
        if name in self._keys:
            key = self._keys[name]
            return None if key is NO_KEY else key
        if name not in self._memo:
            self._memo[name] = self._read(name)
        return self._memo[name]

    def _read(self, name: str) -> Any:
        field = self._plan.plain_fields.get(name)
        if field is not None and not field.read_only and name in self._item:
            raw = self._item[name]
            if raw is None:
                return None
            try:
                return field.to_internal_value(raw)
            except (ValidationError, DjangoValidationError):
                return None
        if self._target is None:
            return None
        if field is not None:
            try:
                return get_attribute(self._target, field.source_attrs)
            except (AttributeError, KeyError, ObjectDoesNotExist):
                return None
        return getattr(self._target, self._plan.model_attnames[name], None)


@dataclass
class KeyPlan:
    """The construction-time plan of the keys pass (see :func:`build_key_plan`)."""

    # Cached fields in key order (derived_from sources first).
    fields: list[CachedField]
    # Field name -> where an update target stores its key (only for the
    # fields that use it).
    stored_paths: dict[str, tuple[str, ...]]
    # Relations the update target query joins for the stored keys.
    select_related: list[str]
    # derived_from inputs: plain serializer fields, and model fields that are
    # not on the serializer (name -> attname).
    plain_fields: dict[str, Any]
    model_attnames: dict[str, str]


class KeyPass(PipelinePass):
    """Fill in the keys of the cached fields each row did not send.

    Per row, in key order: a field the row sent keeps its key; otherwise
    ``derived_from`` builds it, or, on update, the target's stored value
    (see :class:`KeyPlan`). The keys join the key sets (fetches and a
    replacement collector see them) and ``ctx.row_keys``. A key the row did
    not send is never a "not found" error.
    """

    name: ClassVar[str] = "keys"

    def __init__(self, plan: KeyPlan) -> None:
        self._plan = plan

    def run(self, ctx: PipelineContext) -> None:
        stored_paths = self._plan.stored_paths
        for index, item in ctx.survivors:
            pk = ctx.row_pks.get(index)
            target = ctx.targets.get(pk) if pk is not None else None
            keys = ctx.row_keys.setdefault(index, {})
            values = _RowValues(self._plan, item, target, keys)
            for field in self._plan.fields:
                name = field.field_name
                assert name is not None  # set in bind()
                if name in keys:
                    continue
                key: Any = NO_KEY
                if field.derived_from:
                    key = field.derived_key(
                        tuple(values.get(source) for source in field.derived_from)
                    )
                elif target is not None and name in stored_paths:
                    key = field.stored_key(target, stored_paths[name])
                keys[name] = key
                if key is not NO_KEY:
                    ctx.key_sets.setdefault(field.map, set()).add(key)


class CollectionPass(PipelinePass):
    """Run the collector or the auto plan over the key sets.

    update/destroy targets (fetched by :class:`TargetPass`) are exposed as
    ``collected[pk_name]``. Cached fields are collected for create and
    update (destroy has no row payload).

    The auto plan runs as a dependency graph (``concurrent=True``, the
    default): one ``asyncio.TaskGroup`` on the event loop, every fetch
    started as soon as its own ``depends_on`` are done, sync fetches on
    worker threads — or, when the request's connection is in a transaction,
    on the request thread, which serves them one at a time while it waits,
    so they see the transaction. ``concurrent=False`` runs the plan one
    fetch at a time:
    sync fetches inline on the request thread, async ones bridged one by
    one, under one shared timeout.
    """

    name: ClassVar[str] = "collection"

    def __init__(
        self,
        plan: list[CachedField],
        operation: BatchOperation,
        pk_name: str,
        collector: BatchCollector | None,
        *,
        concurrent: bool = True,
    ) -> None:
        self._plan = plan
        self._operation = operation
        self._pk_name = pk_name
        self._collector = collector
        self._concurrent = concurrent

    def run(self, ctx: PipelineContext) -> None:
        seed: dict[str, Any] = {}
        if self._operation != BatchOperation.CREATE:
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
            result = loop.run_in_loop(
                result, timeout=timeout, serve_caller=_needs_request_thread()
            )
        return dict(cast("dict[str, Any]", result))

    def _skipped(self, ctx: PipelineContext, field: CachedField) -> bool:
        """An empty key set makes this fetch provably ``{}``: no call."""
        return field.skip_fetch_on_empty_keys and not ctx.key_sets.get(field.map)

    def _run_plan(self, ctx: PipelineContext, seed: dict[str, Any]) -> dict[str, Any]:
        pending = [
            f for f in self._plan if f.map not in seed and not self._skipped(ctx, f)
        ]
        if not pending:
            # Nothing to fetch: no loop, no thread.
            collected = dict(seed)
            for field in self._plan:
                collected.setdefault(field.map, {})
            return collected
        if not self._concurrent:
            return self._run_sequential(ctx, seed)
        result = loop.run_in_loop(
            self._run_graph(ctx, seed),
            timeout=get_setting("COLLECT_TIMEOUT"),
            serve_caller=_needs_request_thread(),
        )
        return cast("dict[str, Any]", result)

    async def _run_graph(
        self, ctx: PipelineContext, seed: dict[str, Any]
    ) -> dict[str, Any]:
        """Every fetch as a task that waits for its own ``depends_on`` only.

        With 2 and 3 depending on 1, 4 on 3 and 6 on 5, fetches 1, 5 and 7
        start together; 2 and 3 when 1 is done; 4 when 3 is done (not
        waiting for 2); 6 when 5 is done.

        Sync fetches go through :func:`drf_batch.loop.run_sync`: on worker
        threads (concurrently), or, when the request's connection is in a
        transaction, on the request thread (one at a time, seeing it).
        """
        collected = dict(seed)
        by_name: dict[str | None, asyncio.Task[None]] = {}
        by_map: dict[str, asyncio.Task[None]] = {}

        async def run(field: CachedField, deps: list[asyncio.Task[None]]) -> None:
            if deps:
                await asyncio.gather(*deps)
            # A snapshot: seed + everything finished so far, which includes
            # every (transitive) depends_on. Never the live dict, which
            # other tasks keep writing to.
            snapshot = dict(collected)
            keys = ctx.key_sets.get(field.map, set())
            if field.is_async:
                result = await field.fetch(keys, snapshot, ctx.serializer_context)
            else:
                result = await loop.run_sync(
                    field.fetch, keys, snapshot, ctx.serializer_context
                )
            if inspect.isawaitable(result):
                result = await result
            collected[field.map] = result

        try:
            async with asyncio.TaskGroup() as group:
                for field in self._plan:  # topological: dependencies first
                    if field.map in seed:
                        continue
                    if field.map in by_map:
                        # A map shared by two fields is fetched once.
                        by_name[field.field_name] = by_map[field.map]
                        continue
                    if self._skipped(ctx, field):
                        collected[field.map] = {}
                        continue
                    deps = [by_name[d] for d in field.depends_on if d in by_name]
                    task = group.create_task(run(field, deps))
                    by_name[field.field_name] = by_map[field.map] = task
        except BaseExceptionGroup as group_error:
            # Keep the contract: the fetch's own exception propagates.
            raise _first_leaf(group_error) from None
        # Plan order (topological, declaration tie-break), not completion order.
        ordered = dict(seed)
        for field in self._plan:
            if field.map in collected:
                ordered.setdefault(field.map, collected[field.map])
        return ordered

    def _run_sequential(
        self, ctx: PipelineContext, seed: dict[str, Any]
    ) -> dict[str, Any]:
        timeout = get_setting("COLLECT_TIMEOUT")
        deadline = None if timeout is None else time.monotonic() + timeout
        collected = dict(seed)
        for field in self._plan:
            if field.map in collected:
                continue
            if self._skipped(ctx, field):
                collected[field.map] = {}
                continue
            keys = ctx.key_sets.get(field.map, set())
            result = field.fetch(keys, collected, ctx.serializer_context)
            if inspect.isawaitable(result):
                remaining = None
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        loop._close_unawaited(result)
                        raise BatchCollectionTimeout()
                result = loop.run_in_loop(result, timeout=remaining)
            collected[field.map] = result
        return collected


def _needs_request_thread() -> bool:
    """Whether sync fetches must run on this (the request) thread.

    Other threads' connections cannot see what this thread's connection
    sees when it is inside ``atomic()`` (``ATOMIC_REQUESTS``, an outer
    ``atomic()``, test cases that wrap each test in a transaction: they
    would miss its uncommitted writes), or when a database is a private
    in-memory SQLite database (``NAME = ":memory:"``: every connection is a
    separate, empty database).
    """
    if any(conn.in_atomic_block for conn in connections.all(initialized_only=True)):
        return True
    return any(_is_private_memory_db(conn) for conn in connections.all())


def _is_private_memory_db(conn: Any) -> bool:
    if conn.vendor != "sqlite":
        return False
    name = str(conn.settings_dict.get("NAME") or "")
    in_memory = name == ":memory:" or "mode=memory" in name
    return in_memory and "cache=shared" not in name


def _first_leaf(group: BaseExceptionGroup[Any]) -> BaseException:
    exc: BaseException = group
    while isinstance(exc, BaseExceptionGroup):
        exc = exc.exceptions[0]
    return exc


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
    pattern); every row with its keys exposed to ``child.cached()``.
    """

    name: ClassVar[str] = "row_validation"

    def __init__(self, child: BatchSerializer, operation: BatchOperation) -> None:
        self._child = child
        self._operation = operation

    def run(self, ctx: PipelineContext) -> None:
        is_update = self._operation == BatchOperation.UPDATE
        try:
            for index, item in ctx.survivors:
                pk = ctx.row_pks.get(index) if is_update else None
                if is_update:
                    self._child.instance = ctx.targets[pk]
                self._child._batch_row_keys = ctx.row_keys.get(index, {})
                try:
                    validated = self._child.run_validation(item)
                except ValidationError as exc:
                    ctx.row_errors[index] = _normalize_row_error(exc.detail)
                    continue
                except DjangoValidationError as exc:
                    ctx.row_errors[index] = _normalize_row_error(get_error_detail(exc))
                    continue
                ctx.rows.append((index, validated, pk))
        finally:
            if is_update:
                self._child.instance = None
            self._child._batch_row_keys = None


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


def build_key_plan(child: BatchSerializer, model: type[models.Model] | None) -> KeyPlan:
    """Resolve the keys pass; fail fast on bad ``derived_from`` configuration.

    Validates every ``derived_from`` name (a serializer field other than the
    field itself and not a plain DRF relation, or a concrete field of
    ``Meta.model``), orders the cached fields so ``derived_from`` sources
    come first (cycles raise), and plans the stored keys: for every cached
    field without ``derived_from`` whose source is stored on ``Meta.model``
    — except a writable related field that no other cached field reads
    (``derived_from`` / ``depends_on``), whose stored key nothing needs.
    """
    fields = child.get_cached_fields()
    cached_names = {f.field_name for f in fields}
    plain_fields: dict[str, Any] = {}
    model_attnames: dict[str, str] = {}
    for field in fields:
        prefix = f"BatchListSerializer: cached field '{field.field_name}' derived_from"
        for name in field.derived_from:
            if name == field.field_name:
                raise ImproperlyConfigured(f"{prefix} names the field itself.")
            if name in cached_names:
                continue
            declared = child.fields.get(name)
            if isinstance(declared, (RelatedField, ManyRelatedField)):
                raise ImproperlyConfigured(
                    f"{prefix} '{name}': a plain related field would query per "
                    "row; declare it as a CachedRelatedField."
                )
            if declared is not None:
                plain_fields[name] = declared
                continue
            model_field = _model_field(model, name) if model is not None else None
            if model_field is None or not model_field.concrete:
                raise ImproperlyConfigured(
                    f"{prefix} unknown field '{name}': name a serializer field "
                    "or a concrete field of Meta.model."
                )
            model_attnames[name] = model_field.attname

    ordered: list[CachedField] = []
    done: set[str | None] = set()
    remaining = list(fields)
    while remaining:
        ready = [
            f
            for f in remaining
            if all(n in done or n not in cached_names for n in f.derived_from)
        ]
        if not ready:
            raise ImproperlyConfigured(
                "BatchListSerializer: circular derived_from among cached "
                "fields: " + " -> ".join(f.field_name or "?" for f in remaining)
            )
        for field in ready:
            ordered.append(field)
            done.add(field.field_name)
            remaining.remove(field)

    read = {
        name
        for f in fields
        for name in (*f.derived_from, *f.depends_on)
        if name != f.field_name
    }
    stored_paths: dict[str, tuple[str, ...]] = {}
    select_related: set[str] = set()
    for field in fields:
        field_name = field.field_name
        assert field_name is not None  # set in bind()
        if model is None or field.derived_from:
            continue
        if (
            isinstance(field, CachedRelatedField)
            and not field.read_only
            and field_name not in read
        ):
            continue
        stored = field.stored_key_path(model)
        if stored is None:
            continue
        stored_paths[field_name], select = stored
        if select is not None:
            select_related.add(select)
    return KeyPlan(
        fields=ordered,
        stored_paths=stored_paths,
        select_related=sorted(select_related),
        plain_fields=plain_fields,
        model_attnames=model_attnames,
    )


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

    if operation == BatchOperation.CREATE:
        pk_name = model._meta.pk.name if model is not None else "id"
        return BatchPipeline(
            [
                CreateExtractionPass(cached_fields),
                KeyPass(build_key_plan(child, model)),
                CollectionPass(
                    plan,
                    operation,
                    pk_name,
                    config.collector,
                    concurrent=config.concurrent_collection,
                ),
                RowValidationPass(child, operation),
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
        key_plan = build_key_plan(child, model)
        selects = key_plan.select_related

        def update_target_queryset() -> Any:
            # Join exactly what the stored keys read across a relation
            # (ticker.isin), so they cost no query per row. Only here: the
            # write's select_for_update re-fetch stays join-free.
            queryset = target_queryset()
            return queryset.select_related(*selects) if selects else queryset

        passes = [
            UpdateExtractionPass(cached_fields, pk_field, pk_name),
            TargetPass(update_target_queryset),
            NotFoundPass(pk_name),
        ]
    else:
        passes = [
            DestroyExtractionPass(pk_field, pk_name),
            TargetPass(target_queryset),
            NotFoundPass(pk_name),
        ]
    if config.check_object is not None:
        passes.append(PermissionPass(config.check_object))
    if operation == BatchOperation.UPDATE:
        passes += [
            KeyPass(key_plan),
            CollectionPass(
                plan,
                operation,
                pk_name,
                config.collector,
                concurrent=config.concurrent_collection,
            ),
            RowValidationPass(child, operation),
            ConflictPass(conflict_rules, model, pk_name, operation, db_check=True),
        ]
    else:
        passes += [
            CollectionPass([], operation, pk_name, None),
            ConflictPass([(pk_name,)], model, pk_name, operation, db_check=False),
        ]
    return BatchPipeline(passes, config.conflict_status)
