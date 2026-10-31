"""drf-spectacular support: accurate OpenAPI for ``/batch/`` endpoints.

Importing this module registers two extensions (``"drf_batch"`` in
``INSTALLED_APPS`` imports it automatically when drf-spectacular is
installed):

- a view extension for every :class:`~drf_batch.BatchMixin` viewset, which
  documents the batch operations: array request bodies (create rows;
  partial update rows with a required pk; full replace rows with a required
  pk; destroy pks), the success body
  (``{"count": N}`` or the data, per ``batch_response``) and the
  index-aligned 400/403/404/409 bodies plus 413/504;
- a field extension that types batch fields by their lookup field
  (``BatchRelatedField(lookup="isin")`` is a string, a pk lookup an
  integer/UUID, ...).

The schema is derived from the class attributes (``batch_serializer_class``
or ``serializer_class``, ``batch_actions``, ``batch_response``); dynamic
``get_*`` overrides are not consulted.
"""

from __future__ import annotations

import functools
from typing import Any

from django.db import models
from drf_spectacular.drainage import isolate_view_method
from drf_spectacular.extensions import (
    OpenApiSerializerFieldExtension,
    OpenApiViewExtension,
)
from drf_spectacular.plumbing import build_basic_type
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers

from drf_batch.fields import BatchField, BatchRelatedField

_ROW_ERRORS = {
    "type": "array",
    "description": "Index-aligned row errors (`{}` for rows that would have applied).",
    "items": {
        "type": "object",
        "additionalProperties": {"type": "array", "items": {"type": "string"}},
    },
}
_DETAIL = {"type": "object", "properties": {"detail": {"type": "string"}}}


class BatchCountSerializer(serializers.Serializer):
    """The default success body: ``{"count": N}``."""

    count = serializers.IntegerField()


def _base_name(serializer_class: type) -> str:
    name = serializer_class.__name__
    return name.removesuffix("Serializer") or name


def _pk_schema(model: type[models.Model]) -> dict[str, Any]:
    pk = model._meta.pk
    if isinstance(pk, models.UUIDField):
        return {"type": "string", "format": "uuid"}
    if isinstance(pk, models.IntegerField):
        return {"type": "integer"}
    return {"type": "string"}


def _pk_serializer_field(model: type[models.Model]) -> serializers.Field:
    pk = model._meta.pk
    if isinstance(pk, models.UUIDField):
        return serializers.UUIDField()
    if isinstance(pk, models.IntegerField):
        return serializers.IntegerField()
    return serializers.CharField()


@functools.cache
def _update_row_class(
    child_class: type[serializers.ModelSerializer],
    model: type[models.Model],
    kind: str = "Update",
) -> type[serializers.ModelSerializer]:
    """The child plus a writable pk field (one class per child and kind: one
    component each, so the partial and the full row never share a name)."""
    pk_name = model._meta.pk.name
    meta_attrs: dict[str, Any] = {}
    fields = getattr(child_class.Meta, "fields", None)
    if isinstance(fields, (list, tuple)) and pk_name not in fields:
        meta_attrs["fields"] = (*fields, pk_name)
    return type(
        f"{_base_name(child_class)}Batch{kind}RowSerializer",
        (child_class,),
        {
            pk_name: _pk_serializer_field(model),
            "Meta": type("Meta", (child_class.Meta,), meta_attrs),
            "__module__": child_class.__module__,
        },
    )


def _update_row_serializer(
    child_class: type[serializers.ModelSerializer], model: type[models.Model]
) -> serializers.Serializer:
    """An update row: required pk, every other writable field optional."""
    pk_name = model._meta.pk.name
    row = _update_row_class(child_class, model)()
    for name, field in row.fields.items():
        if not field.read_only:
            field.required = name == pk_name
    return row


def _replace_row_serializer(
    child_class: type[serializers.ModelSerializer], model: type[models.Model]
) -> serializers.Serializer:
    """A replace (PUT) row: required pk, the child's own required fields."""
    return _update_row_class(child_class, model, "Replace")()


