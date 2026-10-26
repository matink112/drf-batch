"""Executable form of the README quickstart (run inside a clean venv)."""

from __future__ import annotations

import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "settings")
django.setup()

from django.contrib.auth.models import User  # noqa: E402
from django.core.management import call_command  # noqa: E402
from quickstart_app.models import Customer, Order, Ticker  # noqa: E402
from rest_framework.test import APIClient  # noqa: E402

call_command("migrate", run_syncdb=True, verbosity=0)

Ticker.objects.create(isin="1101", name="Alpha Corp")
Ticker.objects.create(isin="1102", name="Beta Corp")
Ticker.objects.create(isin="1103", name="Gamma Corp")
Customer.objects.create(isin="112001", name="Acme Trading")
Customer.objects.create(isin="112002", name="Beta Trading")
Customer.objects.create(isin="112003", name="Gamma Trading")

client = APIClient()
client.force_authenticate(user=User.objects.create_user("demo", password="demo-pass"))

ok_rows = [
    {"ticker": "1101", "customer": "112001", "volume": 10, "reference_id": "REF-1"},
    {"ticker": "1102", "customer": "112001", "volume": 5, "reference_id": "REF-2"},
]
response = client.post("/orders/batch/", ok_rows, format="json")
assert response.status_code == 201, response.content
assert response.json() == {"count": 2}, response.content
assert Order.objects.count() == 2

# An int ISIN is coerced to its str form for the CharField lookup. A fresh
# customer keeps the (ticker, customer) unique constraint happy.
coerce_rows = [
    {"ticker": 1101, "customer": "112002", "volume": 7, "reference_id": "REF-3"},
]
response = client.post("/orders/batch/", coerce_rows, format="json")
assert response.status_code == 201, response.content
assert response.json() == {"count": 1}, response.content
assert Order.objects.get(reference_id="REF-3").ticker.isin == "1101"
assert Order.objects.count() == 3

bad_rows = [
    {"ticker": "1103", "customer": "112001", "volume": 10, "reference_id": "REF-5"},
    {"ticker": "XYZ", "customer": "112001", "volume": 10, "reference_id": "REF-6"},
]
response = client.post("/orders/batch/", bad_rows, format="json")
assert response.status_code == 400, response.content
assert response.json() == [
    {},
    {"ticker": ["Ticker 'XYZ' not found."]},
], response.content
assert Order.objects.count() == 3  # nothing from the rejected batch

# --- Service cache field + model cache fields, together --------------------
# /service-orders/batch/ uses ServiceOrderSerializer:
#   band (CachedServiceField, mocked fetch, depends_on=ticker) runs AFTER the
#   ticker DB lookup and builds {band: [isins]} from its result;
#   exchange_ticker (subclass of CachedRelatedField, depends_on=band) runs a
#   second DB query scoped to the service's band "A";
#   validate() then consumes the service map with zero I/O.

# Happy path: a row may assert the band and the scoped ticker itself; both
# hit their maps, and the unlisted 1103 simply has no band (still orderable).
svc_ok_rows = [
    {
        "ticker": "1101",
        "customer": "112003",
        "band": "A",
        "exchange_ticker": "1101",
        "volume": 3,
        "reference_id": "SVC-1",
    },
    {"ticker": "1103", "customer": "112003", "volume": 4, "reference_id": "SVC-2"},
]
response = client.post("/service-orders/batch/", svc_ok_rows, format="json")
assert response.status_code == 201, response.content
assert response.json() == {"count": 2}, response.content
assert Order.objects.count() == 5

# validate() sees the service result: band "B" tickers are suspended.
response = client.post(
    "/service-orders/batch/",
    [
        {
            "ticker": "1102",
            "customer": "112003",
            "volume": 3,
            "reference_id": "SVC-3",
        }
    ],
    format="json",
)
assert response.status_code == 400, response.content
assert response.json() == [{"ticker": ["Ticker is suspended on exchange B."]}], (
    response.content
)

