from __future__ import annotations

import asyncio
from typing import Any

import pytest
from django.core.exceptions import (
    ImproperlyConfigured,
)
from django.core.exceptions import (
    ValidationError as DjangoValidationError,
)
from django.db import IntegrityError, connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework import serializers
from rest_framework.exceptions import ValidationError

from drf_batch import (
    BatchCollector,
    BatchListSerializer,
    BatchSerializer,
    CachedRelatedField,
    CachedServiceField,
    loop,
)
from drf_batch.exceptions import BatchCollectionTimeout, BatchConflict
from drf_batch.pipeline import _normalize_row_error
from tests.apps.demo.collectors import SpyCollector
from tests.apps.demo.models import Order, Ticker
from tests.apps.demo.serializers import OrderSerializer, ServiceOrderSerializer


@pytest.fixture(autouse=True)
def _shutdown_batch_loop():
    yield
    loop.shutdown()


def make_list_serializer(rows, **kwargs):
    context: dict[str, Any] = {}
    serializer = BatchListSerializer(
        child=OrderSerializer(context=context), data=rows, context=context, **kwargs
    )
    return serializer, context


def _rows(n: int = 2) -> list[dict[str, Any]]:
    base = [
        {"ticker": "1101", "customer": "112001", "volume": 1, "reference_id": "A"},
        {"ticker": "1102", "customer": "112001", "volume": 2, "reference_id": "B"},
    ]
    return base[:n]


def test_collected_comes_from_context_and_defaults_to_empty():
    s = OrderSerializer(context={"collected": {"ticker": {"a": 1}}})
    assert s.collected == {"ticker": {"a": 1}}

    bare = OrderSerializer(context={})
    assert bare.collected == {}


def test_validated_data_is_a_list_of_row_dicts(tickers, customers):
    serializer, _ = make_list_serializer(_rows())

    assert serializer.is_valid()
    validated = serializer.validated_data

    assert isinstance(validated, list) and len(validated) == 2
    assert validated[0]["ticker"] == tickers[0]
    assert validated[1]["ticker"] == tickers[1]
    assert validated[1]["customer"] == customers[0]


def test_create_is_all_or_none_and_bulk_by_default(tickers, customers):
    # The default path is bulk_create: SQLite sets the PKs on the returned
    # instances, and everything lands in a single transaction.
    serializer, _ = make_list_serializer(_rows())
    serializer.is_valid(raise_exception=True)
    instances = serializer.save()

    assert [type(i) for i in instances] == [Order, Order]
    assert all(i.pk is not None for i in instances)
    assert Order.objects.count() == 2


def test_per_row_create_when_use_bulk_is_false(tickers, customers):
    created: list[Any] = []

    class RowSpy(OrderSerializer):
        def create(self, validated_data: Any) -> Any:
            instance = super().create(validated_data)
            created.append(instance)
            return instance

    context: dict[str, Any] = {}
    serializer = BatchListSerializer(
        child=RowSpy(context=context),
        data=[
            {"ticker": "1101", "customer": "112001", "volume": 1, "reference_id": "NB"}
        ],
        context=context,
        use_bulk=False,
    )
    serializer.is_valid(raise_exception=True)

    instances = serializer.save()

    assert created == instances
    assert Order.objects.count() == 1


def _with_extra_serializer():
    class WithExtra(OrderSerializer):
        extra = serializers.CharField(required=True)

        class Meta:
            model = Order
            fields = ("id", "ticker", "customer", "volume", "reference_id", "extra")

    return WithExtra


def test_bulk_mode_fails_fast_on_writable_non_model_field(tickers, customers):
    context: dict[str, Any] = {}

    with pytest.raises(ImproperlyConfigured, match="extra"):
        BatchListSerializer(
            child=_with_extra_serializer()(context=context), data=[], context=context
        )


def test_non_bulk_mode_allows_writable_non_model_field(tickers, customers):
    context: dict[str, Any] = {}

    serializer = BatchListSerializer(
        child=_with_extra_serializer()(context=context),
        data=[],
        context=context,
        use_bulk=False,
    )

    assert serializer.is_valid()
    assert serializer.save() == []


