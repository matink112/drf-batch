from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models


def _volume_check() -> models.CheckConstraint:
    """CheckConstraint with the 5.x/6.x-compatible constructor call."""
    condition = models.Q(volume__lt=1_000_000)
    name = "volume_below_one_million"
    try:  # Django >= 6.0
        return models.CheckConstraint(condition=condition, name=name)
    except TypeError:  # Django 5.x positional-only first argument
        return models.CheckConstraint(condition, name=name)  # type: ignore[arg-type,call-arg,misc]


class Ticker(models.Model):
    isin = models.CharField(max_length=32, unique=True)
    name = models.CharField(max_length=256)

    def __str__(self) -> str:
        return self.isin


class Customer(models.Model):
    isin = models.CharField(max_length=32, unique=True)
    name = models.CharField(max_length=256)

    def __str__(self) -> str:
        return self.isin


class Symbol(models.Model):
    """UUID primary key: coverage for non-integer (UUID) lookups."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=256)

    def __str__(self) -> str:
        return self.name


class SymbolOrder(models.Model):
    symbol = models.ForeignKey(Symbol, on_delete=models.CASCADE)
    volume = models.PositiveIntegerField()
    reference_id = models.CharField(max_length=64)

    def __str__(self) -> str:
        return f"SymbolOrder({self.symbol},{self.volume})"


class Order(models.Model):
    ticker = models.ForeignKey(Ticker, on_delete=models.CASCADE)
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE)
    volume = models.PositiveIntegerField()
    reference_id = models.CharField(max_length=64)
    # Scoping tests: a viewset whose get_queryset() filters by owner.
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE
    )
    # NULL-distinct uniqueness tests (several rows may have no external ref).
    external_ref = models.CharField(max_length=64, null=True, blank=True, unique=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["ticker", "customer"], name="unique_ticker_customer"
            ),
            # A DB-only rule the serializer does not know about, so a batch
            # can fail at insert time and exercise the all-or-none rollback.
            _volume_check(),
        ]

    def __str__(self) -> str:
        return f"Order({self.ticker_id},{self.customer_id},{self.volume})"


class OrderNote(models.Model):
    """A PROTECT-ed child of Order: deleting a noted order raises
    ``ProtectedError``, mirroring the create-rollback CheckConstraint test."""

    order = models.ForeignKey(Order, on_delete=models.PROTECT)
    note = models.CharField(max_length=256)

    def __str__(self) -> str:
        return f"OrderNote({self.order_id})"