# A row-asserted band the service map does not list -> row error. (The pair
# stays unique: DRF's ModelSerializer also runs the model's unique
# constraint validators during row validation.)
response = client.post(
    "/service-orders/batch/",
    [
        {
            "ticker": "1103",
            "customer": "112001",
            "band": "C",
            "volume": 3,
            "reference_id": "SVC-4",
        }
    ],
    format="json",
)
assert response.status_code == 400, response.content
assert response.json() == [{"band": ["Object 'C' not found."]}], response.content

# A row-asserted exchange_ticker the scoped DB fetch did not return: the
# band map holds no band "A" for this batch, so the scoped query finds
# nothing and "1102" misses (with the field's own error template).
response = client.post(
    "/service-orders/batch/",
    [
        {
            "ticker": "1103",
            "customer": "112002",
            "exchange_ticker": "1102",
            "volume": 3,
            "reference_id": "SVC-5",
        }
    ],
    format="json",
)
assert response.status_code == 400, response.content
assert response.json() == [
    {"exchange_ticker": ["Ticker '1102' is not listed on exchange A."]}
], response.content
assert Order.objects.count() == 5  # nothing from the rejected batches

# --- Batch update: PATCH by pk, partial --------------------------------------
order1 = Order.objects.get(reference_id="REF-1")
response = client.patch(
    "/orders/batch/",
    [{"id": order1.pk, "volume": 11}],
    format="json",
)
assert response.status_code == 200, response.content
assert response.json() == {"count": 1}, response.content
order1.refresh_from_db()
assert order1.volume == 11
# Omitted fields are preserved.
assert order1.ticker.isin == "1101"
assert order1.customer.isin == "112001"
assert order1.reference_id == "REF-1"

# A missing target pk -> 404, index-aligned; the valid row is not written
# either (all-or-none negotiation).
response = client.patch(
    "/orders/batch/",
    [{"id": order1.pk, "volume": 12}, {"id": 999_999, "volume": 13}],
    format="json",
)
assert response.status_code == 404, response.content
assert response.json() == [
    {},
    {"id": ["Object with id '999999' not found."]},
], response.content
order1.refresh_from_db()
assert order1.volume == 11

# --- Batch delete: DELETE by pk -----------------------------------------------
order2 = Order.objects.get(reference_id="REF-2")
response = client.delete("/orders/batch/", [order2.pk], format="json")
assert response.status_code == 200, response.content
assert response.json() == {"count": 1}, response.content
assert Order.objects.count() == 4

# A missing pk -> 404, index-aligned; nothing is deleted.
response = client.delete("/orders/batch/", [order1.pk, 999_999], format="json")
assert response.status_code == 404, response.content
assert response.json() == [
    {},
    {"id": ["Object with id '999999' not found."]},
], response.content
assert Order.objects.count() == 4

# --- Cross-row conflicts: 409 --------------------------------------------------
# Two rows sharing a reference_id -> 409 on both rows, nothing written.
# (The pairs are free so the model's (ticker, customer) constraint stays
# quiet and the 409 conflict pass is what fires.)
response = client.post(
    "/conflict-orders/batch/",
    [
        {"ticker": "1102", "customer": "112002", "volume": 1, "reference_id": "DUP"},
        {"ticker": "1103", "customer": "112001", "volume": 2, "reference_id": "DUP"},
    ],
    format="json",
)
assert response.status_code == 409, response.content
assert response.json() == [
    {"reference_id": ["Conflicts with another row in this batch."]},
    {"reference_id": ["Conflicts with another row in this batch."]},
], response.content
assert Order.objects.count() == 4  # nothing from the conflicting batch

# A reference_id an existing row already holds -> 409 as well.
response = client.post(
    "/conflict-orders/batch/",
    [{"ticker": "1102", "customer": "112001", "volume": 1, "reference_id": "REF-1"}],
    format="json",
)
assert response.status_code == 409, response.content
assert response.json() == [{"reference_id": ["Conflicts with an existing record."]}], (
    response.content
)
assert Order.objects.count() == 4

print(
    "quickstart OK: auto collection (no collector); int ISIN coerced; "
    "400 index-aligned error; all-or-none holds; "
    "mocked service + model cache fields (both dependency directions); "
    "partial PATCH by pk (404 on missing target); DELETE by pk "
    "(404 on missing pk); 409 in-payload + existing-record conflicts"
)
