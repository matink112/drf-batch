"""Collection fields: resolve a row value against the request's collected maps.

Collection is **serializer-driven** (v0.3): a ``BatchSerializer`` declares
which of its fields are *cached*, and the list serializer auto-collects the
needed lookups before any row is validated — the common case needs no
collector class at all.

- :class:`CachedField` is the base: it owns the map name, the miss/error
  templates, ``prepare_key`` (row value -> lookup key) and the ``collected``
  lookup; subclasses provide :meth:`CachedField.fetch`.
- :class:`CachedRelatedField` auto-derives its model from the same-named
  ForeignKey on ``Meta.model`` (explicit ``model=`` for non-FK lookups) and
  coerces row values to the lookup field's type.
- :class:`CachedServiceField` fetches the map with a user callable
  (sync or ``async def``).

A hit returns the **map value** (for related fields: the model instance), so
``ModelSerializer.create`` can pass it straight through as an FK value and a
user ``validate()`` can inspect it with zero I/O during validation.
"""

from __future__ import annotations

import decimal
import inspect
import uuid
from collections.abc import Callable
from typing import Any, cast
from uuid import UUID

from django.core.exceptions import FieldDoesNotExist, ImproperlyConfigured
from django.db import connection, models
from django.utils.translation import gettext_lazy
from rest_framework import serializers
from rest_framework.exceptions import ValidationError

from drf_batch._utils import default_manager
from drf_batch.collectors import BatchContext


class _NoKey:
    """Sentinel: the row had no value for the field (missing or null)."""

    def __repr__(self) -> str:
        return "<NO_KEY>"


NO_KEY = _NoKey()


def _source_attribute(field: serializers.Field) -> str | None:
    """The model attribute a field maps to.

    DRF reads the row's input key from the field name and places the
    validated value under the field's ``source``; for ``Model(**row)`` and
    FK auto-resolution only the first source segment is the attribute name.
    ``source='*'`` (virtual) maps to nothing.
    """
    source = field.source or field.field_name
    if source in (None, "*"):
        return field.field_name
    return str(source).split(".")[0]


class CachedField(serializers.Field):
    """Base class for fields resolved against the request's collected maps.

    :param map: key into the request's ``collected`` maps; defaults to the
        field name.
    :param error: optional template for the miss message; ``{value}`` is
        replaced with the offending (coerced) value. Defaults to
        ``"Object '{value}' not found."``.
    :param depends_on: names of other cached fields whose fetches must
        finish first. Fetches without a dependency between them run
        concurrently; every fetch receives a snapshot of the maps collected
        so far, which always includes its ``depends_on`` maps.
    """

    default_error_messages = {  # noqa: RUF012
        "not_found": gettext_lazy("Object '{value}' not found."),
        "invalid_key": gettext_lazy(
            "Invalid value for '{field}': expected a {expected}, got {actual}."
        ),
    }

    def __init__(
        self,
        map: str | None = None,
        *,
        error: str | None = None,
        depends_on: Any = (),
        **kwargs: Any,
    ) -> None:
        self._map = map
        self.error = error
        self.depends_on = tuple(depends_on)
        super().__init__(**kwargs)

    def bind(self, field_name: str, parent: Any) -> None:
        super().bind(field_name, parent)
        self.map = self._map or field_name

    @property
    def _collected(self) -> dict[str, Any]:
        parent = self.parent
        collected = getattr(parent, "collected", None) if parent is not None else None
        return collected if isinstance(collected, dict) else {}

    @property
    def is_async(self) -> bool:
        """Whether :meth:`fetch` is an ``async def`` (auto-detected)."""
        return inspect.iscoroutinefunction(type(self).fetch)

    @property
    def skip_fetch_on_empty_keys(self) -> bool:
        """Whether an empty key set makes the fetch provably ``{}``.

        A database lookup is one (``in_bulk(set())`` is empty, so the query
        is skipped); a service fetch may build its map from the other
        collected maps, so :class:`CachedServiceField` keeps it running.
        """
        return True

    def prepare_key(self, value: Any) -> Any:
        """Coerce the raw row value to a lookup key.

        Must be **idempotent**. ``None`` (missing or null in the row) maps to
        the :data:`NO_KEY` sentinel: the field contributes no key, and
        required/blank handling happens during row validation.
        """
        if value is None:
            return NO_KEY
        return value

    def fetch(
        self, keys: set[Any], collected: dict[str, Any], context: BatchContext
    ) -> Any:
        """Return the ``{lookup_key: value}`` map for ``keys``.

        ``collected`` is a snapshot of the maps finished so far (always
        including this field's ``depends_on``); ``context`` is the
        serializer context. May be a sync method (run on a worker thread,
        or on the request thread inside a transaction) or an ``async def``
        (run on the event loop).
        """
        raise NotImplementedError

    def to_internal_value(self, value: Any) -> Any:
        key = self.prepare_key(value)
        if key is NO_KEY:
            key = None
        mapping = self._collected.get(self.map) or {}
        instance = mapping.get(key)
        if instance is None:
            template = (
                self.error
                if self.error is not None
                else self.default_error_messages["not_found"]
            )
            raise ValidationError(str(template).format(value=key))
        return instance

    def to_representation(self, value: Any) -> Any:
        """Return the lookup key that maps to ``value`` in the collected map.

        The reverse map is built once per request and cached in the
        serializer context next to the mapping it was built from (so it can
        never outlive or mismatch that mapping). Outside a batch request
        (nothing collected) the value is returned unchanged. Duplicate map
        entries pointing at the same value: last key wins.
        """
        mapping = self._collected.get(self.map)
        if not mapping:
            return value
        cache = cast("dict[str, Any]", self.context).setdefault(
            "_drf_batch_reverse", {}
        )
        entry = cache.get(self.map)
        if entry is None or entry[0] is not mapping:
            entry = (mapping, {id(item): key for key, item in mapping.items()})
            cache[self.map] = entry
        return entry[1].get(id(value), value)

    def get_attribute(self, instance: Any) -> Any:
        # A read-only cached field whose name is not a model attribute
        # (e.g. a service field) has nothing to read on output.
        try:
            return super().get_attribute(instance)
        except AttributeError:
            return None