def test_create_rolls_back_when_a_row_hits_the_db(tickers, customers):
    # volume >= 1_000_000 violates a CheckConstraint the serializer does not
    # know about, so validation passes and the DB rejects the row.
    rows = [
        {"ticker": "1102", "customer": "112001", "volume": 7, "reference_id": "A"},
        {
            "ticker": "1101",
            "customer": "112001",
            "volume": 2_000_000,
            "reference_id": "B",
        },
    ]
    serializer, _ = make_list_serializer(rows)
    serializer.is_valid(raise_exception=True)

    with pytest.raises(BatchConflict) as excinfo:
        serializer.save()

    assert isinstance(excinfo.value.__cause__, IntegrityError)
    assert excinfo.value.get_codes() == {"detail": "batch_integrity_conflict"}
    assert Order.objects.count() == 0


def test_non_dict_row_is_normalized_to_all_errors(tickers, customers):
    rows = [
        {"ticker": "1101", "customer": "112001", "volume": 1, "reference_id": "A"},
        "oops",
    ]
    serializer, _ = make_list_serializer(rows)

    assert serializer.is_valid() is False
    assert serializer.errors == [
        {},
        {"non_field_errors": ["Invalid row: expected a dictionary, got str."]},
    ]


def test_non_list_data_fails_with_not_a_list(tickers, customers):
    serializer, _ = make_list_serializer({"ticker": "1101"})

    with pytest.raises(ValidationError):
        serializer.is_valid(raise_exception=True)


def test_empty_rows_validate_to_empty_list_and_issue_no_queries(tickers, customers):
    serializer, _ = make_list_serializer([])

    assert serializer.is_valid()
    assert serializer.validated_data == []
    with CaptureQueriesContext(connection) as ctx:
        assert serializer.save() == []
    assert ctx.captured_queries == []


def test_normalize_row_error_shapes():
    assert _normalize_row_error({"a": ["b"]}) == {"a": ["b"]}
    assert _normalize_row_error(["x", "y"]) == {"non_field_errors": ["x", "y"]}
    assert _normalize_row_error("boom") == {"non_field_errors": ["boom"]}


def test_dict_data_fails_not_a_list_when_allow_empty_false(tickers, customers):
    serializer, _ = make_list_serializer({"ticker": "1101"}, allow_empty=False)

    with pytest.raises(ValidationError) as exc_info:
        serializer.is_valid(raise_exception=True)

    assert "Expected a list of items" in str(exc_info.value.detail)


def test_empty_list_fails_when_allow_empty_false(tickers, customers):
    serializer, _ = make_list_serializer([], allow_empty=False)

    with pytest.raises(ValidationError) as exc_info:
        serializer.is_valid(raise_exception=True)

    assert "may not be empty" in str(exc_info.value.detail)


def test_django_validation_error_from_child_is_normalized(tickers, customers):
    class ExplodingSerializer(OrderSerializer):
        def run_validation(self, data: Any = None):
            if isinstance(data, dict) and data.get("reference_id") == "BOOM":
                raise DjangoValidationError("field blew up")
            return super().run_validation(data)

    context: dict[str, Any] = {}
    serializer = BatchListSerializer(
        child=ExplodingSerializer(context=context),
        data=[
            {
                "ticker": "1101",
                "customer": "112001",
                "volume": 1,
                "reference_id": "BOOM",
            }
        ],
        context=context,
    )

    assert serializer.is_valid() is False
    assert serializer.errors == [{"non_field_errors": ["field blew up"]}]


# ---------------------------------------------------------------------------
# auto collection
# ---------------------------------------------------------------------------


