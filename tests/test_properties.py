"""Property-based tests: arbitrary payload shapes never crash the pipeline.

For any JSON payload on POST/PATCH/DELETE, the response is one of the
documented statuses (never a 500), and a rejected batch writes nothing.
"""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.apps.demo.models import Customer, Order, Ticker

URL = "/orders/batch/"
ALLOWED = {200, 201, 400, 404, 409, 413}

scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),  # unbounded: out-of-range keys must be 400s, not 500s
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=12),
)
json_values = st.recursive(
    scalars,
    lambda children: (
        st.lists(children, max_size=3)
        | st.dictionaries(st.text(max_size=8), children, max_size=3)
    ),
    max_leaves=8,
)
# Rows biased towards the real field names, so validation goes deep.
field_values = st.one_of(
    json_values, st.sampled_from(["1101", "1102", "112001", 1101, "nope"])
)
rows = st.one_of(
    json_values,
    st.fixed_dictionaries(
        {},
        optional={
            "id": field_values,
            "ticker": field_values,
            "customer": field_values,
            "volume": st.one_of(field_values, st.integers(min_value=-5, max_value=5)),
            "reference_id": field_values,
        },
    ),
)
payloads = st.one_of(json_values, st.lists(rows, max_size=6))

common = settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


@pytest.fixture
def seeded(db):
    ticker = Ticker.objects.create(isin="1101", name="Alpha Corp")
    Ticker.objects.create(isin="1102", name="Beta Corp")
    customer = Customer.objects.create(isin="112001", name="Acme")
    Order.objects.create(
        ticker=ticker, customer=customer, volume=1, reference_id="SEED"
    )


def _check(response: Any, before: int) -> None:
    assert response.status_code in ALLOWED, response.content
    if response.status_code >= 400:
        assert Order.objects.count() == before


@common
@given(payload=payloads)
def test_post_never_crashes(client, seeded, payload):
    before = Order.objects.count()
    _check(client.post(URL, payload, format="json"), before)


@common
@given(payload=payloads)
def test_patch_never_crashes(client, seeded, payload):
    before = Order.objects.count()
    _check(client.patch(URL, payload, format="json"), before)


@common
@given(payload=st.one_of(payloads, st.lists(scalars, max_size=6)))
def test_delete_never_crashes(client, seeded, payload):
    before = Order.objects.count()
    _check(client.delete(URL, payload, format="json"), before)
