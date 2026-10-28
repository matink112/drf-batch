from __future__ import annotations

import pytest
from django.test import override_settings
from rest_framework.test import APIClient

from tests import conftest
from tests.apps.demo.models import Order, SymbolOrder, Ticker

BATCH_URL = "/orders/batch/"
SMALL_BATCH_URL = "/small-orders/batch/"
NO_BULK_BATCH_URL = "/no-bulk-orders/batch/"
SYMBOL_BATCH_URL = "/symbol-orders/batch/"
SERVICE_BATCH_URL = "/service-orders/batch/"
CONFLICT_BATCH_URL = "/conflict-orders/batch/"
FLAKY_BATCH_URL = "/flaky-orders/batch/"
CREATE_ONLY_BATCH_URL = "/create-only-orders/batch/"


def row(ticker: str, customer: str, volume: int, reference_id: str):
    return {
        "ticker": ticker,
        "customer": customer,
        "volume": volume,
        "reference_id": reference_id,
    }


class TestBatchCreate:
    def test_happy_path_persists_rows_and_returns_count(
        self, client, tickers, customers, valid_rows
    ):
        response = client.post(BATCH_URL, valid_rows)

        assert response.status_code == 201
        assert response.json() == {"count": 2}
        assert Order.objects.count() == 2
        order = Order.objects.get(reference_id="REF_A_1")
        assert order.ticker.isin == "1101"
        assert order.customer.isin == "112001"
        assert order.volume == 10

    def test_empty_list_is_legal(self, client, tickers, customers):
        response = client.post(BATCH_URL, [])

        assert response.status_code == 201
        assert response.json() == {"count": 0}
        assert Order.objects.count() == 0

    def test_index_aligned_errors_with_empty_objects_for_valid_rows(
        self, client, tickers, customers
    ):
        rows = [
            row("1101", "112001", 10, "R1"),
            row("XYZ", "112001", 10, "R2"),
            row("1102", "112001", 3, "R3"),
        ]
        response = client.post(BATCH_URL, rows)

        assert response.status_code == 400
        assert response.json() == [
            {},
            {"ticker": ["Ticker 'XYZ' not found."]},
            {},
        ]
        assert Order.objects.count() == 0

    def test_row_with_multiple_field_errors_reports_all_of_them(
        self, client, tickers, customers
    ):
        rows = [row("NOPE", "MISSING", 0, "R1")]
        response = client.post(BATCH_URL, rows)

        assert response.status_code == 400
        assert response.json() == [
            {
                "ticker": ["Ticker 'NOPE' not found."],
                "customer": ["Customer 'MISSING' not found."],
                "volume": ["Ensure this value is greater than or equal to 1."],
            },
        ]

    def test_non_dict_row_is_normalized_to_all_errors(self, client, tickers, customers):
        rows = [row("1101", "112001", 10, "R1"), "oops"]
        response = client.post(BATCH_URL, rows)

        assert response.status_code == 400
        assert response.json() == [
            {},
            {"non_field_errors": ["Invalid row: expected a dictionary, got str."]},
        ]

    def test_non_list_body_is_400(self, client, tickers, customers):
        response = client.post(BATCH_URL, {"ticker": "1101"})

        assert response.status_code == 400

    def test_invalid_row_leaves_valid_row_unpersisted(self, client, tickers, customers):
        rows = [row("1101", "112001", 10, "R1"), row("XYZ", "112001", 10, "R2")]
        response = client.post(BATCH_URL, rows)

        assert response.status_code == 400
        assert Order.objects.count() == 0

    def test_coerced_int_isin_persists(self, client, tickers, customers):
        # An int ISIN is coerced to its str form for the char lookup.
        rows = [
            {"ticker": 1101, "customer": "112001", "volume": 10, "reference_id": "C1"}
        ]
        response = client.post(BATCH_URL, rows)

        assert response.status_code == 201
        assert response.json() == {"count": 1}
        order = Order.objects.get(reference_id="C1")
        assert order.ticker.isin == "1101"

    def test_malformed_key_is_400_and_nothing_is_persisted(
        self, client, tickers, customers
    ):
        rows = [
            row("1101", "112001", 10, "M1"),
            {
                "ticker": [1, 2],
                "customer": "112001",
                "volume": 10,
                "reference_id": "M2",
            },
        ]
        response = client.post(BATCH_URL, rows)

        assert response.status_code == 400
        assert response.json() == [
            {},
            {"ticker": ["Invalid value for 'ticker': expected a string, got list."]},
        ]
        assert Order.objects.count() == 0