def test_auto_collection_fetches_with_exact_key_sets(tickers, customers):
    calls: list[set[Any]] = []

    class SpyTicker(CachedRelatedField):
        def fetch(self, keys, collected, context):
            calls.append(set(keys))
            return super().fetch(keys, collected, context)

    class S(OrderSerializer):
        ticker = SpyTicker(lookup="isin", error="Ticker '{value}' not found.")

    context: dict[str, Any] = {}
    rows = [
        {"ticker": "1101", "customer": "112001", "volume": 1, "reference_id": "A"},
        {"ticker": 1102, "customer": "112001", "volume": 2, "reference_id": "B"},
    ]
    serializer = BatchListSerializer(
        child=S(context=context), data=rows, context=context
    )

    assert serializer.is_valid()

    # One ticker fetch with the union of coerced keys (int 1102 -> "1102");
    # maps land in the context under the field-name keys.
    assert calls == [{"1101", "1102"}]
    assert context["collected"]["ticker"]["1101"] == tickers[0]
    assert context["collected"]["customer"]["112001"] == customers[0]

    validated = serializer.validated_data
    assert validated[0]["ticker"] == tickers[0]
    assert validated[1]["ticker"] == tickers[1]


def test_source_differing_from_field_name_follows_drf_input_semantics(
    tickers, customers
):
    # DRF reads the row key from the field name and places the validated
    # value under the source; extraction, collection, and create follow
    # that (the FK auto-resolves from the source's attribute name).
    class S(OrderSerializer):
        ticker_sym = CachedRelatedField(
            source="ticker", lookup="isin", error="Ticker '{value}' not found."
        )

        class Meta:
            model = Order
            fields = ("id", "ticker_sym", "customer", "volume", "reference_id")

    context: dict[str, Any] = {}
    rows = [
        {"ticker_sym": "1101", "customer": "112001", "volume": 1, "reference_id": "A"},
        {"ticker_sym": "1102", "customer": "112001", "volume": 2, "reference_id": "B"},
    ]
    serializer = BatchListSerializer(
        child=S(context=context), data=rows, context=context
    )

    assert serializer.is_valid()

    # The map is named after the field; validated_data after the source.
    assert context["collected"]["ticker_sym"] == {
        "1101": tickers[0],
        "1102": tickers[1],
    }
    validated = serializer.validated_data
    assert validated[0]["ticker"] == tickers[0]
    assert validated[1]["ticker"] == tickers[1]

    serializer.save()
    assert Order.objects.get(reference_id="A").ticker == tickers[0]


def test_source_differing_from_field_name_keys_pass1_errors(tickers, customers):
    class S(OrderSerializer):
        ticker_sym = CachedRelatedField(
            source="ticker", lookup="isin", error="Ticker '{value}' not found."
        )

        class Meta:
            model = Order
            fields = ("id", "ticker_sym", "customer", "volume", "reference_id")

    rows = [
        {"ticker_sym": "1101", "customer": "112001", "volume": 1, "reference_id": "A"},
        {"ticker_sym": [1, 2], "customer": "112001", "volume": 2, "reference_id": "B"},
    ]
    context: dict[str, Any] = {}
    serializer = BatchListSerializer(
        child=S(context=context), data=rows, context=context
    )

    with pytest.raises(ValidationError) as exc_info:
        serializer.is_valid(raise_exception=True)

    assert exc_info.value.detail == [
        {},
        {
            "ticker_sym": [
                "Invalid value for 'ticker_sym': expected a string, got list."
            ]
        },
    ]


def test_malformed_key_is_400_and_fetch_never_called(tickers, customers):
    calls: list[set[Any]] = []

    class SpyTicker(CachedRelatedField):
        def fetch(self, keys, collected, context):
            calls.append(set(keys))
            return super().fetch(keys, collected, context)

    class S(OrderSerializer):
        ticker = SpyTicker(lookup="isin", error="Ticker '{value}' not found.")

    rows = [
        {"ticker": "1101", "customer": "112001", "volume": 1, "reference_id": "A"},
        {"ticker": [1, 2], "customer": "112001", "volume": 2, "reference_id": "B"},
    ]
    context: dict[str, Any] = {}
    serializer = BatchListSerializer(
        child=S(context=context), data=rows, context=context
    )

    with pytest.raises(ValidationError) as exc_info:
        serializer.is_valid(raise_exception=True)

    assert exc_info.value.detail == [
        {},
        {"ticker": ["Invalid value for 'ticker': expected a string, got list."]},
    ]
    assert calls == []


