from __future__ import annotations

from django.db import models


class Ticker(models.Model):
    isin = models.CharField(max_length=32, unique=True)
    name = models.CharField(max_length=256)


class Customer(models.Model):
    isin = models.CharField(max_length=32, unique=True)
    name = models.CharField(max_length=256)


class Order(models.Model):
    ticker = models.ForeignKey(Ticker, on_delete=models.CASCADE)
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE)
    volume = models.PositiveIntegerField()
    reference_id = models.CharField(max_length=64)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["ticker", "customer"], name="unique_ticker_customer"
            ),
        ]
