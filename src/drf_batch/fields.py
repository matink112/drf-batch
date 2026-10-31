"""Collection fields: resolve a row value against the request's collected maps.

Collection is **serializer-driven** (v0.3): a ``BatchSerializer`` declares
which of its fields are *batch fields*, and the list serializer auto-collects the
needed lookups before any row is validated — the common case needs no
collector class at all.

- :class:`BatchField` is the base: it owns the map name, the miss/error
  templates, ``prepare_key`` (row value -> lookup key) and the ``collected``
  lookup; subclasses provide :meth:`BatchField.fetch`.
- :class:`BatchRelatedField` auto-derives its model from the same-named
  ForeignKey on ``Meta.model`` (explicit ``model=`` for non-FK lookups) and
  coerces row values to the lookup field's type.
- :class:`BatchServiceField` fetches the map with a user callable
  (sync or ``async def``).

Where a row's key comes from (see :meth:`BatchField.stored_key_path` and
:meth:`BatchField.derived_key`): a writable field the row sends uses the
row's value; ``read_only`` fields never read the row. Otherwise the key is
**derived** from other fields (``derived_from=``) or, on update, read from
the **stored** row. Only a key the row sent can be a "not found" error.

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


def _model_field(model: type[models.Model], attribute: str | None) -> Any:
    """``model``'s field named ``attribute``, or ``None``."""
    try:
        return model._meta.get_field(attribute or "")
    except FieldDoesNotExist:
        return None