def test_empty_key_set_skips_the_fetch(tickers, customers):
    calls: list[set[Any]] = []

    class SpyTicker(CachedRelatedField):
        def fetch(self, keys, collected, context):
            calls.append(set(keys))
            return super().fetch(keys, collected, context)

    class S(OrderSerializer):
        ticker = SpyTicker(lookup="isin", error="Ticker '{value}' not found.")

    rows = [{"customer": "112001", "volume": 1, "reference_id": "A"}]
    context: dict[str, Any] = {}
    serializer = BatchListSerializer(
        child=S(context=context), data=rows, context=context
    )

    assert serializer.is_valid() is False
    assert serializer.errors == [{"ticker": ["This field is required."]}]
    assert calls == []
    assert context["collected"]["ticker"] == {}


def test_two_fields_sharing_a_map_do_one_union_fetch(tickers, customers):
    calls: list[set[Any]] = []

    class SpyTicker(CachedRelatedField):
        def fetch(self, keys, collected, context):
            calls.append(set(keys))
            return super().fetch(keys, collected, context)

    class S(OrderSerializer):
        ticker = SpyTicker(lookup="isin", error="Ticker '{value}' not found.")
        ticker_alt = CachedRelatedField(
            lookup="isin", model=Ticker, map="ticker", read_only=True
        )

        class Meta:
            model = Order
            fields = (
                "id",
                "ticker",
                "ticker_alt",
                "customer",
                "volume",
                "reference_id",
            )

    rows = [
        {
            "ticker": "1101",
            "ticker_alt": "1102",
            "customer": "112001",
            "volume": 1,
            "reference_id": "A",
        }
    ]
    context: dict[str, Any] = {}
    serializer = BatchListSerializer(
        child=S(context=context), data=rows, context=context
    )

    assert serializer.is_valid()

    assert calls == [{"1101", "1102"}]
    assert serializer.validated_data[0]["ticker"] == tickers[0]
    assert "ticker_alt" not in serializer.validated_data[0]


# ---------------------------------------------------------------------------
# depends_on
# ---------------------------------------------------------------------------


def test_depends_on_orders_fetches_topologically(tickers, customers):
    order_log: list[str] = []

    def db_fetch(keys, collected, context):
        order_log.append("db")
        return {"k": "v"}

    def svc_fetch(keys, collected, context):
        order_log.append("svc")
        return {"x": 1}

    class DepSerializer(BatchSerializer):
        # Declared before its dependency on purpose.
        svc = CachedServiceField(fetch=svc_fetch, depends_on=("db",), read_only=True)
        db = CachedServiceField(fetch=db_fetch, read_only=True)

        class Meta:
            model = Order
            fields = ("db", "svc")

    rows = [{"db": "k", "svc": "x"}]
    context: dict[str, Any] = {}
    serializer = BatchListSerializer(
        child=DepSerializer(context=context), data=rows, context=context
    )

    assert serializer.is_valid()

    assert order_log == ["db", "svc"]
    assert context["collected"]["db"] == {"k": "v"}
    assert context["collected"]["svc"] == {"x": 1}


def test_dependency_fetch_sees_the_maps_collected_so_far(tickers, customers):
    seen: dict[str, Any] = {}

    def svc_fetch(keys, collected, context):
        seen["ticker"] = collected.get("ticker")
        return {"x": 1}

    class DepSerializer(BatchSerializer):
        ticker = CachedRelatedField(lookup="isin", error="Ticker '{value}' not found.")
        customer = CachedRelatedField(
            lookup="isin", error="Customer '{value}' not found."
        )
        svc = CachedServiceField(
            fetch=svc_fetch, depends_on=("ticker",), read_only=True
        )
        volume = serializers.IntegerField(min_value=1)
        reference_id = serializers.CharField(max_length=64)

        class Meta:
            model = Order
            fields = ("id", "ticker", "customer", "volume", "reference_id", "svc")

    rows = [
        {
            "ticker": "1101",
            "customer": "112001",
            "volume": 1,
            "reference_id": "A",
            "svc": "x",
        }
    ]
    context: dict[str, Any] = {}
    serializer = BatchListSerializer(
        child=DepSerializer(context=context), data=rows, context=context
    )

    assert serializer.is_valid()

    assert seen["ticker"] == {"1101": tickers[0]}


