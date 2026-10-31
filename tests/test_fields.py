from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any, cast

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db import models
from rest_framework.exceptions import ValidationError

from drf_batch import BatchRelatedField, BatchSerializer, BatchServiceField
from drf_batch.fields import NO_KEY
from tests.apps.demo.models import Customer, Order, Symbol, Ticker


def make_collected(ticker: Ticker | None = None, customer: Customer | None = None):
    return {
        "ticker": {"1101": ticker} if ticker is not None else {},
        "customer": {"112001": customer} if customer is not None else {},
    }


def make_row(**overrides):
    row = {
        "ticker": "1101",
        "customer": "112001",
        "volume": 5,
        "reference_id": "R1",
    }
    row.update(overrides)
    return row


def test_hit_returns_the_model_instance(tickers, customers):
    ticker, customer = tickers[0], customers[0]
    from tests.apps.demo.serializers import OrderSerializer

    s = OrderSerializer(context={"collected": make_collected(ticker, customer)})

    validated = s.run_validation(make_row())

    assert validated["ticker"] is ticker
    assert validated["customer"] is customer
    assert validated["volume"] == 5


def test_miss_uses_the_custom_error_template_with_value(tickers, customers):
    ticker, customer = tickers[0], customers[0]
    from tests.apps.demo.serializers import OrderSerializer

    s = OrderSerializer(context={"collected": make_collected(ticker, customer)})

    with pytest.raises(ValidationError) as exc_info:
        s.run_validation(make_row(ticker="MISSING"))

    assert exc_info.value.detail == {"ticker": ["Ticker 'MISSING' not found."]}


def test_missing_map_key_in_collected_is_a_miss_not_a_keyerror(tickers, customers):
    from tests.apps.demo.serializers import OrderSerializer

    s = OrderSerializer(context={"collected": {}})

    with pytest.raises(ValidationError) as exc_info:
        s.run_validation(make_row())

    assert exc_info.value.detail == {
        "ticker": ["Ticker '1101' not found."],
        "customer": ["Customer '112001' not found."],
    }


def test_default_error_template_when_no_error_kwarg():
    class ThingSerializer(BatchSerializer):
        thing = BatchRelatedField(model=Ticker, lookup="isin")

        class Meta:
            model = Order
            fields = ("thing",)

    s = ThingSerializer(context={})

    with pytest.raises(ValidationError) as exc_info:
        s.run_validation({"thing": "XYZ"})

    assert exc_info.value.detail == {"thing": ["Object 'XYZ' not found."]}


def test_to_representation_returns_the_lookup_key(tickers, customers):
    from tests.apps.demo.serializers import OrderSerializer

    ticker, customer = tickers[0], customers[0]
    s = OrderSerializer(
        context={"collected": make_collected(ticker, customer)}, instance=None
    )
    order = Order(ticker=ticker, customer=customer, volume=5, reference_id="R1")

    data = s.to_representation(order)

    assert data["ticker"] == "1101"
    assert data["customer"] == "112001"


# ---------------------------------------------------------------------------
# prepare_key coercion matrix
# ---------------------------------------------------------------------------


class _Probe(models.Model):
    """Model exposing every lookup type for the coercion matrix (no DB needed)."""

    text = models.CharField(max_length=16)
    slug = models.SlugField()
    number = models.IntegerField()
    flag = models.BooleanField()
    ratio = models.FloatField()
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    stamp = models.DateTimeField()

    class Meta:
        app_label = "demo"


class ProbeSerializer(BatchSerializer):
    by_text = BatchRelatedField(model=_Probe, lookup="text")
    by_slug = BatchRelatedField(model=_Probe, lookup="slug")
    by_number = BatchRelatedField(model=_Probe, lookup="number")
    by_flag = BatchRelatedField(model=_Probe, lookup="flag")
    by_ratio = BatchRelatedField(model=_Probe, lookup="ratio")
    by_amount = BatchRelatedField(model=_Probe, lookup="amount")
    by_stamp = BatchRelatedField(model=_Probe, lookup="stamp")
    by_pk = BatchRelatedField(model=Ticker, lookup="pk")
    by_symbol_pk = BatchRelatedField(model=Symbol, lookup="pk")

    class Meta:
        model = Order
        fields = (
            "by_text",
            "by_slug",
            "by_number",
            "by_flag",
            "by_ratio",
            "by_amount",
            "by_stamp",
            "by_pk",
            "by_symbol_pk",
        )