def _responses(
    success: Any, status: int, *, not_found: bool
) -> dict[int, OpenApiResponse]:
    responses = {
        status: OpenApiResponse(response=success),
        400: OpenApiResponse(response=_ROW_ERRORS, description="Validation error."),
        409: OpenApiResponse(
            response=_ROW_ERRORS,
            description=(
                "Conflict with another row or existing data. A database "
                "constraint violation at write time returns `{detail}` instead."
            ),
        ),
        413: OpenApiResponse(response=_DETAIL, description="Batch too large."),
        504: OpenApiResponse(response=_DETAIL, description="Collection timed out."),
    }
    if not_found:
        responses[404] = OpenApiResponse(
            response=_ROW_ERRORS, description="Target not found (or out of scope)."
        )
        responses[403] = OpenApiResponse(
            response=_ROW_ERRORS, description="Object permission denied."
        )
    return responses


class BatchMixinViewExtension(OpenApiViewExtension):
    target_class = "drf_batch.mixins.BatchMixin"
    match_subclasses = True

    def view_replacement(self) -> Any:
        view = self.target
        child_class = view.batch_serializer_class or getattr(
            view, "serializer_class", None
        )
        if child_class is None:
            return view
        model = child_class.Meta.model
        base = _base_name(child_class)
        enabled = set(view.batch_actions)
        data_mode = getattr(view, "batch_response", "count") == "data"
        count = BatchCountSerializer
        pk_array = {"type": "array", "items": _pk_schema(model)}

        schemas: dict[str, Any] = {}
        if "create" in enabled:
            schemas["batch_create"] = extend_schema(
                summary=f"Batch-create {base} rows (all-or-none)",
                request=child_class(many=True),
                responses=_responses(
                    child_class(many=True) if data_mode else count,
                    201,
                    not_found=False,
                ),
            )
        if "update" in enabled:
            schemas["batch_update"] = extend_schema(
                summary=f"Batch-update {base} rows by primary key (partial)",
                request=serializers.ListSerializer(
                    child=_update_row_serializer(child_class, model)
                ),
                responses=_responses(
                    child_class(many=True) if data_mode else count,
                    200,
                    not_found=True,
                ),
            )
        if "replace" in enabled:
            schemas["batch_replace"] = extend_schema(
                summary=f"Batch-replace {base} rows by primary key (full rows)",
                request=serializers.ListSerializer(
                    child=_replace_row_serializer(child_class, model)
                ),
                responses=_responses(
                    child_class(many=True) if data_mode else count,
                    200,
                    not_found=True,
                ),
            )
        if "destroy" in enabled:
            schemas["batch_destroy"] = extend_schema(
                summary=f"Batch-delete {base} rows by primary key",
                # OpenAPI tooling drops DELETE request bodies; describe it.
                description=(
                    "Request body: a JSON array of primary keys, e.g. "
                    "`[1, 2, 3]`. All-or-none."
                ),
                responses=_responses(
                    pk_array if data_mode else count, 200, not_found=True
                ),
            )
        # Disabled operations answer 405: keep them out of the schema.
        for operation, method_name in (
            ("create", "batch_create"),
            ("update", "batch_update"),
            ("replace", "batch_replace"),
            ("destroy", "batch_destroy"),
        ):
            if operation not in enabled:
                schemas[method_name] = extend_schema(exclude=True)
        # The PATCH/PUT/DELETE handlers are ``@batch_create.mapping`` methods,
        # which extend_schema_view does not see: decorate isolated copies on
        # a subclass directly (never the mixin's own functions).
        replacement = type(view.__name__, (view,), {"__module__": view.__module__})
        for method_name, decorator in schemas.items():
            decorator(isolate_view_method(replacement, method_name))
        return replacement


class BatchFieldExtension(OpenApiSerializerFieldExtension):
    target_class = "drf_batch.fields.BatchField"
    match_subclasses = True

    def map_serializer_field(self, auto_schema: Any, direction: Any) -> Any:
        field: BatchField = self.target
        if isinstance(field, BatchRelatedField):
            schema = dict(
                auto_schema._map_model_field(field._resolve_lookup_field(), direction)
            )
            # The lookup column may be non-editable (an auto pk); the batch
            # field itself is writable unless declared read-only.
            schema.pop("readOnly", None)
            return schema
        return build_basic_type(OpenApiTypes.ANY)