def test_unknown_dependency_raises_improperly_configured():
    class S(BatchSerializer):
        a = CachedServiceField(
            fetch=lambda keys, collected, context: {},
            depends_on=("nope",),
            read_only=True,
        )

        class Meta:
            model = Order
            fields = ("a",)

    with pytest.raises(ImproperlyConfigured, match="unknown cached field 'nope'"):
        BatchListSerializer(child=S(context={}), data=[])


def test_circular_dependency_raises_improperly_configured():
    class S(BatchSerializer):
        a = CachedServiceField(
            fetch=lambda keys, collected, context: {}, depends_on=("b",), read_only=True
        )
        b = CachedServiceField(
            fetch=lambda keys, collected, context: {}, depends_on=("a",), read_only=True
        )

        class Meta:
            model = Order
            fields = ("a", "b")

    with pytest.raises(ImproperlyConfigured, match="circular depends_on"):
        BatchListSerializer(child=S(context={}), data=[])


# ---------------------------------------------------------------------------
# read-only cached fields
# ---------------------------------------------------------------------------


def test_read_only_service_field_hit_is_not_in_validated_data(tickers, customers):
    rows = [
        {
            "ticker": "1101",
            "customer": "112001",
            "volume": 1,
            "reference_id": "A",
            "band": "A",
        }
    ]
    context: dict[str, Any] = {}
    serializer = BatchListSerializer(
        child=ServiceOrderSerializer(context=context), data=rows, context=context
    )

    assert serializer.is_valid()

    validated = serializer.validated_data[0]
    assert "band" not in validated
    assert "exchange_ticker" not in validated
    assert validated["ticker"] == tickers[0]


def test_read_only_service_field_miss_is_row_error(tickers, customers):
    rows = [
        {
            "ticker": "1101",
            "customer": "112001",
            "volume": 1,
            "reference_id": "A",
            "band": "X",
        }
    ]
    serializer = BatchListSerializer(
        child=ServiceOrderSerializer(context={}), data=rows, context={}
    )

    assert serializer.is_valid() is False
    assert serializer.errors == [{"band": ["Object 'X' not found."]}]


def test_read_only_row_without_a_key_is_fine(tickers, customers):
    # No "band" in the row: no key, no check against the map. The service
    # fetch still runs (its map is derived from collected["ticker"]).
    rows = [{"ticker": "1101", "customer": "112001", "volume": 1, "reference_id": "A"}]
    context: dict[str, Any] = {}
    serializer = BatchListSerializer(
        child=ServiceOrderSerializer(context=context), data=rows, context=context
    )

    assert serializer.is_valid()
    assert serializer.validated_data[0]["ticker"] == tickers[0]
    assert context["collected"]["band"] == {"A": ["1101"]}


def test_service_fetch_runs_without_own_keys_and_feeds_validate(tickers, customers):
    # No "band" in the row, yet the service fetch must still run so that
    # validate() can consume the map: ticker 1102 is band "B" (suspended).
    rows = [{"ticker": "1102", "customer": "112001", "volume": 1, "reference_id": "A"}]
    context: dict[str, Any] = {}
    serializer = BatchListSerializer(
        child=ServiceOrderSerializer(context=context), data=rows, context=context
    )

    assert serializer.is_valid() is False
    assert serializer.errors == [{"ticker": ["Ticker is suspended on exchange B."]}]
    assert context["collected"]["band"] == {"B": ["1102"]}


def test_db_after_service_scoped_fetch_miss(tickers, customers):
    # Band 'A' hit (ticker 1101 is collected), but the row asserts an
    # exchange_ticker of 1102, which the scoped DB fetch (band-'A' only)
    # cannot resolve.
    rows = [
        {
            "ticker": "1101",
            "customer": "112001",
            "volume": 1,
            "reference_id": "A",
            "band": "A",
            "exchange_ticker": "1102",
        }
    ]
    serializer = BatchListSerializer(
        child=ServiceOrderSerializer(context={}), data=rows, context={}
    )

    assert serializer.is_valid() is False
    assert serializer.errors == [
        {"exchange_ticker": ["Ticker '1102' is not listed on exchange A."]}
    ]