@pytest.fixture
def probes() -> ProbeSerializer:
    return ProbeSerializer()


def _expect_invalid(probes, name: str, value: Any, expected: str) -> None:
    field = probes.fields[name]
    with pytest.raises(ValidationError) as exc_info:
        field.prepare_key(value)
    actual = type(value).__name__
    detail = exc_info.value.detail
    messages = [
        str(m) for m in (detail if isinstance(detail, (list, tuple)) else [detail])
    ]
    assert messages == [
        f"Invalid value for '{name}': expected a {expected}, got {actual}."
    ]


def test_char_lookup_coercion(probes):
    f = probes.fields["by_text"]
    assert f.prepare_key("1101") == "1101"
    assert f.prepare_key(1101) == "1101"
    assert f.prepare_key(1101.0) == "1101"
    assert f.prepare_key(None) is NO_KEY
    _expect_invalid(probes, "by_text", 1101.5, "string")
    _expect_invalid(probes, "by_text", True, "string")
    _expect_invalid(probes, "by_text", ["1101"], "string")
    _expect_invalid(probes, "by_text", {"k": 1}, "string")


def test_slug_lookup_rejects_bool(probes):
    _expect_invalid(probes, "by_slug", False, "string")


def test_int_lookup_coercion(probes):
    f = probes.fields["by_number"]
    assert f.prepare_key(1101) == 1101
    assert f.prepare_key("1101") == 1101
    assert f.prepare_key(1101.0) == 1101
    assert f.prepare_key(None) is NO_KEY
    _expect_invalid(probes, "by_number", "1101.5", "number")
    _expect_invalid(probes, "by_number", 1101.5, "number")
    _expect_invalid(probes, "by_number", True, "number")
    _expect_invalid(probes, "by_number", ["1101"], "number")


def test_auto_pk_lookup_is_int_lookup(probes):
    f = probes.fields["by_pk"]
    assert f.prepare_key(7) == 7
    assert f.prepare_key("7") == 7
    _expect_invalid(probes, "by_pk", "seven", "number")


def test_uuid_lookup_coercion(probes):
    f = probes.fields["by_symbol_pk"]
    u = uuid.uuid4()
    assert f.prepare_key(u) is u
    parsed = f.prepare_key(str(u))
    assert parsed == u and isinstance(parsed, uuid.UUID)
    assert f.prepare_key(None) is NO_KEY
    _expect_invalid(probes, "by_symbol_pk", "not-a-uuid", "UUID")
    _expect_invalid(probes, "by_symbol_pk", 42, "UUID")


def test_bool_lookup_coercion(probes):
    f = probes.fields["by_flag"]
    assert f.prepare_key(True) is True
    assert f.prepare_key(False) is False
    _expect_invalid(probes, "by_flag", 1, "boolean")
    _expect_invalid(probes, "by_flag", "true", "boolean")


def test_float_and_decimal_lookup_coercion(probes):
    ratio = probes.fields["by_ratio"]
    assert ratio.prepare_key(1) == 1
    assert ratio.prepare_key(1.5) == 1.5
    assert ratio.prepare_key("1.5") == 1.5
    _expect_invalid(probes, "by_ratio", "x", "number")

    amount = probes.fields["by_amount"]
    assert amount.prepare_key(2) == 2
    assert amount.prepare_key(2.5) == 2.5
    assert amount.prepare_key("2.50") == Decimal("2.50")
    _expect_invalid(probes, "by_amount", "x", "number")


def test_datetime_lookup_passes_through(probes):
    import datetime

    f = probes.fields["by_stamp"]
    stamp = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    assert f.prepare_key(stamp) is stamp
    assert f.prepare_key("2026-01-01T00:00:00Z") == "2026-01-01T00:00:00Z"


def test_prepare_key_is_idempotent(probes):
    for name, value in [
        ("by_text", 1101),
        ("by_number", "1101"),
        ("by_symbol_pk", "11111111-1111-4111-8111-111111111111"),
        ("by_ratio", "1.5"),
    ]:
        f = probes.fields[name]
        first = f.prepare_key(value)
        assert f.prepare_key(first) == first


