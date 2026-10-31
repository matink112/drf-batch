"""Where a batch field's key comes from: the row, the stored row, derivation.

- a writable field the row sends: the row's value (the only key that can be
  a "not found" error);
- ``read_only``: the row is never read;
- otherwise ``derived_from`` builds the key, or, on update, the target's
  stored value.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.test import APIClient

from drf_batch import (
    BatchCollector,
    BatchListSerializer,
    BatchNotFound,
    BatchPermissionDenied,
    BatchRelatedField,
    BatchSerializer,
    BatchServiceField,
)
from tests.apps.demo.models import Customer, Order, Ticker
from tests.apps.demo.serializers import OrderSerializer

SERVICE_BATCH_URL = "/service-orders/batch/"


def spy_fetch(calls: list[set[Any]], result: dict[Any, Any] | None = None) -> Any:
    def fetch(keys: set[Any], collected: dict[str, Any], context: Any) -> Any:
        calls.append(set(keys))
        return {key: f"svc:{key}" for key in keys} if result is None else result

    return fetch


def run(
    child_class: type[BatchSerializer], rows: list[Any], **kwargs: Any
) -> tuple[BatchListSerializer, dict[str, Any]]:
    """Build the list serializer and run its validation once."""
    context: dict[str, Any] = {}
    serializer = BatchListSerializer(
        child=child_class(context=context), data=rows, context=context, **kwargs
    )
    serializer.is_valid()
    return serializer, context


def ticker_queries(queries: CaptureQueriesContext) -> list[str]:
    """The queries that read the ticker table directly (not via a join)."""
    return [q["sql"] for q in queries if 'FROM "demo_ticker"' in q["sql"]]


@pytest.fixture
def orders(tickers, customers) -> list[Order]:
    return [
        Order.objects.create(
            ticker=tickers[0], customer=customers[0], volume=10, reference_id="U1"
        ),
        Order.objects.create(
            ticker=tickers[1], customer=customers[0], volume=20, reference_id="U2"
        ),
    ]


# ---------------------------------------------------------------------------
# read_only: the row is ignored
# ---------------------------------------------------------------------------


def test_read_only_field_never_reads_the_row(tickers, customers, orders):
    Customer.objects.create(isin="112002", name="Other")
    calls: list[set[Any]] = []

    class S(OrderSerializer):
        flag = BatchServiceField(fetch=spy_fetch(calls), read_only=True)

        class Meta(OrderSerializer.Meta):
            fields = (*OrderSerializer.Meta.fields, "flag")  # type: ignore[assignment]

    create_row = {
        "ticker": "1101",
        "customer": "112002",
        "volume": 1,
        "reference_id": "N",
        "flag": {"malformed": True},
    }
    serializer, _ = run(S, [create_row])
    assert serializer.is_valid(), serializer.errors

    serializer, _ = run(S, [{"id": orders[0].pk, "flag": "X"}], operation="update")
    assert serializer.is_valid(), serializer.errors
    # No row value, no derivation, no stored "flag" column: no keys; the
    # service still runs.
    assert calls == [set(), set()]


def test_read_only_related_field_malformed_row_value_is_not_an_error(
    tickers, customers
):
    class S(OrderSerializer):
        alt = BatchRelatedField(
            model=Ticker, lookup="isin", read_only=True, derived_from="reference_id"
        )

        class Meta(OrderSerializer.Meta):
            fields = (*OrderSerializer.Meta.fields, "alt")  # type: ignore[assignment]

    row = {
        "ticker": "1101",
        "customer": "112001",
        "volume": 1,
        "reference_id": "1102",
        "alt": ["not", "a", "key"],
    }
    serializer, context = run(S, [row])

    assert serializer.is_valid(), serializer.errors
    assert set(context["collected"]["alt"]) == {"1102"}


# ---------------------------------------------------------------------------
# stored keys (update)
# ---------------------------------------------------------------------------


def test_writable_service_field_omitted_on_patch_uses_the_stored_column(orders):
    calls: list[set[Any]] = []

    class S(OrderSerializer):
        reference_id = BatchServiceField(fetch=spy_fetch(calls))  # type: ignore[assignment]

    serializer, _ = run(
        S,
        [
            {"id": orders[0].pk, "volume": 5},
            {"id": orders[1].pk, "reference_id": "NEW"},
        ],
        operation="update",
    )

    assert serializer.is_valid(), serializer.errors
    assert calls == [{"U1", "NEW"}]
    # validated_data holds only what the row sent.
    assert "reference_id" not in serializer.validated_data[0]
    assert serializer.validated_data[1]["reference_id"] == "svc:NEW"


def test_writable_service_field_on_a_foreign_key_uses_the_fk_column(orders, customers):
    calls: list[set[Any]] = []

    class S(BatchSerializer):
        customer = BatchServiceField(fetch=spy_fetch(calls), required=False)
        volume = serializers.IntegerField(min_value=1)

        class Meta:
            model = Order
            fields = ("id", "customer", "volume")

    serializer, _ = run(S, [{"id": orders[0].pk, "volume": 5}], operation="update")

    assert serializer.is_valid(), serializer.errors
    assert calls == [{customers[0].pk}]


def test_writable_service_field_omitted_on_create_has_no_key(tickers, customers):
    calls: list[set[Any]] = []

    class S(OrderSerializer):
        reference_id = BatchServiceField(fetch=spy_fetch(calls), required=False)  # type: ignore[assignment]

    row = {"ticker": "1101", "customer": "112001", "volume": 1}
    serializer, _ = run(S, [row])

    assert serializer.is_valid(), serializer.errors
    assert calls == [set()]  # the fetch still runs


def test_put_omitted_optional_field_uses_the_stored_column(orders):
    calls: list[set[Any]] = []

    class S(OrderSerializer):
        reference_id = BatchServiceField(fetch=spy_fetch(calls), required=False)  # type: ignore[assignment]

    row = {"id": orders[0].pk, "ticker": "1101", "customer": "112001", "volume": 3}
    serializer, _ = run(S, [row], operation="update", partial=False)

    assert serializer.is_valid(), serializer.errors
    assert calls == [{"U1"}]


def test_sent_null_wins_over_the_stored_value(orders):
    calls: list[set[Any]] = []

    class S(OrderSerializer):
        reference_id = BatchServiceField(fetch=spy_fetch(calls), allow_null=True)  # type: ignore[assignment]

    serializer, _ = run(
        S, [{"id": orders[0].pk, "reference_id": None}], operation="update"
    )

    assert serializer.is_valid(), serializer.errors
    assert calls == [set()]


def test_related_field_omitted_without_a_reader_adds_no_query(orders):
    with CaptureQueriesContext(connection) as queries:
        serializer, context = run(
            OrderSerializer,
            [{"id": o.pk, "volume": 5} for o in orders],
            operation="update",
        )

    assert serializer.is_valid(), serializer.errors
    assert "JOIN" not in queries[0]["sql"]  # the targets
    assert ticker_queries(queries) == []
    assert context["collected"]["ticker"] == {}


def test_related_field_read_by_another_gets_stored_keys_in_one_join(orders, tickers):
    class S(OrderSerializer):
        name = BatchServiceField(
            fetch=lambda keys, collected, context: {
                isin: collected["ticker"][isin].name for isin in keys
            },
            derived_from="ticker",
            depends_on=("ticker",),
            read_only=True,
        )

        class Meta(OrderSerializer.Meta):
            fields = (*OrderSerializer.Meta.fields, "name")  # type: ignore[assignment]

    with CaptureQueriesContext(connection) as queries:
        serializer, context = run(
            S, [{"id": o.pk, "volume": 5} for o in orders], operation="update"
        )

    assert serializer.is_valid(), serializer.errors
    # The targets join ticker for the isin; one ticker lookup: no N+1.
    assert '"demo_ticker"' in queries[0]["sql"]
    assert "JOIN" in queries[0]["sql"]
    assert len(ticker_queries(queries)) == 1
    assert context["collected"]["ticker"] == {"1101": tickers[0], "1102": tickers[1]}
    assert context["collected"]["name"] == {"1101": "Alpha Corp", "1102": "Beta Corp"}


def test_pk_lookup_stored_key_reads_the_fk_column_without_a_join(orders, tickers):
    class S(BatchSerializer):
        ticker = BatchRelatedField(required=False)
        echo = BatchServiceField(
            fetch=lambda keys, collected, context: dict.fromkeys(keys, True),
            derived_from="ticker",
            read_only=True,
        )
        volume = serializers.IntegerField(min_value=1)

        class Meta:
            model = Order
            fields = ("id", "ticker", "echo", "volume")

    with CaptureQueriesContext(connection) as queries:
        serializer, context = run(
            S, [{"id": o.pk, "volume": 5} for o in orders], operation="update"
        )

    assert serializer.is_valid(), serializer.errors
    assert "JOIN" not in queries[0]["sql"]
    assert context["collected"]["echo"] == {tickers[0].pk: True, tickers[1].pk: True}


# ---------------------------------------------------------------------------
# derived_from
# ---------------------------------------------------------------------------


class DerivedSerializer(OrderSerializer):
    """``tag`` is derived from (ticker, volume); ``grade`` from ``tag``."""

    tag = BatchServiceField(
        fetch=lambda keys, collected, context: {key: key for key in keys},
        derived_from=("ticker", "volume"),
        read_only=True,
    )
    grade = BatchServiceField(
        fetch=lambda keys, collected, context: {key: "big" for key in keys},
        derived_from="tag",
        key=lambda tag: (tag is not None and tag[1] >= 100) or None,
        read_only=True,
    )

    class Meta(OrderSerializer.Meta):
        fields = (*OrderSerializer.Meta.fields, "tag", "grade")  # type: ignore[assignment]

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        context = cast("dict[str, Any]", self.context)
        context.setdefault("seen", []).append(
            (self.fetched("tag"), self.fetched("grade"))
        )
        return attrs


def test_derived_key_from_the_row_on_create(tickers, customers):
    row = {"ticker": "1101", "customer": "112001", "volume": 150, "reference_id": "A"}
    serializer, context = run(DerivedSerializer, [row])

    assert serializer.is_valid(), serializer.errors
    assert context["seen"] == [(("1101", 150), "big")]


def test_derived_key_mixes_row_and_stored_values_on_patch(orders):
    Ticker.objects.create(isin="1103", name="Gamma Corp")
    serializer, context = run(
        DerivedSerializer,
        [
            {"id": orders[0].pk, "volume": 500},  # ticker stored, volume sent
            {"id": orders[1].pk, "ticker": "1103"},  # ticker sent, volume stored
        ],
        operation="update",
    )

    assert serializer.is_valid(), serializer.errors
    assert context["seen"] == [(("1101", 500), "big"), (("1103", 20), None)]
    assert set(context["collected"]["tag"]) == {("1101", 500), ("1103", 20)}


def test_derived_from_none_input_gives_no_key(tickers, customers):
    # "volume" is missing on create: the default builder gives no key.
    row = {"ticker": "1101", "customer": "112001", "reference_id": "A"}
    serializer, context = run(DerivedSerializer, [row])

    assert serializer.is_valid() is False  # volume is required
    assert context["collected"]["tag"] == {}


def test_derived_from_invalid_plain_value_reports_through_validation(
    tickers, customers
):
    row = {
        "ticker": "1101",
        "customer": "112001",
        "volume": "lots",
        "reference_id": "A",
    }
    serializer, context = run(DerivedSerializer, [row])

    assert serializer.is_valid() is False
    assert "volume" in serializer.errors[0]
    assert context["collected"]["tag"] == {}


def test_derived_from_a_model_field_not_on_the_serializer(orders):
    calls: list[set[Any]] = []

    class S(BatchSerializer):
        volume = serializers.IntegerField(min_value=1)
        owner_check = BatchServiceField(
            fetch=spy_fetch(calls), derived_from="reference_id", read_only=True
        )

        class Meta:
            model = Order
            fields = ("id", "volume", "owner_check")

    serializer, _ = run(S, [{"id": orders[1].pk, "volume": 1}], operation="update")

    assert serializer.is_valid(), serializer.errors
    assert calls == [{"U2"}]


def test_writable_field_derived_when_omitted(tickers, customers):
    calls: list[set[Any]] = []

    class S(OrderSerializer):
        reference_id = BatchServiceField(  # type: ignore[assignment]
            fetch=spy_fetch(calls), derived_from="volume", required=False
        )

    rows = [
        {"ticker": "1101", "customer": "112001", "volume": 1, "reference_id": "R"},
        {"ticker": "1102", "customer": "112001", "volume": 7},
    ]
    serializer, _ = run(S, rows)

    assert serializer.is_valid(), serializer.errors
    assert calls == [{"R", 7}]


# ---------------------------------------------------------------------------
# misses, fetched(), collectors
# ---------------------------------------------------------------------------


def test_derived_and_stored_misses_are_not_row_errors(orders):
    class S(OrderSerializer):
        reference_id = BatchServiceField(fetch=spy_fetch([], result={}))  # type: ignore[assignment]
        band = BatchServiceField(
            fetch=spy_fetch([], result={}), derived_from="volume", read_only=True
        )

        class Meta(OrderSerializer.Meta):
            fields = (*OrderSerializer.Meta.fields, "band")  # type: ignore[assignment]

        def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
            if self.fetched("band") is None:
                raise ValidationError({"band": ["no band"]})
            return attrs

    serializer, _ = run(S, [{"id": orders[0].pk, "volume": 3}], operation="update")

    # The stored reference_id miss is silent; validate() rejects the band.
    assert serializer.is_valid() is False
    assert serializer.errors == [{"band": ["no band"]}]


def test_sent_key_miss_is_still_a_row_error(orders):
    class S(OrderSerializer):
        reference_id = BatchServiceField(fetch=spy_fetch([], result={}))  # type: ignore[assignment]

    serializer, _ = run(
        S, [{"id": orders[0].pk, "reference_id": "X"}], operation="update"
    )

    assert serializer.is_valid() is False
    assert serializer.errors == [{"reference_id": ["Object 'X' not found."]}]


def test_fetched_keeps_per_field_keys_on_a_shared_map(tickers, customers):
    class S(OrderSerializer):
        alt = BatchRelatedField(
            model=Ticker,
            lookup="isin",
            map="ticker",
            read_only=True,
            derived_from="reference_id",
        )

        class Meta(OrderSerializer.Meta):
            fields = (*OrderSerializer.Meta.fields, "alt")  # type: ignore[assignment]

        def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
            context = cast("dict[str, Any]", self.context)
            context["pair"] = (self.fetched("ticker"), self.fetched("alt"))
            return attrs

    row = {"ticker": "1101", "customer": "112001", "volume": 1, "reference_id": "1102"}
    serializer, context = run(S, [row])

    assert serializer.is_valid(), serializer.errors
    assert context["pair"] == (tickers[0], tickers[1])


def test_fetched_rejects_unknown_names_and_is_none_outside_validation():
    s = OrderSerializer(context={})
    with pytest.raises(KeyError):
        s.fetched("volume")
    assert s.fetched("ticker") is None


def test_replacement_collector_receives_derived_and_stored_keys(orders):
    seen: list[dict[str, set[Any]]] = []

    class Collector(BatchCollector):
        def collect(self, keys: dict[str, set[Any]], context: Any) -> dict[str, Any]:
            seen.append({name: set(values) for name, values in keys.items()})
            return {"tag": {}, "grade": {}, "ticker": {}, "customer": {}}

    serializer, _ = run(
        DerivedSerializer,
        [{"id": orders[0].pk, "volume": 500}],
        operation="update",
        collector=Collector(),
    )

    assert serializer.is_valid(), serializer.errors
    assert seen == [{"ticker": {"1101"}, "tag": {("1101", 500)}, "grade": {True}}]


# ---------------------------------------------------------------------------
# pass order: 404 / 403 before any fetch
# ---------------------------------------------------------------------------


def _spied_service() -> tuple[type[BatchSerializer], list[set[Any]]]:
    calls: list[set[Any]] = []

    class S(OrderSerializer):
        reference_id = BatchServiceField(fetch=spy_fetch(calls))  # type: ignore[assignment]

    return S, calls


def test_not_found_raises_before_any_fetch(orders):
    S, calls = _spied_service()
    context: dict[str, Any] = {}
    serializer = BatchListSerializer(
        child=S(context=context),
        data=[{"id": 999_999, "volume": 1}],
        context=context,
        operation="update",
    )

    with pytest.raises(BatchNotFound):
        serializer.is_valid()
    assert calls == []


def test_permission_denied_raises_before_any_fetch(orders):
    S, calls = _spied_service()

    def deny(obj: Any) -> None:
        raise PermissionDenied("nope")

    context: dict[str, Any] = {}
    serializer = BatchListSerializer(
        child=S(context=context),
        data=[{"id": orders[0].pk, "volume": 1}],
        context=context,
        operation="update",
        check_object=deny,
    )

    with pytest.raises(BatchPermissionDenied):
        serializer.is_valid()
    assert calls == []


# ---------------------------------------------------------------------------
# configuration errors
# ---------------------------------------------------------------------------


def _build(**field_kwargs: Any) -> None:
    class S(OrderSerializer):
        extra = BatchServiceField(
            fetch=lambda keys, collected, context: {}, read_only=True, **field_kwargs
        )

        class Meta(OrderSerializer.Meta):
            fields = (*OrderSerializer.Meta.fields, "extra")  # type: ignore[assignment]

    BatchListSerializer(child=S(context={}), data=[])


def test_key_without_derived_from_is_a_type_error():
    with pytest.raises(TypeError, match="derived_from"):
        BatchServiceField(fetch=lambda *a: {}, key=lambda value: value)


def test_non_callable_key_is_a_type_error():
    with pytest.raises(TypeError, match="callable"):
        BatchServiceField(fetch=lambda *a: {}, derived_from="volume", key="nope")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("derived_from", "match"),
    [
        ("nope", "unknown field 'nope'"),
        ("extra", "the field itself"),
    ],
)
def test_bad_derived_from_fails_fast(derived_from, match):
    with pytest.raises(ImproperlyConfigured, match=match):
        _build(derived_from=derived_from)


def test_derived_from_cycle_fails_fast():
    class S(OrderSerializer):
        a = BatchServiceField(fetch=lambda *args: {}, derived_from="b", read_only=True)
        b = BatchServiceField(fetch=lambda *args: {}, derived_from="a", read_only=True)

        class Meta(OrderSerializer.Meta):
            fields = (*OrderSerializer.Meta.fields, "a", "b")  # type: ignore[assignment]

    with pytest.raises(ImproperlyConfigured, match="circular derived_from"):
        BatchListSerializer(child=S(context={}), data=[])


def test_derived_from_plain_related_field_fails_fast():
    class S(BatchSerializer):
        ticker = serializers.PrimaryKeyRelatedField(queryset=Ticker.objects.all())
        extra = BatchServiceField(
            fetch=lambda *args: {}, derived_from="ticker", read_only=True
        )

        class Meta:
            model = Order
            fields = ("ticker", "extra")

    with pytest.raises(ImproperlyConfigured, match="BatchRelatedField"):
        BatchListSerializer(child=S(context={}), data=[])


# ---------------------------------------------------------------------------
# endpoint: POST / PATCH / PUT through BatchMixin (ServiceOrderSerializer)
# ---------------------------------------------------------------------------


class TestServiceEndpoint:
    def test_patch_without_ticker_checks_the_stored_ticker(
        self, client: APIClient, orders
    ):
        # orders[1] holds ticker 1102 (band B, suspended): a PATCH that only
        # changes volume is still checked against it.
        response = client.patch(
            SERVICE_BATCH_URL, [{"id": orders[1].pk, "volume": 3}], format="json"
        )

        assert response.status_code == 400
        assert response.json() == [{"ticker": ["Ticker is suspended on exchange B."]}]

    def test_patch_without_ticker_on_a_listed_ticker_passes(
        self, client: APIClient, orders
    ):
        response = client.patch(
            SERVICE_BATCH_URL, [{"id": orders[0].pk, "volume": 3}], format="json"
        )

        assert response.status_code == 200
        orders[0].refresh_from_db()
        assert orders[0].volume == 3

    def test_put_checks_the_sent_ticker(self, client: APIClient, orders):
        orders[1].delete()  # frees (1102, customer) for the unique pair
        row = {
            "id": orders[0].pk,
            "ticker": "1102",
            "customer": "112001",
            "volume": 3,
            "reference_id": "P",
        }
        response = client.put(SERVICE_BATCH_URL, [row], format="json")

        assert response.status_code == 400
        assert response.json() == [{"ticker": ["Ticker is suspended on exchange B."]}]

    def test_post_checks_the_sent_ticker(self, client: APIClient, tickers, customers):
        row = {"ticker": "1101", "customer": "112001", "volume": 3, "reference_id": "C"}
        response = client.post(SERVICE_BATCH_URL, [row], format="json")

        assert response.status_code == 201
