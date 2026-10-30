from __future__ import annotations

from rest_framework import serializers

from drf_batch import BatchSerializer, CachedRelatedField

from .models import Order


class OrderSerializer(BatchSerializer):
    ticker = CachedRelatedField(lookup="isin", error="Ticker '{value}' not found.")
    customer = CachedRelatedField(lookup="isin", error="Customer '{value}' not found.")
    volume = serializers.IntegerField(min_value=1)

    class Meta:
        model = Order
        fields = ("id", "ticker", "customer", "volume")
