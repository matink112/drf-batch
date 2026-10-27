"""DRF integration and the REVIEW.md regressions.

Scoping (get_queryset) and object permissions, DRF action names and hooks,
the read path, response modes, model-derived uniqueness, and the write-path
fixes (bulk size setting, grouped bulk_update, fresh targets, retries).
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest import mock

import pytest
from django.db import OperationalError, connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from drf_batch import (
    BatchListSerializer,
    BatchNotFound,
    FixedRetryPolicy,
    loop,
)
from tests.apps.demo.models import Order
from tests.apps.demo.serializers import OrderSerializer
from tests.apps.demo.views import RecordingPermission


def row(ticker: str, customer: str, volume: int, ref: str, **extra: Any) -> dict:
    return {
        "ticker": ticker,
        "customer": customer,
        "volume": volume,
        "reference_id": ref,
        **extra,
    }


def make_order(tickers, customers, *, ticker=0, volume=10, ref="R", owner=None):
    return Order.objects.create(
        ticker=tickers[ticker],
        customer=customers[0],
        volume=volume,
        reference_id=ref,
        owner=owner,
    )


@pytest.fixture
def other_user(db):
    from django.contrib.auth.models import User

    return User.objects.create_user(username="someone-else", password="x")


# ---------------------------------------------------------------------------
# B3: scoping and object permissions
# ---------------------------------------------------------------------------


class TestQuerysetScoping:
    URL = "/owned-orders/batch/"

    def test_create_stamps_owner_through_the_perform_hook(
        self, client, user, tickers, customers
    ):
        response = client.post(self.URL, [row("1101", "112001", 1, "A")])

        assert response.status_code == 201
        assert Order.objects.get().owner == user

    def test_update_outside_the_queryset_is_a_404_and_writes_nothing(
        self, client, user, other_user, tickers, customers
    ):
        mine = make_order(tickers, customers, owner=user, ref="MINE")
        theirs = make_order(tickers, customers, ticker=1, owner=other_user)

        response = client.patch(
            self.URL,
            [{"id": mine.pk, "volume": 5}, {"id": theirs.pk, "volume": 99}],
        )

        assert response.status_code == 404
        body = response.json()
        assert body[0] == {}
        assert list(body[1]) == ["id"]
        mine.refresh_from_db()
        theirs.refresh_from_db()
        assert (mine.volume, theirs.volume) == (10, 10)

    def test_destroy_outside_the_queryset_is_a_404_and_deletes_nothing(
        self, client, other_user, tickers, customers
    ):
        theirs = make_order(tickers, customers, owner=other_user)

        response = client.delete(self.URL, [theirs.pk])

        assert response.status_code == 404
        assert Order.objects.filter(pk=theirs.pk).exists()

    def test_in_scope_update_and_destroy_succeed(
        self, client, user, tickers, customers
    ):
        mine = make_order(tickers, customers, owner=user)

        assert client.patch(self.URL, [{"id": mine.pk, "volume": 7}]).status_code == 200
        mine.refresh_from_db()
        assert mine.volume == 7
        assert client.delete(self.URL, [mine.pk]).status_code == 200
        assert not Order.objects.exists()

    def test_404_wins_over_row_validation_errors(
        self, client, user, tickers, customers
    ):
        # Rows are only validated against targets the caller may modify.
        mine = make_order(tickers, customers, owner=user)

        response = client.patch(
            self.URL, [{"id": mine.pk, "volume": 0}, {"id": 999_999, "volume": 1}]
        )

        assert response.status_code == 404
        assert response.json()[0] == {}


class TestObjectPermissions:
    URL = "/guarded-orders/batch/"

    def test_update_of_a_forbidden_target_is_a_403_index_aligned(
        self, client, tickers, customers
    ):
        frozen = make_order(tickers, customers, volume=10, ref="F")
        open_ = make_order(tickers, customers, ticker=1, volume=20, ref="O")

        response = client.patch(
            self.URL,
            [{"id": open_.pk, "volume": 21}, {"id": frozen.pk, "volume": 11}],
        )

        assert response.status_code == 403
        assert response.json() == [
            {},
            {"non_field_errors": ["Orders with volume 10 are frozen."]},
        ]
        open_.refresh_from_db()
        assert open_.volume == 20  # all-or-none

    def test_destroy_of_a_forbidden_target_is_a_403(self, client, tickers, customers):
        frozen = make_order(tickers, customers, volume=10)

        response = client.delete(self.URL, [frozen.pk])

        assert response.status_code == 403
        assert Order.objects.filter(pk=frozen.pk).exists()


class TestScopedRelatedField:
    URL = "/scoped-ticker-orders/batch/"

    def test_a_ticker_outside_the_field_queryset_is_a_miss(
        self, client, tickers, customers
    ):
        # "1102" is Beta Corp: excluded by the field's queryset.
        response = client.post(
            self.URL, [row("1101", "112001", 1, "A"), row("1102", "112001", 1, "B")]
        )

        assert response.status_code == 400
        assert response.json() == [{}, {"ticker": ["Ticker '1102' not found."]}]


# ---------------------------------------------------------------------------
# D1/D2/D8: DRF action names, hooks, responses
# ---------------------------------------------------------------------------


class TestActions:
    URL = "/recording-orders/batch/"

    def test_each_method_has_its_own_action_name(self, client, tickers, customers):
        order = make_order(tickers, customers)
        RecordingPermission.seen.clear()

        client.post(self.URL, [row("1102", "112001", 1, "N")])
        client.patch(self.URL, [{"id": order.pk, "volume": 3}])
        client.delete(self.URL, [order.pk])

        assert RecordingPermission.seen == [
            "batch_create",
            "batch_update",
            "batch_destroy",
        ]

    def test_route_name_is_stable(self):
        from django.urls import reverse

        assert reverse("order-batch") == "/orders/batch/"


class TestDataResponse:
    URL = "/data-orders/batch/"

    def test_create_returns_serialized_rows(self, client, tickers, customers):
        response = client.post(self.URL, [row("1101", "112001", 4, "D1")])

        assert response.status_code == 201
        order = Order.objects.get()
        assert response.json() == [
            {
                "id": order.pk,
                "ticker": "1101",
                "customer": "112001",
                "volume": 4,
                "reference_id": "D1",
            }
        ]

    def test_destroy_returns_the_deleted_pks(self, client, tickers, customers):
        order = make_order(tickers, customers)

        response = client.delete(self.URL, [order.pk])

        assert response.status_code == 200
        assert response.json() == [order.pk]


# ---------------------------------------------------------------------------
# B4: the batch serializer on the read path
# ---------------------------------------------------------------------------


class TestReadPath:
    def test_list_and_retrieve_render_lookup_values(self, client, tickers, customers):
        order = make_order(tickers, customers, ref="READ")

        listing = client.get("/rw-orders/")
        detail = client.get(f"/rw-orders/{order.pk}/")

        assert listing.status_code == 200
        assert detail.status_code == 200
        assert detail.json()["ticker"] == "1101"
        assert detail.json()["customer"] == "112001"
        assert listing.json() == [detail.json()]

    def test_the_same_viewset_serves_the_batch_route(self, client, tickers, customers):
        response = client.post("/rw-orders/batch/", [row("1101", "112001", 1, "A")])

        assert response.status_code == 201

    def test_many_true_builds_a_batch_list_serializer(self, tickers, customers):
        serializer = OrderSerializer(
            data=[row("1101", "112001", 1, "A")], many=True, context={}
        )

        assert isinstance(serializer, BatchListSerializer)
        assert serializer.is_valid(), serializer.errors
        serializer.save()
        assert Order.objects.count() == 1


# ---------------------------------------------------------------------------
# D5 / C2: model-derived uniqueness, zero per-row I/O, NULLs
# ---------------------------------------------------------------------------


class TestModelUniqueness:
    def test_query_count_does_not_grow_with_the_batch(self, client, tickers, customers):
        counts = []
        for size in (1, 2):
            Order.objects.all().delete()
            rows = [
                row(("1101", "1102")[i], "112001", 1, f"Q{size}{i}")
                for i in range(size)
            ]
            with CaptureQueriesContext(connection) as queries:
                response = client.post("/orders/batch/", rows)
            assert response.status_code == 201
            counts.append(
                sum(q["sql"].startswith("SELECT") for q in queries.captured_queries)
            )

        assert counts[0] == counts[1]

    def test_null_values_never_conflict(self, client, tickers, customers):
        rows = [
            row("1101", "112001", 1, "A", external_ref=None),
            row("1102", "112001", 1, "B", external_ref=None),
        ]

        response = client.post("/extref-orders/batch/", rows)

        assert response.status_code == 201

    def test_duplicate_non_null_unique_values_conflict(
        self, client, tickers, customers
    ):
        rows = [
            row("1101", "112001", 1, "A", external_ref="X"),
            row("1102", "112001", 1, "B", external_ref="X"),
        ]

        response = client.post("/extref-orders/batch/", rows)

        assert response.status_code == 409
        msg = "Conflicts with another row in this batch."
        assert response.json() == [{"external_ref": [msg]}] * 2


# ---------------------------------------------------------------------------
# write path: C1, C3, C4, D6
# ---------------------------------------------------------------------------


class TestWritePath:
    def test_bulk_size_setting_is_honored(self, client, tickers, customers):
        with (
            override_settings(DRF_BATCH={"BULK_SIZE": 1}),
            mock.patch(
                "django.db.models.query.QuerySet.bulk_create",
                autospec=True,
                return_value=[],
            ) as bulk_create,
        ):
            client.post("/orders/batch/", [row("1101", "112001", 1, "A")])

        assert bulk_create.call_args.kwargs["batch_size"] == 1

    def test_bulk_update_never_rewrites_fields_a_row_did_not_send(
        self, client, tickers, customers
    ):
        o1 = make_order(tickers, customers, ref="U1")
        o2 = make_order(tickers, customers, ticker=1, ref="U2")

        with CaptureQueriesContext(connection) as queries:
            response = client.patch(
                "/orders/batch/",
                [{"id": o1.pk, "volume": 5}, {"id": o2.pk, "reference_id": "U2B"}],
            )

        assert response.status_code == 200
        updates = [
            q["sql"] for q in queries.captured_queries if q["sql"].startswith("UPDATE")
        ]
        assert len(updates) == 2
        assert all(("volume" in sql) != ("reference_id" in sql) for sql in updates)

    def test_non_bulk_update_calls_the_serializer_update(
        self, client, tickers, customers
    ):
        order = make_order(tickers, customers)

        response = client.patch(
            "/audited-orders/batch/", [{"id": order.pk, "volume": 3}]
        )

        assert response.status_code == 200
        order.refresh_from_db()
        assert (order.volume, order.reference_id) == (3, f"AUDITED-{order.pk}")

    def test_a_target_deleted_after_validation_is_a_404(self, tickers, customers):
        order = make_order(tickers, customers)
        serializer = BatchListSerializer(
            child=OrderSerializer(context={}),
            data=[{"id": order.pk, "volume": 2}],
            context={},
            operation="update",
        )
        assert serializer.is_valid(), serializer.errors
        Order.objects.filter(pk=order.pk).delete()

        with pytest.raises(BatchNotFound):
            serializer.save()


@pytest.mark.django_db(transaction=True)
def test_non_bulk_destroy_retry_uses_fresh_instances(tickers, customers):
    o1 = make_order(tickers, customers)
    o2 = make_order(tickers, customers, ticker=1)
    real_delete = Order.delete
    calls = {"n": 0}

    def flaky_delete(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:  # attempt 1 fails after deleting o1
            raise OperationalError("transient")
        return real_delete(self, *args, **kwargs)

    serializer = BatchListSerializer(
        child=OrderSerializer(context={}),
        data=[o1.pk, o2.pk],
        context={},
        operation="destroy",
        use_bulk=False,
        retry_policy=FixedRetryPolicy(attempts=2, delay=0),
    )
    assert serializer.is_valid(), serializer.errors
    with mock.patch.object(Order, "delete", flaky_delete):
        serializer.save()

    assert not Order.objects.exists()
    assert calls["n"] == 4  # 2 attempts x 2 rows


# ---------------------------------------------------------------------------
# C5: worker threads manage their connections like a request
# ---------------------------------------------------------------------------


def test_to_thread_applies_request_connection_handling_on_the_worker():
    # Asserted on the calls: Django's SQLite backend never really closes an
    # in-memory test database, so the effect is not observable here.
    import threading

    calls: list[tuple[str, str]] = []

    def fake_close_old_connections() -> None:
        calls.append(("close_old", threading.current_thread().name))

    def work() -> str:
        calls.append(("call", threading.current_thread().name))
        return threading.current_thread().name

    with mock.patch.object(loop, "close_old_connections", fake_close_old_connections):
        worker = asyncio.run(loop.to_thread(work))

    assert worker != threading.current_thread().name
    assert calls == [("close_old", worker), ("call", worker), ("close_old", worker)]


@pytest.mark.parametrize(
    ("method", "body"),
    [
        ("patch", [{"id": 10**30, "volume": 1}]),
        ("delete", [10**30]),
    ],
)
def test_out_of_range_integer_pk_is_a_400(client, method, body):
    response = getattr(client, method)("/orders/batch/", body)

    assert response.status_code == 400
    assert response.json() == [
        {"id": ["Invalid value for 'id': expected a number in range, got int."]}
    ]
