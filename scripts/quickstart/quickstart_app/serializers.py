from __future__ import annotations

from typing import Any

from rest_framework import serializers
from rest_framework.exceptions import ValidationError

from drf_batch import BatchSerializer, CachedRelatedField, CachedServiceField

from .models import Order, Ticker


class OrderSerializer(BatchSerializer):
    # No collector class: the cached fields auto-collect against the related
    # models (maps are named after the fields), and row ISINs are coerced to
    # the lookup's type (str/int/... -> str for a CharField lookup).
    ticker = CachedRelatedField(lookup="isin", error="Ticker '{value}' not found.")
    customer = CachedRelatedField(lookup="isin", error="Customer '{value}' not found.")
    volume = serializers.IntegerField(min_value=1)
    reference_id = serializers.CharField(max_length=64)

    class Meta:
        model = Order
        fields = ("id", "ticker", "customer", "volume", "reference_id")


class ConflictOrderSerializer(OrderSerializer):
    """Order serializer with a cross-row uniqueness rule on reference_id.

    ``batch_unique_together`` makes the pipeline reject duplicate
    reference_ids within the payload and clashes with existing rows as a
    409 (index-aligned) before any write.
    """

    class Meta(OrderSerializer.Meta):
        batch_unique_together = ("reference_id",)


# --- Service cache fields together with model cache fields -----------------
#
# A mocked third-party "exchange listing" service. In a real app the fetch
# would be an HTTP/gRPC call; a dict plays the external system.
_LISTINGS: dict[str, str] = {"Alpha Corp": "A", "Beta Corp": "B"}


def fetch_bands(
    keys: set[Any], collected: dict[str, Any], context: Any
) -> dict[Any, Any]:
    """Service-after-DB: build the {band: [isins]} map from the ticker query."""
    bands: dict[Any, Any] = {}
    for isin, ticker in collected.get("ticker", {}).items():
        band = _LISTINGS.get(ticker.name)
        if band is not None:
            bands.setdefault(band, []).append(isin)
    return bands


class ExchangeScopedTickerField(CachedRelatedField):
    """DB-after-service: only fetch tickers the service listed on exchange A."""

    def fetch(
        self, keys: set[Any], collected: dict[str, Any], context: Any
    ) -> dict[Any, Any]:
        allowed = set(collected.get("band", {}).get("A", ()))
        return Ticker.objects.filter(isin__in=(keys & allowed)).in_bulk(
            field_name="isin"
        )


class ServiceOrderSerializer(BatchSerializer):
    """Model + service cache fields in one batch (still no collector class).

    Fetch order (topological, via depends_on):
      1. ticker/customer   -> DB, auto (declaration order)
      2. band              -> service, consumes collected["ticker"]
      3. exchange_ticker   -> DB again, scoped to the service's band "A"
    Both service-backed fields are read_only: the row may optionally supply
    their value (checked against the map), and validate() reads the maps via
    self.collected with zero I/O.
    """

    ticker = CachedRelatedField(lookup="isin", error="Ticker '{value}' not found.")
    customer = CachedRelatedField(lookup="isin", error="Customer '{value}' not found.")
    band = CachedServiceField(fetch=fetch_bands, depends_on=("ticker",), read_only=True)
    exchange_ticker = ExchangeScopedTickerField(
        model=Ticker,
        lookup="isin",
        depends_on=("band",),
        read_only=True,
        error="Ticker '{value}' is not listed on exchange A.",
    )
    volume = serializers.IntegerField(min_value=1)
    reference_id = serializers.CharField(max_length=64)

    class Meta:
        model = Order
        fields = (
            "id",
            "ticker",
            "customer",
            "band",
            "exchange_ticker",
            "volume",
            "reference_id",
        )

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        # Cross-row rule that consumes the service result (zero I/O):
        # band "B" tickers are suspended.
        suspended = self.collected.get("band", {}).get("B", [])
        if attrs["ticker"].isin in suspended:
            raise ValidationError({"ticker": ["Ticker is suspended on exchange B."]})
        return attrs
