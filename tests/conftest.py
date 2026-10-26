from __future__ import annotations

import uuid

import pytest
from django.contrib.auth.models import User
from rest_framework.test import APIClient

from tests.apps.demo.models import Customer, Symbol, Ticker

# Fixed UUIDs so tests can reference symbols by stable id in payloads.
SYMBOL_A_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
SYMBOL_B_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")


@pytest.fixture
def user(db) -> User:
    return User.objects.create_user(username="batcher", password="s3cret-pass")


@pytest.fixture
def client(user) -> APIClient:
    api_client = APIClient()
    api_client.force_authenticate(user=user)
    return api_client


@pytest.fixture
def tickers(db) -> list[Ticker]:
    return [
        Ticker.objects.create(isin="1101", name="Alpha Corp"),
        Ticker.objects.create(isin="1102", name="Beta Corp"),
    ]


@pytest.fixture
def customers(db) -> list[Customer]:
    return [Customer.objects.create(isin="112001", name="Acme Trading")]


@pytest.fixture
def symbols(db) -> list[Symbol]:
    return [
        Symbol.objects.create(id=SYMBOL_A_ID, name="Alpha Systems"),
        Symbol.objects.create(id=SYMBOL_B_ID, name="Beta Systems"),
    ]


@pytest.fixture
def valid_rows() -> list[dict[str, object]]:
    return [
        {
            "ticker": "1101",
            "customer": "112001",
            "volume": 10,
            "reference_id": "REF_A_1",
        },
        {
            "ticker": "1102",
            "customer": "112001",
            "volume": 5,
            "reference_id": "REF_A_2",
        },
    ]
