from __future__ import annotations

from rest_framework.test import APIClient

from tests.apps.demo.models import Order, SymbolOrder

BATCH_URL = "/orders/batch/"
NO_BULK_BATCH_URL = "/no-bulk-orders/batch/"
SMALL_BATCH_URL = "/small-orders/batch/"
SYMBOL_BATCH_URL = "/symbol-orders/batch/"


def create_orders(tickers, customers):
    o1 = Order.objects.create(
        ticker=tickers[0], customer=customers[0], volume=10, reference_id="U1"
    )
    o2 = Order.objects.create(
        ticker=tickers[1], customer=customers[0], volume=20, reference_id="U2"
    )
    return o1, o2


class TestBatchUpdate:
    def test_happy_partial_update_changes_only_supplied_fields(
        self, client, tickers, customers
    ):
        o1, _ = create_orders(tickers, customers)

        response = client.patch(BATCH_URL, [{"id": o1.pk, "volume": 99}])

        assert response.status_code == 200
        assert response.json() == {"count": 1}
        o1.refresh_from_db()
        assert o1.volume == 99
        # Omitted fields are preserved.
        assert o1.ticker == tickers[0]
        assert o1.customer == customers[0]
        assert o1.reference_id == "U1"

    def test_multi_row_update(self, client, tickers, customers):
        o1, o2 = create_orders(tickers, customers)

        response = client.patch(
            BATCH_URL,
            [
                {"id": o1.pk, "volume": 1},
                {"id": o2.pk, "volume": 2, "reference_id": "U2B"},
            ],
        )

        assert response.status_code == 200
        assert response.json() == {"count": 2}
        o1.refresh_from_db()
        o2.refresh_from_db()
        assert o1.volume == 1
        assert o2.volume == 2
        assert o2.reference_id == "U2B"
        # o2.ticker preserved.
        assert o2.ticker == tickers[1]

    def test_update_can_change_a_cached_field(self, client, tickers, customers):
        o1, o2 = create_orders(tickers, customers)
        # o2 holds (tickers[1], customers[0]) — the pair the update would take
        # — so remove it to keep the (ticker, customer) constraint intact.
        o2.delete()

        response = client.patch(BATCH_URL, [{"id": o1.pk, "ticker": "1102"}])

        assert response.status_code == 200
        assert response.json() == {"count": 1}
        o1.refresh_from_db()
        assert o1.ticker == tickers[1]

    def test_row_with_only_id_is_a_valid_noop(self, client, tickers, customers):
        o1, _ = create_orders(tickers, customers)

        response = client.patch(BATCH_URL, [{"id": o1.pk}])

        assert response.status_code == 200
        assert response.json() == {"count": 1}
        o1.refresh_from_db()
        assert o1.volume == 10  # unchanged

    def test_missing_target_is_404_index_aligned(self, client, tickers, customers):
        o1, _ = create_orders(tickers, customers)

        response = client.patch(
            BATCH_URL,
            [
                {"id": o1.pk, "volume": 5},
                {"id": 999_999, "volume": 6},
            ],
        )

        assert response.status_code == 404
        assert response.json() == [
            {},
            {"id": ["Object with id '999999' not found."]},
        ]
        o1.refresh_from_db()
        assert o1.volume == 10  # nothing changed

    def test_missing_pk_is_400_required(self, client, tickers, customers):
        response = client.patch(BATCH_URL, [{"volume": 5}])

        assert response.status_code == 400
        assert response.json() == [{"id": ["This field is required."]}]

    def test_malformed_pk_is_400_invalid_key_and_no_collection(
        self, client, tickers, customers
    ):
        response = client.patch(BATCH_URL, [{"id": "not-a-number", "volume": 5}])

        assert response.status_code == 400
        assert response.json() == [
            {"id": ["Invalid value for 'id': expected a number, got str."]}
        ]

    def test_empty_list_is_200_count_zero(self, client, tickers, customers):
        response = client.patch(BATCH_URL, [])

        assert response.status_code == 200
        assert response.json() == {"count": 0}

    def test_too_large_batch_is_413(self, client, tickers, customers):
        response = client.patch(
            SMALL_BATCH_URL,
            [
                {"id": 1, "volume": 1},
                {"id": 2, "volume": 2},
                {"id": 3, "volume": 3},
            ],
        )

        assert response.status_code == 413

    def test_db_error_rolls_back_the_whole_batch(
        self, client, user, tickers, customers
    ):
        o1, o2 = create_orders(tickers, customers)
        quiet_client = APIClient(raise_request_exception=False)
        quiet_client.force_authenticate(user=user)

        response = quiet_client.patch(
            BATCH_URL,
            [
                {"id": o1.pk, "volume": 2_000_000},  # violates CheckConstraint
                {"id": o2.pk, "volume": 50},
            ],
        )

        assert response.status_code == 409
        o1.refresh_from_db()
        o2.refresh_from_db()
        assert o1.volume == 10  # rolled back
        assert o2.volume == 20  # rolled back

    def test_non_bulk_update_uses_save_update_fields(self, client, tickers, customers):
        o1, _ = create_orders(tickers, customers)

        response = client.patch(NO_BULK_BATCH_URL, [{"id": o1.pk, "volume": 77}])

        assert response.status_code == 200
        assert response.json() == {"count": 1}
        o1.refresh_from_db()
        assert o1.volume == 77


class TestUuidPkUpdate:
    # SymbolOrder's own pk is an auto int; the UUID pk is Symbol's, looked up
    # via the ``symbol`` cached field (str -> UUID coercion, like create).

    def test_uuid_pk_update(self, client, symbols):
        symbol_a = symbols[0]
        order = SymbolOrder.objects.create(
            symbol=symbol_a, volume=3, reference_id="UU1"
        )

        response = client.patch(
            SYMBOL_BATCH_URL,
            [{"id": str(order.pk), "symbol": str(symbol_a.id), "volume": 8}],
        )

        assert response.status_code == 200
        assert response.json() == {"count": 1}
        order.refresh_from_db()
        assert order.volume == 8
        assert order.symbol == symbol_a

    def test_uuid_pk_missing_target_is_404(self, client, symbols):
        response = client.patch(
            SYMBOL_BATCH_URL,
            [{"id": 999_999, "symbol": str(symbols[0].id), "volume": 8}],
        )

        assert response.status_code == 404
        assert response.json()[0]["id"][0].startswith("Object with id '")

    def test_malformed_uuid_pk_is_400(self, client, symbols):
        response = client.patch(
            SYMBOL_BATCH_URL, [{"id": 1, "symbol": "nope", "volume": 8}]
        )

        assert response.status_code == 400
        assert response.json() == [
            {"symbol": ["Invalid value for 'symbol': expected a UUID, got str."]}
        ]