class _Malformed(Exception):
    """A row value cannot be coerced to the lookup field's type."""


def _coerce_str(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    raise _Malformed


def _coerce_int(value: Any) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            raise _Malformed from None
    raise _Malformed


def _coerce_float(value: Any) -> float:
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            raise _Malformed from None
    raise _Malformed


def _coerce_decimal(value: Any) -> Any:
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            return decimal.Decimal(value)
        except decimal.InvalidOperation:
            raise _Malformed from None
    raise _Malformed


def _coerce_uuid(value: Any) -> UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return uuid.UUID(value)
        except ValueError:
            raise _Malformed from None
    raise _Malformed


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    raise _Malformed


# Ordered (field types, coercer, expected-type name for the error message).
# Subclasses match their base (SlugField/EmailField are CharFields; every
# integer and auto field is an IntegerField).
_COERCERS: tuple[
    tuple[tuple[type[models.Field], ...], Callable[[Any], Any], str], ...
] = (
    ((models.BooleanField,), _coerce_bool, "boolean"),
    ((models.UUIDField,), _coerce_uuid, "UUID"),
    ((models.CharField, models.TextField), _coerce_str, "string"),
    ((models.IntegerField,), _coerce_int, "number"),
    ((models.FloatField,), _coerce_float, "number"),
    ((models.DecimalField,), _coerce_decimal, "number"),
)


def _in_backend_range(value: int, lookup: models.Field) -> bool:
    """Whether the database can even hold ``value`` in ``lookup``'s column.

    An out-of-range key cannot match a row, and some backends raise on it
    (SQLite: ``OverflowError``) instead of returning nothing.
    """
    low, high = connection.ops.integer_field_range(lookup.get_internal_type())
    return (low is None or value >= low) and (high is None or value <= high)


def _coerce_lookup_key(value: Any, lookup: models.Field) -> tuple[Any, str | None]:
    """Coerce ``value`` for the ``lookup`` field.

    Returns ``(key, None)`` on success or ``(None, expected)`` when the
    value is malformed; ``expected`` is the type name for the error message.
    ``bool`` is an ``int`` subclass and is only valid for boolean lookups.
    Date/DateTime/Time and unknown field types pass through uncoerced
    (documented limitation).
    """
    for types, coerce, expected in _COERCERS:
        if isinstance(lookup, types):
            if isinstance(value, bool) and coerce is not _coerce_bool:
                return None, expected
            try:
                key = coerce(value)
            except _Malformed:
                return None, expected
            if coerce is _coerce_int and not _in_backend_range(key, lookup):
                return None, "number in range"
            return key, None
    if isinstance(value, bool):
        return None, "the lookup's type"
    return value, None


class CachedRelatedField(CachedField):
    """A relation field auto-collected from the database.

    The model is auto-derived from the ForeignKey (or OneToOneField) on
    ``Meta.model`` named by the field's ``source`` (the field name by
    default; DRF reads the input row key from the field name and places the
    validated value under the source); pass ``model=`` explicitly for
    non-FK lookups. Row values are coerced to the lookup field's type
    (``lookup=`` defaults to ``pk``); a malformed value is a row error, not
    a 500.

    Like DRF's ``RelatedField``, the candidate rows can be scoped: pass
    ``queryset=`` or override :meth:`get_queryset` (``self.context`` holds the
    request) so a client cannot reference rows it may not see. The default is
    every row of the model's default manager.

    :param map: key into the collected maps; defaults to the field name.
    :param queryset: rows the lookup may resolve to; its model is the lookup
        model.
    :param model: lookup model; defaults to ``queryset.model`` or the related
        model of the same-named FK on ``Meta.model``.
    :param lookup: field on the lookup model to match against; defaults to
        ``"pk"``.
    :param error: miss-message template (``{value}`` = coerced value).
    :param depends_on: cached fields that must be fetched first.
    """

    def __init__(
        self,
        map: str | None = None,
        *,
        queryset: Any = None,
        model: Any = None,
        lookup: str | None = None,
        error: str | None = None,
        depends_on: Any = (),
        **kwargs: Any,
    ) -> None:
        self.queryset = queryset
        if model is None and queryset is not None:
            model = queryset.model
        self._model = model
        self.lookup = lookup or "pk"
        self._lookup_field: models.Field | None = None
        super().__init__(map=map, error=error, depends_on=depends_on, **kwargs)

    def _resolve_model(self) -> Any:
        if self._model is not None:
            return self._model
        parent = self.parent
        model = (
            getattr(getattr(parent, "Meta", None), "model", None)
            if parent is not None
            else None
        )
        if model is None:
            raise ImproperlyConfigured(
                f"CachedRelatedField '{self.field_name}': the parent "
                "serializer has no Meta.model; pass model= explicitly."
            )
        attribute = _source_attribute(self)
        try:
            model_field = model._meta.get_field(attribute)
        except FieldDoesNotExist:
            model_field = None
        if isinstance(model_field, (models.ForeignKey, models.OneToOneField)):
            self._model = model_field.related_model
            return self._model
        raise ImproperlyConfigured(
            f"CachedRelatedField '{self.field_name}': "
            f"{model.__name__}.{attribute} is not a ForeignKey or "
            "OneToOneField; pass model= explicitly for non-FK lookups."
        )

    def _resolve_lookup_field(self) -> models.Field:
        if self._lookup_field is None:
            model = self._resolve_model()
            if self.lookup == "pk":
                self._lookup_field = model._meta.pk
            else:
                try:
                    self._lookup_field = model._meta.get_field(self.lookup)
                except FieldDoesNotExist:
                    raise ImproperlyConfigured(
                        f"CachedRelatedField '{self.field_name}': "
                        f"{model.__name__} has no field '{self.lookup}'."
                    ) from None
        return self._lookup_field

    def prepare_key(self, value: Any) -> Any:
        if value is None:
            return NO_KEY
        key, expected = _coerce_lookup_key(value, self._resolve_lookup_field())
        if expected is not None:
            self.fail(
                "invalid_key",
                field=self.field_name,
                expected=expected,
                actual=type(value).__name__,
            )
        return key

    def get_queryset(self) -> Any:
        """The rows this field may resolve to (re-evaluated per request)."""
        if self.queryset is not None:
            return self.queryset.all()
        return default_manager(self._resolve_model()).all()

    def fetch(
        self, keys: set[Any], collected: dict[str, Any], context: BatchContext
    ) -> dict[Any, Any]:
        queryset = self.get_queryset()
        if self.lookup == "pk":
            return cast("dict[Any, Any]", queryset.in_bulk(keys))
        return cast("dict[Any, Any]", queryset.in_bulk(keys, field_name=self.lookup))

    def to_representation(self, value: Any) -> Any:
        """The lookup value of the related instance (e.g. its ``isin``).

        Deterministic and request-independent, so the serializer also works
        on the read path (``list`` / ``retrieve``).
        """
        if isinstance(value, models.Model):
            return getattr(value, self._resolve_lookup_field().attname)
        return value


class CachedServiceField(CachedField):
    """A cached field whose map comes from a user ``fetch`` callable.

    :param map: key into the collected maps; defaults to the field name.
    :param fetch: a sync or ``async def`` callable
        ``(keys, collected, context) -> dict``, or ``None`` when a subclass
        overrides :meth:`fetch`.
    :param error: miss-message template (``{value}`` = coerced value).
    :param depends_on: cached fields that must be fetched first.

    ``prepare_key`` is the identity here (``None`` -> ``NO_KEY``); override
    it in a subclass to coerce service keys.

    The fetch is invoked **even when no row supplied a key for this field**
    (unlike a database lookup, which is skipped for an empty key set), since
    its map may be derived from the other collected maps; return ``{}`` when
    nothing applies.
    """

    def __init__(
        self,
        map: str | None = None,
        *,
        fetch: Any = None,
        error: str | None = None,
        depends_on: Any = (),
        **kwargs: Any,
    ) -> None:
        if fetch is None and type(self).fetch is CachedServiceField.fetch:
            raise TypeError(
                "CachedServiceField: pass a fetch callable "
                "(or override fetch() in a subclass)."
            )
        if fetch is not None and not callable(fetch):
            raise TypeError(
                f"CachedServiceField: fetch must be callable, "
                f"got {type(fetch).__name__}."
            )
        self._fetch = fetch
        super().__init__(map=map, error=error, depends_on=depends_on, **kwargs)

    def fetch(
        self, keys: set[Any], collected: dict[str, Any], context: BatchContext
    ) -> Any:
        assert self._fetch is not None
        return self._fetch(keys, collected, context)

    @property
    def skip_fetch_on_empty_keys(self) -> bool:
        return False

    @property
    def is_async(self) -> bool:
        return self._fetch is not None and inspect.iscoroutinefunction(self._fetch)
