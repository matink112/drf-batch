from __future__ import annotations

from django.db import models


class Ticker(models.Model):
    isin = models.CharField(max_length=32, unique=True)


class Customer(models.Model):
    isin = models.CharField(max_length=32, unique=True)


class Order(models.Model):
    ticker = models.ForeignKey(Ticker, on_delete=models.CASCADE)
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE)
    volume = models.IntegerField()