class BatchField(serializers.Field):
    """Base class for fields resolved against the request's collected maps.

    :param map: key into the request's ``collected`` maps; defaults to the
        field name.
    :param error: optional template for the miss message; ``{value}`` is
        replaced with the offending (coerced) value. Defaults to
        ``"Object '{value}' not found."``.
    :param depends_on: names of other batch fields whose fetches must
        finish first. Fetches without a dependency between them run
        concurrently; every fetch receives a snapshot of the maps collected
        so far, which always includes its ``depends_on`` maps.
    :param derived_from: a field name or a tuple of names the key is built
        from when the row does not send this field (always, for a
        ``read_only`` field). Each name is a serializer field (the row's
        value, else the stored value on update) or a model field (the stored
        value); a batch field gives its own key for the row.
    :param key: builds the key from the ``derived_from`` values (passed
        positionally, ``None`` included); return ``None`` for no key. The
        default is the value for one name, a tuple for several, and no key
        when any value is ``None``.
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
        derived_from: str | tuple[str, ...] | None = None,
        key: Callable[..., Any] | None = None,
        **kwargs: Any,
    ) -> None:
        if isinstance(derived_from, str):
            derived_from = (derived_from,)
        derived_from = tuple(derived_from or ())
        if key is not None and not callable(key):
            raise TypeError(
                f"{type(self).__name__}: key must be callable, "
                f"got {type(key).__name__}."
            )
        if key is not None and not derived_from:
            raise TypeError(
                f"{type(self).__name__}: key= builds the key from the "
                "derived_from values; pass derived_from= too."
            )
        self._map = map
        self.error = error
        self.depends_on = tuple(depends_on)
        self.derived_from: tuple[str, ...] = derived_from
        self._key = key
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
        collected maps, so :class:`BatchServiceField` keeps it running.
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

    def implied_key(self, value: Any) -> Any:
        """Coerce a key the row did not send (stored or derived).

        :meth:`prepare_key`, but a value that cannot be coerced gives no key
        (:data:`NO_KEY`) instead of a row error: the client never sent it.
        """
        try:
            return self.prepare_key(value)
        except ValidationError:
            return NO_KEY

    def derived_key(self, values: tuple[Any, ...]) -> Any:
        """The key built from the ``derived_from`` values (:data:`NO_KEY` if none)."""
        if self._key is not None:
            return self.implied_key(self._key(*values))
        if any(value is None for value in values):
            return NO_KEY
        return self.implied_key(values[0] if len(values) == 1 else values)

    def stored_key_path(
        self, model: type[models.Model]
    ) -> tuple[tuple[str, ...], str | None] | None:
        """Where an update target stores this field's key.

        ``(attributes, select_related)``: the attribute chain to read on the
        target, and the relation the target query must join for it (or
        ``None``). ``None`` when the field's source is not a concrete field
        of ``model``. A ForeignKey/OneToOneField reads its column
        (``ticker_id``).
        """
        model_field = _model_field(model, _source_attribute(self))
        if model_field is None or not model_field.concrete:
            return None
        return (model_field.attname,), None

    def stored_key(self, instance: Any, path: tuple[str, ...]) -> Any:
        """The key stored on ``instance`` at ``path`` (:data:`NO_KEY` if none)."""
        value = instance
        for attribute in path:
            value = getattr(value, attribute, None)
            if value is None:
                return NO_KEY
        return self.implied_key(value)

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
        # A read-only batch field whose name is not a model attribute
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


class BatchRelatedField(BatchField):
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
    :param depends_on: batch fields that must be fetched first.
    :param derived_from: see :class:`BatchField` (also ``key``).

    On update, a row that omits a writable related field contributes the
    target's stored key only when another batch field reads this one
    (``derived_from`` or ``depends_on``); nothing else needs it.
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
        derived_from: str | tuple[str, ...] | None = None,
        key: Callable[..., Any] | None = None,
        **kwargs: Any,
    ) -> None:
        self.queryset = queryset
        if model is None and queryset is not None:
            model = queryset.model
        self._model = model
        self.lookup = lookup or "pk"
        self._lookup_field: models.Field | None = None
        super().__init__(
            map=map,
            error=error,
            depends_on=depends_on,
            derived_from=derived_from,
            key=key,
            **kwargs,
        )

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
                f"BatchRelatedField '{self.field_name}': the parent "
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
            f"BatchRelatedField '{self.field_name}': "
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
                        f"BatchRelatedField '{self.field_name}': "
                        f"{model.__name__} has no field '{self.lookup}'."
                    ) from None
        return self._lookup_field

    def stored_key_path(
        self, model: type[models.Model]
    ) -> tuple[tuple[str, ...], str | None] | None:
        """The stored related object's lookup value.

        A lookup on the relation's target field (the pk by default) reads
        the FK column (``ticker_id``, no join); another lookup reads it
        across the relation (``ticker.isin``), joined in the target query.
        """
        model_field = _model_field(model, _source_attribute(self))
        if not isinstance(model_field, (models.ForeignKey, models.OneToOneField)):
            return super().stored_key_path(model)
        lookup_field = self._resolve_lookup_field()
        if lookup_field == model_field.target_field:
            return (model_field.attname,), None
        return (model_field.name, lookup_field.attname), model_field.name

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


class BatchServiceField(BatchField):
    """A batch field whose map comes from a user ``fetch`` callable.

    :param map: key into the collected maps; defaults to the field name.
    :param fetch: a sync or ``async def`` callable
        ``(keys, collected, context) -> dict``, or ``None`` when a subclass
        overrides :meth:`fetch`.
    :param error: miss-message template (``{value}`` = coerced value).
    :param depends_on: batch fields that must be fetched first.
    :param derived_from: see :class:`BatchField` (also ``key``).

    On update, a row that omits the field contributes the target's stored
    value of the field's model attribute (column, or FK column), so the
    service sees a key for every row without any opt-in.

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
        derived_from: str | tuple[str, ...] | None = None,
        key: Callable[..., Any] | None = None,
        **kwargs: Any,
    ) -> None:
        if fetch is None and type(self).fetch is BatchServiceField.fetch:
            raise TypeError(
                "BatchServiceField: pass a fetch callable "
                "(or override fetch() in a subclass)."
            )
        if fetch is not None and not callable(fetch):
            raise TypeError(
                f"BatchServiceField: fetch must be callable, "
                f"got {type(fetch).__name__}."
            )
        self._fetch = fetch
        super().__init__(
            map=map,
            error=error,
            depends_on=depends_on,
            derived_from=derived_from,
            key=key,
            **kwargs,
        )

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
