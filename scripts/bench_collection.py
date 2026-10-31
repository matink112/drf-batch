"""Benchmark collection latency with and without a surrounding transaction.

Builds batch serializers with N sync batch fields; each fetch runs one ORM
query and then sleeps for a simulated round-trip (``--latency`` ms, standing
in for a remote database or a sync service client). Each case is timed for
``is_valid()`` (collection + validation, no write):

- outside ``atomic()``: sync fetches run concurrently on worker threads
- inside ``atomic()`` (as under ``ATOMIC_REQUESTS``): they run on the
  request thread, one at a time

Usage::

    uv run python scripts/bench_collection.py [--fields 1 2 4 8] [--latency 5 20]
"""

from __future__ import annotations

import argparse
import statistics
import sys
import tempfile
import time
from pathlib import Path

import django
from django.conf import settings

REPO_ROOT = Path(__file__).resolve().parent.parent
ROWS = 100
REPEATS = 5


def _configure(db_path: str) -> None:
    sys.path.insert(0, str(REPO_ROOT))
    settings.configure(
        SECRET_KEY="bench",
        INSTALLED_APPS=[
            "django.contrib.auth",
            "django.contrib.contenttypes",
            "rest_framework",
            "drf_batch",
            "tests.apps.demo",
        ],
        # A file database: worker threads open their own connections to it.
        DATABASES={
            "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": db_path}
        },
        USE_TZ=True,
        DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
    )
    django.setup()


def _make_serializer(n_fields: int, latency: float) -> type:
    from rest_framework import serializers
    from tests.apps.demo.models import Ticker

    from drf_batch import BatchSerializer, BatchServiceField

    def fetch(keys: set[str], collected: dict, context: dict) -> dict[str, str]:
        rows = Ticker.objects.filter(isin__in=keys).values_list("isin", "name")
        names = dict(rows)
        time.sleep(latency)
        return names

    attrs: dict = {
        "isin": serializers.CharField(),
        "name": serializers.CharField(),
    }
    for i in range(n_fields):
        attrs[f"f{i}"] = BatchServiceField(fetch=fetch, read_only=True)
    meta = {"model": Ticker, "fields": tuple(attrs)}
    attrs["Meta"] = type("Meta", (), meta)
    return type(f"Bench{n_fields}Serializer", (BatchSerializer,), attrs)


def _time(serializer_class: type, rows: list[dict], *, in_atomic: bool) -> float:
    from django.db import transaction

    samples = []
    for _ in range(REPEATS):
        start = time.perf_counter()
        if in_atomic:
            with transaction.atomic():
                serializer = serializer_class(data=rows, many=True)
                serializer.is_valid(raise_exception=True)
        else:
            serializer = serializer_class(data=rows, many=True)
            serializer.is_valid(raise_exception=True)
        samples.append(time.perf_counter() - start)
    return statistics.median(samples) * 1000


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fields", type=int, nargs="+", default=[1, 2, 4, 8])
    parser.add_argument("--latency", type=float, nargs="+", default=[1, 5, 20])
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        _configure(str(Path(tmp) / "bench.sqlite3"))
        from django.core.management import call_command
        from tests.apps.demo.models import Ticker

        call_command("migrate", verbosity=0, run_syncdb=True)
        Ticker.objects.bulk_create(
            Ticker(isin=f"X{i}", name=f"T{i}") for i in range(ROWS)
        )

        print(f"{ROWS} rows, median of {REPEATS} runs, collection + validation\n")
        header = ("fields", "latency", "no atomic", "atomic", "ratio")
        print("{:>6} {:>8} {:>10} {:>10} {:>6}".format(*header))
        for latency_ms in args.latency:
            for n_fields in args.fields:
                serializer_class = _make_serializer(n_fields, latency_ms / 1000)
                rows = [
                    {"isin": f"N{i}", "name": "n"}
                    | {f"f{j}": f"X{i}" for j in range(n_fields)}
                    for i in range(ROWS)
                ]
                _time(serializer_class, rows, in_atomic=False)  # warm-up
                free = _time(serializer_class, rows, in_atomic=False)
                atomic = _time(serializer_class, rows, in_atomic=True)
                print(
                    f"{n_fields:>6} {latency_ms:>6.0f}ms {free:>8.1f}ms "
                    f"{atomic:>8.1f}ms {atomic / free:>5.1f}x"
                )


if __name__ == "__main__":
    main()