class TestUUidPkLookup:
    def test_str_uuid_persists_via_pk_lookup(self, client, symbols):
        rows = [
            {"symbol": str(conftest.SYMBOL_A_ID), "volume": 7, "reference_id": "U1"},
        ]
        response = client.post(SYMBOL_BATCH_URL, rows)

        assert response.status_code == 201
        assert response.json() == {"count": 1}
        assert SymbolOrder.objects.count() == 1
        assert (
            SymbolOrder.objects.get(reference_id="U1").symbol_id == conftest.SYMBOL_A_ID
        )

    def test_malformed_uuid_is_400(self, client, symbols):
        rows = [{"symbol": "not-a-uuid", "volume": 7, "reference_id": "U2"}]
        response = client.post(SYMBOL_BATCH_URL, rows)

        assert response.status_code == 400
        assert response.json() == [
            {"symbol": ["Invalid value for 'symbol': expected a UUID, got str."]}
        ]
        assert SymbolOrder.objects.count() == 0

    def test_unknown_uuid_is_a_miss(self, client, symbols):
        import uuid as _uuid

        rows = [{"symbol": str(_uuid.uuid4()), "volume": 7, "reference_id": "U3"}]
        response = client.post(SYMBOL_BATCH_URL, rows)

        assert response.status_code == 400
        assert "not found" in str(response.json()[0]["symbol"])
        assert SymbolOrder.objects.count() == 0


class TestServiceField:
    def test_service_and_dependency_persist(self, client, tickers, customers):
        rows = [
            {
                "ticker": "1101",
                "customer": "112001",
                "volume": 10,
                "reference_id": "SV1",
                "band": "A",
                "exchange_ticker": "1101",
            }
        ]
        response = client.post(SERVICE_BATCH_URL, rows)

        assert response.status_code == 201
        assert response.json() == {"count": 1}
        assert Order.objects.count() == 1

    def test_read_only_service_field_value_is_ignored(self, client, tickers, customers):
        rows = [
            {
                "ticker": "1101",
                "customer": "112001",
                "volume": 10,
                "reference_id": "SV2",
                "band": "X",
            }
        ]
        response = client.post(SERVICE_BATCH_URL, rows)

        assert response.status_code == 201
        assert Order.objects.count() == 1

    def test_db_after_service_scoped_miss_is_400(self, client, tickers, customers):
        Ticker.objects.create(isin="1103", name="Gamma Corp")  # no band
        rows = [
            {
                "ticker": "1103",
                "customer": "112001",
                "volume": 10,
                "reference_id": "SV3",
            }
        ]
        response = client.post(SERVICE_BATCH_URL, rows)

        assert response.status_code == 400
        assert response.json() == [
            {"exchange_ticker": ["Ticker is not listed on exchange A."]}
        ]
        assert Order.objects.count() == 0

    def test_validate_consumes_service_result(self, client, tickers, customers):
        # Band 'B' (Beta Corp) is suspended by the serializer's validate().
        rows = [
            {
                "ticker": "1102",
                "customer": "112001",
                "volume": 10,
                "reference_id": "SV4",
                "band": "B",
            }
        ]
        response = client.post(SERVICE_BATCH_URL, rows)

        assert response.status_code == 400
        assert response.json() == [{"ticker": ["Ticker is suspended on exchange B."]}]
        assert Order.objects.count() == 0


class TestBatchSizeLimit:
    def test_view_attribute_limit_returns_413(
        self, client, tickers, customers, valid_rows
    ):
        rows = [*valid_rows, row("1101", "112001", 1, "R3")]
        response = client.post(SMALL_BATCH_URL, rows)

        assert response.status_code == 413
        assert Order.objects.count() == 0

    def test_default_setting_limit_returns_413(self, client, tickers, customers):
        rows = [row("1101", "112001", 1, f"R{i}") for i in range(1001)]
        response = client.post(BATCH_URL, rows)

        assert response.status_code == 413

    def test_setting_override_applies(self, client, tickers, customers, valid_rows):
        with override_settings(DRF_BATCH={"BATCH_MAX_SIZE": 1}):
            response = client.post(BATCH_URL, valid_rows)

        assert response.status_code == 413


class TestBulkCreate:
    def test_batch_use_bulk_false_opt_out_still_persists_rows(
        self, client, tickers, customers, valid_rows
    ):
        response = client.post(NO_BULK_BATCH_URL, valid_rows)

        assert response.status_code == 201
        assert response.json() == {"count": 2}
        assert Order.objects.count() == 2

    def test_get_batch_use_bulk_defaults_to_true_and_honours_override(self):
        from tests.apps.demo.views import OrderViewSet

        view = OrderViewSet()
        assert view.get_batch_use_bulk() is True

        view.batch_use_bulk = False
        assert view.get_batch_use_bulk() is False

    def test_get_batch_bulk_size_resolves_view_attribute_then_setting(self):
        from tests.apps.demo.views import OrderViewSet

        view = OrderViewSet()
        assert view.get_batch_bulk_size() == 1000

        view.batch_bulk_size = 50
        assert view.get_batch_bulk_size() == 50

        view.batch_bulk_size = None
        assert view.get_batch_bulk_size() == 1000
        with override_settings(DRF_BATCH={"BULK_SIZE": 250}):
            assert view.get_batch_bulk_size() == 250


