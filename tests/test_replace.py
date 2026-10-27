"""Batch replace: ``PUT /<resource>/batch/`` (full rows, opt-in)."""

from __future__ import annotations

import pytest

from tests.apps.demo.models import Order
from tests.apps.demo.serializers import OrderSerializer

REPLACE_URL = "/replace-orders/batch/"
OWNED_URL = "/owned-orders/batch/"
GUARDED_URL = "/guarded-orders/batch/"


def create_orders(tickers, customers, owner=None):
    o1 = Order.objects.create(
        ticker=tickers[0],
        customer=customers[0],
        volume=10,
        reference_id="R1",
        owner=owner,
    )
    o2 = Order.objects.create(
        ticker=tickers[1],
        customer=customers[0],
        volume=20,
        reference_id="R2",
        owner=owner,
    )
    return o1, o2


def full(pk, ticker="1101", customer="112001", volume=5, reference_id="NEW"):
    return {
        "id": pk,
        "ticker": ticker,
        "customer": customer,
        "volume": volume,
        "reference_id": reference_id,
    }


class TestBatchReplace:
    def test_full_rows_replace_every_field(self, client, tickers, customers):
        o1, o2 = create_orders(tickers, customers)

        response = client.put(
            REPLACE_URL,
            [full(o1.pk, volume=1, reference_id="A"), full(o2.pk, "1102", volume=2)],
        )

        assert response.status_code == 200
        assert response.json() == {"count": 2}
        o1.refresh_from_db()
        o2.refresh_from_db()
        assert (o1.volume, o1.reference_id) == (1, "A")
        assert (o2.ticker, o2.volume, o2.reference_id) == (tickers[1], 2, "NEW")

    def test_missing_required_field_is_an_index_aligned_400(
        self, client, tickers, customers
    ):
        o1, o2 = create_orders(tickers, customers)
        incomplete = {"id": o2.pk, "volume": 7}

        response = client.put(REPLACE_URL, [full(o1.pk, volume=1), incomplete])

        assert response.status_code == 400
        errors = response.json()
        assert errors[0] == {}
        assert set(errors[1]) == {"ticker", "customer", "reference_id"}
        o1.refresh_from_db()
        assert o1.volume == 10  # all-or-none: nothing written

    def test_patch_on_the_same_route_stays_partial(self, client, tickers, customers):
        o1, _ = create_orders(tickers, customers)

        response = client.patch(REPLACE_URL, [{"id": o1.pk, "volume": 3}])

        assert response.status_code == 200
        o1.refresh_from_db()
        assert (o1.volume, o1.reference_id) == (3, "R1")

    def test_replace_reuses_the_update_hook(self, client, user, tickers, customers):
        o1, _ = create_orders(tickers, customers)

        response = client.put(REPLACE_URL, [full(o1.pk)])

        assert response.status_code == 200
        o1.refresh_from_db()
        assert o1.owner == user

    def test_unknown_pk_is_404(self, client, tickers, customers):
        create_orders(tickers, customers)

        response = client.put(REPLACE_URL, [full(999_999)])

        assert response.status_code == 404

    def test_conflict_is_409(self, client, tickers, customers):
        o1, _ = create_orders(tickers, customers)

        # o1 takes o2's (ticker, customer) pair.
        response = client.put(REPLACE_URL, [full(o1.pk, ticker="1102")])

        assert response.status_code == 409
        o1.refresh_from_db()
        assert o1.ticker == tickers[0]

    def test_rows_outside_get_queryset_are_404(self, client, tickers, customers):
        o1, _ = create_orders(tickers, customers)  # no owner: out of scope

        response = client.put(OWNED_URL, [full(o1.pk)])

        assert response.status_code == 404

    def test_object_permissions_apply(self, client, tickers, customers):
        o1, _ = create_orders(tickers, customers)  # volume 10: frozen

        response = client.put(GUARDED_URL, [full(o1.pk)])

        assert response.status_code == 403
        assert response.json() == [
            {"non_field_errors": ["Orders with volume 10 are frozen."]}
        ]

    def test_put_is_405_unless_enabled(self, client, tickers, customers):
        o1, _ = create_orders(tickers, customers)

        response = client.put("/orders/batch/", [full(o1.pk)])

        assert response.status_code == 405


@pytest.mark.django_db
class TestStandalone:
    def test_partial_false_enforces_required_fields(self, tickers, customers):
        o1, _ = create_orders(tickers, customers)

        serializer = OrderSerializer(
            data=[{"id": o1.pk, "volume": 7}],
            many=True,
            operation="update",
            partial=False,
        )

        assert not serializer.is_valid()
        assert "reference_id" in serializer.errors[0]

    def test_update_is_partial_by_default(self, tickers, customers):
        o1, _ = create_orders(tickers, customers)

        serializer = OrderSerializer(
            data=[{"id": o1.pk, "volume": 7}], many=True, operation="update"
        )

        assert serializer.is_valid(), serializer.errors