def test_validate_sees_service_results_via_collected(tickers, customers):
    # The demo validate() rejects band-'B' tickers — it reads the service map
    # from self.collected (zero I/O).
    rows = [
        {
            "ticker": "1102",
            "customer": "112001",
            "volume": 1,
            "reference_id": "A",
            "band": "B",
        }
    ]
    serializer = BatchListSerializer(
        child=ServiceOrderSerializer(context={}), data=rows, context={}
    )

    assert serializer.is_valid() is False
    assert serializer.errors == [{"ticker": ["Ticker is suspended on exchange B."]}]


# ---------------------------------------------------------------------------
# collector replacement
# ---------------------------------------------------------------------------


def test_collector_replacement_receives_coerced_key_sets(tickers, customers):
    rows = [
        {"ticker": 1101, "customer": "112001", "volume": 1, "reference_id": "A"},
    ]
    context: dict[str, Any] = {}
    collector = SpyCollector()
    serializer = BatchListSerializer(
        child=OrderSerializer(context=context),
        data=rows,
        context=context,
        collector=collector,
    )

    assert serializer.is_valid()

    assert collector.collect_calls == 1
    assert collector.seen_keys == {"ticker": {"1101"}, "customer": {"112001"}}
    assert context["collected"]["ticker"]["1101"] == tickers[0]
    assert serializer.validated_data[0]["ticker"] == tickers[0]


def test_collector_missing_map_gives_row_miss_errors(tickers, customers):
    class PartialCollector(BatchCollector):
        def collect(self, keys, context):
            return {
                "ticker": Ticker.objects.filter(
                    isin__in=keys.get("ticker", set())
                ).in_bulk(field_name="isin")
            }

    rows = [
        {"ticker": "1101", "customer": "112001", "volume": 1, "reference_id": "A"},
    ]
    context: dict[str, Any] = {}
    serializer = BatchListSerializer(
        child=OrderSerializer(context=context),
        data=rows,
        context=context,
        collector=PartialCollector(),
    )

    assert serializer.is_valid() is False
    assert serializer.errors == [{"customer": ["Customer '112001' not found."]}]


# ---------------------------------------------------------------------------
# async auto collection
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_async_service_fetch_bridged_to_the_loop(tickers, customers):
    # transaction=True: the fetches run on other connections (thread pool),
    # which need the fixture data committed, not in a rollback transaction.
    async def async_band(keys, collected, context):
        await asyncio.sleep(0.01)
        return {"A": [t.isin for t in collected.get("ticker", {}).values()]}

    class Svc(OrderSerializer):
        band = CachedServiceField(
            fetch=async_band, depends_on=("ticker",), read_only=True
        )

        class Meta:
            model = Order
            fields = (*OrderSerializer.Meta.fields, "band")

    rows = [
        {
            "ticker": "1101",
            "customer": "112001",
            "volume": 1,
            "reference_id": "A",
            "band": "A",
        }
    ]
    context: dict[str, Any] = {}
    serializer = BatchListSerializer(
        child=Svc(context=context), data=rows, context=context
    )

    assert serializer.is_valid()

    assert loop._loop_thread() is not None
    assert context["collected"]["band"] == {"A": ["1101"]}
    assert serializer.validated_data[0]["ticker"] == tickers[0]


def test_sequential_collection_runs_inline_and_starts_no_loop_thread(
    tickers, customers
):
    assert loop._loop_thread() is None

    serializer, _ = make_list_serializer(_rows(), concurrent_collection=False)
    serializer.is_valid(raise_exception=True)

    assert loop._loop_thread() is None
    assert serializer.validated_data[0]["ticker"] == tickers[0]