# ---------------------------------------------------------------------------
# to_internal_value: coercion feeds the map lookup
# ---------------------------------------------------------------------------


def test_int_row_value_hits_char_map_via_coercion(tickers, customers):
    from tests.apps.demo.serializers import OrderSerializer

    s = OrderSerializer(context={"collected": make_collected(tickers[0], customers[0])})

    validated = s.run_validation(make_row(ticker=1101))

    assert validated["ticker"] is tickers[0]


def test_miss_uses_the_coerced_value_in_the_message(tickers, customers):
    from tests.apps.demo.serializers import OrderSerializer

    s = OrderSerializer(context={"collected": make_collected(None, customers[0])})

    with pytest.raises(ValidationError) as exc_info:
        s.run_validation(make_row(ticker="1103"))

    assert exc_info.value.detail == {"ticker": ["Ticker '1103' not found."]}


def test_uuid_row_value_hits_uuid_map(tickers, customers, symbols):
    from tests.apps.demo.serializers import SymbolOrderSerializer

    s = SymbolOrderSerializer(
        context={"collected": {"symbol": {symbols[0].pk: symbols[0]}}}
    )

    validated = s.run_validation(
        {"symbol": str(symbols[0].pk), "volume": 1, "reference_id": "S"}
    )

    assert validated["symbol"] is symbols[0]


# ---------------------------------------------------------------------------
# map name resolution / get_attribute / construction
# ---------------------------------------------------------------------------


def test_map_defaults_to_field_name_after_bind(probes):
    assert probes.fields["by_text"].map == "by_text"


def test_explicit_map_wins():
    class S(BatchSerializer):
        thing = BatchRelatedField(model=Ticker, lookup="isin", map="tickers")

        class Meta:
            model = Order
            fields = ("thing",)

    s = S()
    assert cast("BatchRelatedField", s.fields["thing"]).map == "tickers"


def test_get_attribute_attribute_error_returns_none():
    # A writable (required) batch field whose name is not a model
    # attribute: get_attribute swallows the AttributeError on output.
    class S(BatchSerializer):
        band = BatchServiceField(fetch=lambda keys, collected, context: {})

        class Meta:
            model = Order
            fields = ("band",)

    s = S()
    assert s.fields["band"].get_attribute(Order()) is None


def test_batch_related_field_resolves_model_from_same_named_fk(probes):
    from tests.apps.demo.serializers import OrderSerializer

    s = OrderSerializer()
    assert cast("BatchRelatedField", s.fields["ticker"])._resolve_model() is Ticker
    assert cast("BatchRelatedField", s.fields["customer"])._resolve_model() is Customer


def test_batch_related_field_non_fk_without_model_fails_fast():
    from drf_batch import BatchListSerializer

    class S(BatchSerializer):
        volume = BatchRelatedField(lookup="number", model=_Probe)
        # read-only: keeps the bulk-field check out of the way so the
        # non-FK model resolution is what fails.
        reference = BatchRelatedField(read_only=True)

        class Meta:
            model = Order
            fields = ("volume", "reference")

    with pytest.raises(
        ImproperlyConfigured, match="is not a ForeignKey or OneToOneField"
    ):
        BatchListSerializer(child=S(context={}), data=[])


def test_batch_related_field_unknown_lookup_fails_fast():
    from drf_batch import BatchListSerializer

    class S(BatchSerializer):
        ticker = BatchRelatedField(lookup="nope")

        class Meta:
            model = Order
            fields = ("ticker",)

    with pytest.raises(ImproperlyConfigured, match="has no field 'nope'"):
        BatchListSerializer(child=S(context={}), data=[])


def test_batch_service_field_requires_callable_fetch():
    with pytest.raises(TypeError, match="must be callable"):
        BatchServiceField(fetch="nope")

    with pytest.raises(TypeError, match="pass a fetch callable"):
        BatchServiceField()


def test_batch_field_fetch_is_abstract():
    from drf_batch.fields import BatchField

    class S(BatchSerializer):
        thing = BatchRelatedField(model=Ticker, lookup="isin")

        class Meta:
            model = Order
            fields = ("thing",)

    field = cast("BatchRelatedField", S().fields["thing"])
    with pytest.raises(NotImplementedError):
        BatchField.fetch(field, set(), {}, {})  # base abstract method
