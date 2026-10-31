from __future__ import annotations

from typing import Any

from rest_framework import serializers
from rest_framework.exceptions import ValidationError

from drf_batch import BatchRelatedField, BatchSerializer, BatchServiceField

from .models import Customer, Order, SymbolOrder, Ticker


class OrderSerializer(BatchSerializer):
    """Per-row order payload.

    ticker/customer auto-collect against the related models (the maps are
    named after the fields); no collector class is needed.
    """

    ticker = BatchRelatedField(lookup="isin", error="Ticker '{value}' not found.")
    customer = BatchRelatedField(lookup="isin", error="Customer '{value}' not found.")
    volume = serializers.IntegerField(min_value=1)
    reference_id = serializers.CharField(max_length=64)

    class Meta:
        model = Order
        fields = ("id", "ticker", "customer", "volume", "reference_id")


class ConflictOrderSerializer(OrderSerializer):
    """Order serializer with a cross-row uniqueness rule on reference_id.

    ``batch_unique_together`` declares that ``reference_id`` must be unique
    both within the batch and against existing rows -> 409 conflict pass.
    """

    class Meta(OrderSerializer.Meta):
        batch_unique_together = ("reference_id",)


class RawOrderSerializer(BatchSerializer):
    """Plain FK-by-pk serializer for views without collection."""

    ticker = serializers.PrimaryKeyRelatedField(queryset=Ticker.objects.all())
    customer = serializers.PrimaryKeyRelatedField(queryset=Customer.objects.all())
    volume = serializers.IntegerField(min_value=1)
    reference_id = serializers.CharField(max_length=64)

    class Meta:
        model = Order
        fields = ("id", "ticker", "customer", "volume", "reference_id")


class SymbolOrderSerializer(BatchSerializer):
    """UUID primary-key lookup: symbol auto-collects against Symbol's pk."""

    symbol = BatchRelatedField()
    volume = serializers.IntegerField(min_value=1)
    reference_id = serializers.CharField(max_length=64)

    class Meta:
        model = SymbolOrder
        fields = ("id", "symbol", "volume", "reference_id")


class ExchangeScopedTickerField(BatchRelatedField):
    """DB-after-service: a DB fetch restricted to the service's band 'A'.

    The map only holds tickers the ``band`` service listed on exchange A,
    so a row whose ticker is not band-'A' misses.
    """

    def fetch(
        self, keys: set[Any], collected: dict[str, Any], context: Any
    ) -> dict[Any, Any]:
        bands = collected.get("band", {})
        allowed = {isin for isin, band in bands.items() if band == "A"}
        return Ticker.objects.filter(isin__in=(keys & allowed)).in_bulk(
            field_name="isin"
        )


# In-memory "exchange listing" service, derived from the DB's ticker map:
# ticker name -> band. Tickers without a band are unlisted.
_BANDS = {"Alpha Corp": "A", "Beta Corp": "B"}


def fetch_bands(
    keys: set[Any], collected: dict[str, Any], context: Any
) -> dict[Any, Any]:
    """Service-after-DB: ``{isin: band}`` for the rows' tickers (sent or stored)."""
    tickers = collected.get("ticker", {})
    bands: dict[Any, Any] = {}
    for isin in keys:
        ticker = tickers.get(isin)
        band = _BANDS.get(ticker.name) if ticker is not None else None
        if band is not None:
            bands[isin] = band
    return bands


class ServiceOrderSerializer(BatchSerializer):
    """Service + dependency demo (auto collection, no collector class).

    ``band`` is a service field fetched *after* the ticker DB lookup;
    ``exchange_ticker`` is a DB lookup fetched *after* the service field.
    Both are read-only (the row's value is ignored) and keyed by the row's
    ticker (``derived_from``): the one it sends, or, on a PATCH that omits
    it, the stored one. ``validate()`` reads them via ``self.fetched()``.
    """

    ticker = BatchRelatedField(lookup="isin", error="Ticker '{value}' not found.")
    customer = BatchRelatedField(lookup="isin", error="Customer '{value}' not found.")
    volume = serializers.IntegerField(min_value=1)
    reference_id = serializers.CharField(max_length=64)
    band = BatchServiceField(
        fetch=fetch_bands,
        derived_from="ticker",
        depends_on=("ticker",),
        read_only=True,
    )
    exchange_ticker = ExchangeScopedTickerField(
        model=Ticker,
        lookup="isin",
        derived_from="ticker",
        depends_on=("band",),
        read_only=True,
    )

    class Meta:
        model = Order
        fields = (
            "id",
            "ticker",
            "customer",
            "volume",
            "reference_id",
            "band",
            "exchange_ticker",
        )

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        # Cross-field rules that consume the service results (zero I/O).
        if self.fetched("band") == "B":
            raise ValidationError({"ticker": ["Ticker is suspended on exchange B."]})
        if self.fetched("exchange_ticker") is None:
            raise ValidationError(
                {"exchange_ticker": ["Ticker is not listed on exchange A."]}
            )
        return attrs


class ExternalRefOrderSerializer(OrderSerializer):
    """Writes the nullable ``unique=True`` external_ref: a model-derived rule."""

    external_ref = serializers.CharField(max_length=64, allow_null=True, required=False)

    class Meta(OrderSerializer.Meta):
        fields = (*OrderSerializer.Meta.fields, "external_ref")  # type: ignore[assignment]


class ScopedTickerOrderSerializer(OrderSerializer):
    """The ticker lookup is scoped: only tickers named 'Alpha Corp' resolve."""

    ticker = BatchRelatedField(
        queryset=Ticker.objects.filter(name="Alpha Corp"),
        lookup="isin",
        error="Ticker '{value}' not found.",
    )


class AuditedUpdateOrderSerializer(OrderSerializer):
    """Overrides ``update()``: the per-row (non-bulk) update must call it."""

    def update(self, instance: Any, validated_data: dict[str, Any]) -> Any:
        validated_data["reference_id"] = f"AUDITED-{instance.pk}"
        return super().update(instance, validated_data)