@pytest.mark.django_db(transaction=True)
def test_async_fetch_timeout_is_a_collection_timeout(tickers, customers):
    # transaction=True: the sync ticker fetch runs on the thread pool.
    async def slow_band(keys, collected, context):
        await asyncio.sleep(0.5)
        return {}

    class Svc(OrderSerializer):
        band = CachedServiceField(fetch=slow_band, read_only=True)

        class Meta:
            model = Order
            fields = (*OrderSerializer.Meta.fields, "band")

    rows = [
        {
            "ticker": "1101",
            "customer": "112001",
            "volume": 1,
            "reference_id": "A",
            "band": "A",
        }
    ]
    serializer = BatchListSerializer(child=Svc(context={}), data=rows, context={})

    with (
        override_settings(DRF_BATCH={"COLLECT_TIMEOUT": 0.05}),
        pytest.raises(BatchCollectionTimeout),
    ):
        serializer.is_valid(raise_exception=True)


def test_sync_fetch_exception_propagates(tickers, customers):
    def bad_band(keys, collected, context):
        raise ValueError("svc exploded")

    class Svc(OrderSerializer):
        band = CachedServiceField(fetch=bad_band, read_only=True)

        class Meta:
            model = Order
            fields = (*OrderSerializer.Meta.fields, "band")

    rows = [
        {
            "ticker": "1101",
            "customer": "112001",
            "volume": 1,
            "reference_id": "A",
            "band": "A",
        }
    ]
    serializer = BatchListSerializer(child=Svc(context={}), data=rows, context={})

    with pytest.raises(ValueError, match="svc exploded"):
        serializer.is_valid(raise_exception=True)


# ---------------------------------------------------------------------------
# cross-row conflicts (batch_unique_together)
# ---------------------------------------------------------------------------


class CompositeOrderSerializer(OrderSerializer):
    class Meta(OrderSerializer.Meta):
        batch_unique_together = (("volume", "reference_id"),)


def _composite_rows() -> list[dict[str, Any]]:
    return [
        {"ticker": "1101", "customer": "112001", "volume": 5, "reference_id": "C1"},
        {"ticker": "1102", "customer": "112001", "volume": 5, "reference_id": "C1"},
    ]


def test_composite_rule_in_payload_conflict_flags_all_rule_fields(tickers, customers):
    context: dict[str, Any] = {}
    serializer = BatchListSerializer(
        child=CompositeOrderSerializer(context=context),
        data=_composite_rows(),
        context=context,
        operation="create",
    )

    with pytest.raises(BatchConflict) as exc_info:
        serializer.is_valid(raise_exception=True)

    message = "Conflicts with another row in this batch."
    assert exc_info.value.detail == [
        {"volume": [message], "reference_id": [message]},
        {"volume": [message], "reference_id": [message]},
    ]


def test_composite_rule_requires_all_fields_to_match(tickers, customers):
    # Same volume, different reference_id -> no conflict.
    context: dict[str, Any] = {}
    rows = [
        {"ticker": "1101", "customer": "112001", "volume": 5, "reference_id": "C1"},
        {"ticker": "1102", "customer": "112001", "volume": 5, "reference_id": "C2"},
    ]
    serializer = BatchListSerializer(
        child=CompositeOrderSerializer(context=context),
        data=rows,
        context=context,
        operation="create",
    )

    assert serializer.is_valid()
    assert serializer.save()  # persists


def test_single_field_rule_db_conflict(tickers, customers):
    Order.objects.create(
        ticker=tickers[0], customer=customers[0], volume=1, reference_id="TAKEN"
    )
    context: dict[str, Any] = {}
    rows = [
        {"ticker": "1102", "customer": "112001", "volume": 1, "reference_id": "TAKEN"}
    ]
    from tests.apps.demo.serializers import ConflictOrderSerializer

    serializer = BatchListSerializer(
        child=ConflictOrderSerializer(context=context),
        data=rows,
        context=context,
        operation="create",
    )

    with pytest.raises(BatchConflict) as exc_info:
        serializer.is_valid(raise_exception=True)

    assert exc_info.value.detail == [
        {"reference_id": ["Conflicts with an existing record."]}
    ]


def test_bad_rule_field_raises_improperly_configured():
    class BadSerializer(OrderSerializer):
        class Meta(OrderSerializer.Meta):
            batch_unique_together = ("no_such_field",)

    with pytest.raises(ImproperlyConfigured, match="no_such_field"):
        BatchListSerializer(child=BadSerializer(context={}), data=[], context={})