class TestSerializerClassResolution:
    def test_falls_back_to_serializer_class_when_batch_one_not_set(self):
        from tests.apps.demo.serializers import OrderSerializer
        from tests.apps.demo.views import OrderViewSet

        class View(OrderViewSet):
            batch_serializer_class = None
            serializer_class = OrderSerializer

        assert View().get_batch_serializer_class() is OrderSerializer

    def test_batch_serializer_class_wins_when_set(self):
        from tests.apps.demo.views import OrderViewSet

        assert (
            OrderViewSet().get_batch_serializer_class()
            is OrderViewSet.batch_serializer_class
        )


class TestHttpSemantics:
    # POST/PATCH/DELETE are the batch methods; GET is not routed to the
    # action, and PUT is off unless batch_actions lists "replace": both 405.
    @pytest.mark.parametrize("method", ["get", "put"])
    def test_methods_other_than_batch_are_405(self, client, method):
        response = getattr(client, method)(BATCH_URL)

        assert response.status_code == 405

    def test_unauthenticated_request_is_401(self, tickers, customers, valid_rows):
        api_client = APIClient()
        response = api_client.post(BATCH_URL, valid_rows)

        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == 'Basic realm="api"'


class TestRollback:
    def test_db_error_not_caught_by_validation_rolls_back_the_whole_batch(
        self, user, tickers, customers
    ):
        # volume >= 1_000_000 violates a CheckConstraint the serializer does
        # not know about: the IntegrityError becomes a non-aligned 409.
        quiet_client = APIClient(raise_request_exception=False)
        quiet_client.force_authenticate(user=user)

        rows = [
            row("1102", "112001", 7, "N1"),
            row("1101", "112001", 2_000_000, "N2"),
        ]
        response = quiet_client.post(BATCH_URL, rows)

        assert response.status_code == 409
        assert set(response.json()) == {"detail"}
        assert Order.objects.count() == 0

    def test_model_unique_constraint_is_a_409_with_index_aligned_errors(
        self, client, tickers, customers
    ):
        Order.objects.create(
            ticker=tickers[0], customer=customers[0], volume=1, reference_id="SEED"
        )
        rows = [row("1102", "112001", 7, "N1"), row("1101", "112001", 9, "N2")]
        response = client.post(BATCH_URL, rows)

        # The model's UniqueConstraint(ticker, customer) is owned by the
        # conflict pass (one query, no per-row UniqueTogetherValidator).
        assert response.status_code == 409
        msg = "Conflicts with an existing record."
        assert response.json() == [{}, {"ticker": [msg], "customer": [msg]}]
        assert Order.objects.count() == 1

    def test_in_payload_duplicate_of_model_unique_constraint_is_409(
        self, client, tickers, customers
    ):
        # DRF's per-row validator could not see this (both rows are new):
        # previously an IntegrityError at write time.
        rows = [row("1101", "112001", 7, "D1"), row("1101", "112001", 9, "D2")]
        response = client.post(BATCH_URL, rows)

        assert response.status_code == 409
        msg = "Conflicts with another row in this batch."
        assert response.json() == [{"ticker": [msg], "customer": [msg]}] * 2
        assert Order.objects.count() == 0


