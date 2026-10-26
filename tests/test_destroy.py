from __future__ import annotations

from rest_framework.test import APIClient

from tests.apps.demo.models import Order, OrderNote

BATCH_URL = "/orders/batch/"
NO_BULK_BATCH_URL = "/no-bulk-orders/batch/"
SMALL_BATCH_URL = "/small-orders/batch/"


def create_orders(tickers, customers):
    o1 = Order.objects.create(
        ticker=tickers[0], customer=customers[0], volume=10, reference_id="D1"
    )
    o2 = Order.objects.create(
        ticker=tickers[1], customer=customers[0], volume=20, reference_id="D2"
    )
    return o1, o2


class TestBatchDestroy:
    def test_happy_delete(self, client, tickers, customers):
        o1, o2 = create_orders(tickers, customers)

        response = client.delete(BATCH_URL, [o1.pk, o2.pk])

        assert response.status_code == 200
        assert response.json() == {"count": 2}
        assert Order.objects.count() == 0

    def test_mixed_missing_is_404_and_nothing_deleted(self, client, tickers, customers):
        o1, _ = create_orders(tickers, customers)

        response = client.delete(BATCH_URL, [o1.pk, 999_999])

        assert response.status_code == 404
        assert response.json() == [
            {},
            {"id": ["Object with id '999999' not found."]},
        ]
        assert Order.objects.count() == 2  # nothing deleted (both survive)

    def test_malformed_pk_is_400(self, client, tickers, customers):
        response = client.delete(BATCH_URL, ["not-a-number"])

        assert response.status_code == 400
        assert response.json() == [
            {"id": ["Invalid value for 'id': expected a number, got str."]}
        ]

    def test_dict_row_is_400_expected_pk(self, client, tickers, customers):
        response = client.delete(BATCH_URL, [{"id": 1}])

        assert response.status_code == 400
        assert response.json() == [
            {"non_field_errors": ["Invalid row: expected a pk value, got dict."]}
        ]

    def test_none_pk_is_400_required(self, client, tickers, customers):
        response = client.delete(BATCH_URL, [None])

        assert response.status_code == 400
        assert response.json() == [{"id": ["This field is required."]}]

    def test_duplicate_pks_are_409(self, client, tickers, customers):
        o1, _ = create_orders(tickers, customers)

        response = client.delete(BATCH_URL, [o1.pk, o1.pk])

        assert response.status_code == 409
        assert response.json() == [
            {"id": ["Conflicts with another row in this batch."]},
            {"id": ["Conflicts with another row in this batch."]},
        ]
        assert Order.objects.count() == 2  # nothing deleted (both survive)

    def test_empty_list_is_200_count_zero(self, client, tickers, customers):
        response = client.delete(BATCH_URL, [])

        assert response.status_code == 200
        assert response.json() == {"count": 0}

    def test_too_large_batch_is_413(self, client, tickers, customers):
        response = client.delete(SMALL_BATCH_URL, [1, 2, 3])

        assert response.status_code == 413

    def test_protected_delete_rolls_back(self, client, user, tickers, customers):
        # o1 has a PROTECT-ed OrderNote, so deleting it raises ProtectedError
        # (an IntegrityError) -> 409, and the whole batch (including o2)
        # rolls back.
        o1, o2 = create_orders(tickers, customers)
        OrderNote.objects.create(order=o1, note="keep me")
        quiet_client = APIClient(raise_request_exception=False)
        quiet_client.force_authenticate(user=user)

        response = quiet_client.delete(BATCH_URL, [o1.pk, o2.pk])

        assert response.status_code == 409
        assert "database constraint" in response.json()["detail"]
        assert Order.objects.count() == 2  # all-or-none: nothing deleted

    def test_non_bulk_delete_uses_instance_delete(self, client, tickers, customers):
        o1, _ = create_orders(tickers, customers)

        response = client.delete(NO_BULK_BATCH_URL, [o1.pk])

        assert response.status_code == 200
        assert response.json() == {"count": 1}
        assert Order.objects.count() == 1  # o1 deleted, o2 remains
