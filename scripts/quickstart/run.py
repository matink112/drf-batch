"""Executable form of the README quickstart (run inside a clean venv)."""

from __future__ import annotations

import importlib
import os
import pkgutil
from pathlib import Path

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "settings")
django.setup()

from django.core.management import call_command  # noqa: E402
from quickstart_app.models import Customer, Order, Ticker  # noqa: E402
from rest_framework.test import APIClient  # noqa: E402

import drf_batch  # noqa: E402

# --- The wheel ships every module -------------------------------------------
# The source tree is not on the path, so a module left out of the wheel fails
# here. contrib.spectacular needs the optional drf-spectacular extra.
package_dir = Path(drf_batch.__file__).parent
assert "site-packages" in package_dir.parts, package_dir
assert (package_dir / "py.typed").is_file(), "py.typed missing from the wheel"
for module in pkgutil.walk_packages(drf_batch.__path__, "drf_batch."):
    if module.name != "drf_batch.contrib.spectacular":
        importlib.import_module(module.name)
for name in drf_batch.__all__:
    assert hasattr(drf_batch, name), name

# --- The README quickstart ---------------------------------------------------
call_command("migrate", run_syncdb=True, verbosity=0)

Ticker.objects.create(isin="1101")
Ticker.objects.create(isin="1102")
Customer.objects.create(isin="112001")

client = APIClient()

# POST creates. An int ISIN is coerced to the CharField lookup's str form.
response = client.post(
    "/orders/batch/",
    [
        {"ticker": "1101", "customer": "112001", "volume": 10},
        {"ticker": 1102, "customer": "112001", "volume": 5},
    ],
    format="json",
)
assert response.status_code == 201, response.content
assert response.json() == {"count": 2}, response.content
assert Order.objects.count() == 2
assert Order.objects.filter(ticker__isin="1102").exists()

# An unknown key -> 400, index-aligned; nothing from the batch is written.
response = client.post(
    "/orders/batch/",
    [
        {"ticker": "1101", "customer": "112001", "volume": 1},
        {"ticker": "XYZ", "customer": "112001", "volume": 1},
    ],
    format="json",
)
assert response.status_code == 400, response.content
assert response.json() == [{}, {"ticker": ["Ticker 'XYZ' not found."]}], (
    response.content
)
assert Order.objects.count() == 2

# PATCH updates by pk, partially: omitted fields are preserved.
first, second = Order.objects.order_by("pk")
response = client.patch(
    "/orders/batch/", [{"id": first.pk, "volume": 11}], format="json"
)
assert response.status_code == 200, response.content
assert response.json() == {"count": 1}, response.content
first.refresh_from_db()
assert first.volume == 11
assert first.ticker.isin == "1101"

# DELETE by pk.
response = client.delete("/orders/batch/", [second.pk], format="json")
assert response.status_code == 200, response.content
assert response.json() == {"count": 1}, response.content
assert list(Order.objects.values_list("pk", flat=True)) == [first.pk]

# The usual single-item routes still work, and render the lookup value.
response = client.get(f"/orders/{first.pk}/")
assert response.status_code == 200, response.content
assert response.json() == {
    "id": first.pk,
    "ticker": "1101",
    "customer": "112001",
    "volume": 11,
}, response.content

print(
    "quickstart OK: every module imports from the wheel; README POST "
    "(int ISIN coerced), 400 index-aligned and all-or-none, PATCH, DELETE "
    "and single-item GET"
)