class TestConflict:
    """Cross-row / existing-record conflicts (409) via batch_unique_together."""

    def test_in_payload_duplicate_reference_id_is_409_on_both_rows(
        self, client, tickers, customers
    ):
        rows = [
            row("1101", "112001", 10, "SAME"),
            row("1102", "112001", 5, "SAME"),
        ]
        response = client.post(CONFLICT_BATCH_URL, rows)

        assert response.status_code == 409
        assert response.json() == [
            {"reference_id": ["Conflicts with another row in this batch."]},
            {"reference_id": ["Conflicts with another row in this batch."]},
        ]
        assert Order.objects.count() == 0

    def test_existing_reference_id_is_409(self, client, tickers, customers):
        Order.objects.create(
            ticker=tickers[0], customer=customers[0], volume=1, reference_id="TAKEN"
        )
        rows = [row("1102", "112001", 10, "TAKEN")]
        response = client.post(CONFLICT_BATCH_URL, rows)

        assert response.status_code == 409
        assert response.json() == [
            {"reference_id": ["Conflicts with an existing record."]}
        ]
        assert Order.objects.count() == 1

    def test_update_to_a_sibling_current_value_is_409(self, client, tickers, customers):
        o1 = Order.objects.create(
            ticker=tickers[0], customer=customers[0], volume=1, reference_id="A"
        )
        Order.objects.create(
            ticker=tickers[1], customer=customers[0], volume=1, reference_id="B"
        )

        response = client.patch(
            CONFLICT_BATCH_URL, [{"id": o1.pk, "reference_id": "B"}]
        )

        assert response.status_code == 409
        assert response.json() == [
            {"reference_id": ["Conflicts with an existing record."]}
        ]
        o1.refresh_from_db()
        assert o1.reference_id == "A"  # unchanged

    def test_row_keeping_its_own_value_is_not_a_conflict(
        self, client, tickers, customers
    ):
        o1 = Order.objects.create(
            ticker=tickers[0], customer=customers[0], volume=1, reference_id="A"
        )
        Order.objects.create(
            ticker=tickers[1], customer=customers[0], volume=1, reference_id="B"
        )

        # Changing a non-rule field only: reference_id stays "A" (its own).
        response = client.patch(CONFLICT_BATCH_URL, [{"id": o1.pk, "volume": 9}])

        assert response.status_code == 200
        assert response.json() == {"count": 1}
        o1.refresh_from_db()
        assert o1.volume == 9
        assert o1.reference_id == "A"

    def test_update_in_payload_conflict_on_a_new_shared_value(
        self, client, tickers, customers
    ):
        o1 = Order.objects.create(
            ticker=tickers[0], customer=customers[0], volume=1, reference_id="A"
        )
        o2 = Order.objects.create(
            ticker=tickers[1], customer=customers[0], volume=1, reference_id="B"
        )

        # Both rows change to the same *new* reference_id "NEW" (not in the DB,
        # so only the in-payload conflict fires, on both rows).
        response = client.patch(
            CONFLICT_BATCH_URL,
            [
                {"id": o1.pk, "reference_id": "NEW"},
                {"id": o2.pk, "reference_id": "NEW"},
            ],
        )

        assert response.status_code == 409
        assert response.json() == [
            {"reference_id": ["Conflicts with another row in this batch."]},
            {"reference_id": ["Conflicts with another row in this batch."]},
        ]
        o1.refresh_from_db()
        o2.refresh_from_db()
        assert o1.reference_id == "A"  # unchanged
        assert o2.reference_id == "B"  # unchanged


class TestRetry:
    # transaction=True: a retry policy only retries outside an outer
    # transaction (pytest-django's default test transaction is one).
    @pytest.mark.django_db(transaction=True)
    def test_flaky_write_is_retried_and_succeeds(self, client, tickers, customers):
        from tests.apps.demo.views import FlakyRetryPolicy

        FlakyRetryPolicy.instances.clear()
        rows = [row("1101", "112001", 10, "FLAKY_1")]

        response = client.post(FLAKY_BATCH_URL, rows)

        assert response.status_code == 201
        assert response.json() == {"count": 1}
        assert Order.objects.count() == 1
        # First attempt raised OperationalError, second succeeded -> 2 attempts.
        assert len(FlakyRetryPolicy.instances) == 1
        assert FlakyRetryPolicy.instances[0].attempts_made == 2

    def test_inside_an_outer_transaction_the_write_runs_once_and_warns(
        self, user, tickers, customers
    ):
        from tests.apps.demo.views import FlakyRetryPolicy

        FlakyRetryPolicy.instances.clear()
        quiet_client = APIClient(raise_request_exception=False)
        quiet_client.force_authenticate(user=user)

        with pytest.warns(RuntimeWarning, match="outer transaction"):
            response = quiet_client.post(
                FLAKY_BATCH_URL, [row("1101", "112001", 10, "FLAKY_2")]
            )

        assert response.status_code == 500  # the one attempt failed
        assert FlakyRetryPolicy.instances[0].attempts_made == 1


class TestDisabledOperations:
    def test_create_still_works(self, client, tickers, customers):
        rows = [row("1101", "112001", 10, "CO_1")]
        response = client.post(CREATE_ONLY_BATCH_URL, rows)

        assert response.status_code == 201
        assert Order.objects.count() == 1

    @pytest.mark.parametrize("method", ["patch", "delete"])
    def test_disabled_operations_are_405(self, client, method):
        response = getattr(client, method)(CREATE_ONLY_BATCH_URL, [])

        assert response.status_code == 405
