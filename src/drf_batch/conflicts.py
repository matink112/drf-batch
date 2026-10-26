"""The 409 conflict pass: cross-row and DB uniqueness rules.

Rules come from two places, resolved up front (:func:`resolve_conflict_rules`,
fail-fast ``ImproperlyConfigured``):

- ``Meta.batch_unique_together`` on the serializer (explicit);
- the model's own uniqueness — non-pk ``unique=True`` fields,
  ``Meta.unique_together`` and unconditional field-only ``UniqueConstraint``s
  — for every rule whose fields are all writable on the serializer. Opt out
  with ``Meta.batch_model_unique = False``.

DRF's per-row ``UniqueValidator`` / ``UniqueTogetherValidator`` for those
rules are stripped from the child (:func:`strip_owned_unique_validators`):
they cost one query per row and cannot see duplicates inside the payload.

:class:`ConflictPass` enforces the rules during validation:

- in-payload — rows are grouped by key tuple; any group of more than one row
  conflicts ("Conflicts with another row in this batch." under each rule
  field). Key tuples containing ``None`` never conflict (SQL ``UNIQUE``
  treats NULLs as distinct);
- DB (when ``db_check`` is on) — one query per rule (cartesian over-fetch,
  filtered in memory; skipped when any rule field's value set is empty);
  create conflicts with any match, update conflicts unless the existing pk is
  the row's own target.

The pass only records failures on ``ctx.conflict_errors`` (index-aligned);
the :class:`~drf_batch.pipeline.BatchPipeline` coordinator raises. For
destroy the rule is ``(pk_name,)`` with ``db_check=False`` (duplicate pks).

Internal module; may change without notice before 1.0.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from django.core.exceptions import FieldDoesNotExist, ImproperlyConfigured
from django.db import models
from django.utils.translation import gettext_lazy
from rest_framework.validators import UniqueTogetherValidator, UniqueValidator

from drf_batch._utils import default_manager
from drf_batch.fields import _source_attribute
from drf_batch.pipeline import PipelineContext, PipelinePass

if TYPE_CHECKING:
    from drf_batch.serializers import BatchSerializer


def _meta(child: BatchSerializer) -> Any:
    return getattr(child, "Meta", None)


def _writable_model_fields(child: BatchSerializer) -> set[str]:
    """Model attribute names the child serializer writes."""
    return {
        attr
        for field in child.fields.values()
        if not field.read_only and (attr := _source_attribute(field)) is not None
    }


def _model_unique_rules(model: type[models.Model]) -> list[tuple[str, ...]]:
    rules: list[tuple[str, ...]] = [
        (field.name,)
        for field in model._meta.concrete_fields
        if field.unique and not field.primary_key
    ]
    rules.extend(tuple(fields) for fields in model._meta.unique_together)
    # Django's own notion of "unconditional, field-only" unique constraints.
    rules.extend(tuple(c.fields) for c in model._meta.total_unique_constraints)
    return rules


def resolve_conflict_rules(child: BatchSerializer) -> list[tuple[str, ...]]:
    """Explicit + model-derived rules, normalized and de-duplicated."""
    meta = _meta(child)
    explicit = getattr(meta, "batch_unique_together", ())
    model = getattr(meta, "model", None)
    if explicit and model is None:
        raise ImproperlyConfigured(
            "BatchListSerializer: batch_unique_together requires a "
            "Meta.model on the child serializer."
        )
    if model is None:
        return []

    normalized: list[tuple[str, ...]] = []
    for rule in explicit:
        fields = (rule,) if isinstance(rule, str) else tuple(rule)
        if not fields:
            raise ImproperlyConfigured(
                "BatchListSerializer: a batch_unique_together rule must "
                "name at least one field."
            )
        for f in fields:
            try:
                model_field = model._meta.get_field(f)
            except FieldDoesNotExist:
                raise ImproperlyConfigured(
                    f"BatchListSerializer: batch_unique_together field "
                    f"'{f}' is not a field on {model.__name__}."
                ) from None
            if not model_field.concrete:
                raise ImproperlyConfigured(
                    f"BatchListSerializer: batch_unique_together field "
                    f"'{f}' is not a concrete field on {model.__name__}."
                )
        normalized.append(fields)

    if getattr(meta, "batch_model_unique", True):
        writable = _writable_model_fields(child)
        normalized.extend(
            rule
            for rule in _model_unique_rules(model)
            if all(f in writable for f in rule)
        )

    # De-duplicate (order-insensitive per rule, first occurrence wins).
    seen: set[frozenset[str]] = set()
    rules: list[tuple[str, ...]] = []
    for rule in normalized:
        key = frozenset(rule)
        if key not in seen:
            seen.add(key)
            rules.append(rule)
    return rules


def strip_owned_unique_validators(
    child: BatchSerializer, rules: list[tuple[str, ...]]
) -> None:
    """Remove DRF unique validators whose rule the conflict pass enforces."""
    owned = {frozenset(rule) for rule in rules}
    if not owned:
        return
    for field in child.fields.values():
        attr = _source_attribute(field)
        if attr is not None and frozenset((attr,)) in owned:
            field.validators = [
                v for v in field.validators if not isinstance(v, UniqueValidator)
            ]
    child.validators = [
        v
        for v in child.validators
        if not (
            isinstance(v, UniqueTogetherValidator)
            and getattr(v, "condition", None) is None
            and frozenset(v.fields) in owned
        )
    ]


class ConflictPass(PipelinePass):
    """Enforce the conflict rules (in-payload + DB) -> 409.

    Records into ``ctx.conflict_errors`` (index-aligned); the coordinator
    raises. A no-op when no rules apply.
    """

    name: ClassVar[str] = "conflict"

    def __init__(
        self,
        conflict_rules: list[tuple[str, ...]],
        model: type[models.Model] | None,
        pk_name: str,
        operation: str,
        db_check: bool,
    ) -> None:
        self._conflict_rules = conflict_rules
        self._model = model
        self._pk_name = pk_name
        self._operation = operation
        self._db_check = db_check

    def run(self, ctx: PipelineContext) -> None:
        rules = self._conflict_rules
        if not rules:
            return
        assert self._model is not None  # resolve_conflict_rules requires it

        # In-payload: group rows by key tuple; any group of >1 conflicts.
        message = str(gettext_lazy("Conflicts with another row in this batch."))
        for rule in rules:
            groups: dict[tuple[Any, ...], list[int]] = {}
            for index, payload, pk in ctx.rows:
                key = self._row_key_tuple(ctx, payload, pk, rule)
                if None in key:
                    continue  # NULLs are distinct under SQL UNIQUE
                groups.setdefault(key, []).append(index)
            for indices in groups.values():
                if len(indices) > 1:
                    for index in indices:
                        err = ctx.conflict_errors.setdefault(index, {})
                        for f in rule:
                            err.setdefault(f, []).append(message)

        if self._db_check:
            for rule in rules:
                self._check_db(ctx, rule)

    def _check_db(self, ctx: PipelineContext, rule: tuple[str, ...]) -> None:
        # One query per rule (cartesian over-fetch, filtered in memory).
        field_values: dict[str, set[Any]] = {f: set() for f in rule}
        for _index, payload, pk in ctx.rows:
            key = self._row_key_tuple(ctx, payload, pk, rule)
            for f, value in zip(rule, key, strict=True):
                if value is not None:
                    field_values[f].add(value)
        if not all(field_values[f] for f in rule):
            return
        assert self._model is not None
        # Uniqueness is table-wide, not tenant-scoped: deliberately the
        # default manager, never the view's (scoped) queryset.
        qs = default_manager(self._model).all()
        for f in rule:
            qs = qs.filter(**{f"{f}__in": list(field_values[f])})
        existing_map: dict[tuple[Any, ...], set[Any]] = {}
        for row in qs.values(self._pk_name, *rule):
            tuple_key = tuple(row[f] for f in rule)
            existing_map.setdefault(tuple_key, set()).add(row[self._pk_name])
        message = str(gettext_lazy("Conflicts with an existing record."))
        for index, payload, pk in ctx.rows:
            key = self._row_key_tuple(ctx, payload, pk, rule)
            pks = existing_map.get(key)
            if pks is None:
                continue
            if self._operation == "create" or pk not in pks:
                err = ctx.conflict_errors.setdefault(index, {})
                for f in rule:
                    err.setdefault(f, []).append(message)

    def _row_key_tuple(
        self,
        ctx: PipelineContext,
        payload: Any,
        pk: Any,
        rule: tuple[str, ...],
    ) -> tuple[Any, ...]:
        key: list[Any] = []
        for f in rule:
            if self._operation == "destroy":
                # Destroy rows are (index, pk, pk): the scalar pk is the
                # whole payload, and the rule is always (pk_name,).
                value = pk
            elif self._operation == "update" and f not in payload:
                value = self._current_field_value(ctx, pk, f)
            else:
                value = payload.get(f)
            key.append(_conflict_key_value(value))
        return tuple(key)

    def _current_field_value(self, ctx: PipelineContext, pk: Any, field: str) -> Any:
        instance = ctx.targets.get(pk)
        if instance is None:
            return None
        assert self._model is not None
        model_field = self._model._meta.get_field(field)
        # FKs compare by their raw id column (``ticker_id``).
        return getattr(instance, getattr(model_field, "attname", field))


def _conflict_key_value(value: Any) -> Any:
    """Normalize a conflict-rule value to a comparable, hashable form.

    Model instances (FK values) are reduced to their pk so they compare
    equal to the raw values returned by ``QuerySet.values()``.
    """
    if isinstance(value, models.Model):
        return value.pk
    return value
