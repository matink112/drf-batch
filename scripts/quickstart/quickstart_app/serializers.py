from __future__ import annotations

from rest_framework import serializers

from drf_batch import BatchRelatedField, BatchSerializer

from .models import Order


class OrderSerializer(BatchSerializer):
    ticker = BatchRelatedField(lookup="isin", error="Ticker '{value}' not found.")
    customer = BatchRelatedField(lookup="isin", error="Customer '{value}' not found.")
    volume = serializers.IntegerField(min_value=1)

    class Meta:
        model = Order
        fields = ("id", "ticker", "customer", "volume")
