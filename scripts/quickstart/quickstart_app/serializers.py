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
    """Service-after-DB: ``{isin: band}`` for the rows' tickers (sent or stored)."""
    tickers = collected.get("ticker", {})
    return {
        isin: _LISTINGS[tickers[isin].name]
        for isin in keys
        if isin in tickers and tickers[isin].name in _LISTINGS
    }


class ExchangeScopedTickerField(CachedRelatedField):
    """DB-after-service: only fetch tickers the service listed on exchange A."""

    def fetch(
        self, keys: set[Any], collected: dict[str, Any], context: Any
    ) -> dict[Any, Any]:
        bands = collected.get("band", {})
        allowed = {isin for isin, band in bands.items() if band == "A"}
        return Ticker.objects.filter(isin__in=(keys & allowed)).in_bulk(
            field_name="isin"
        )


class ServiceOrderSerializer(BatchSerializer):
    """Model + service cache fields in one batch (still no collector class).

    Fetch order (topological, via depends_on):
      1. ticker/customer   -> DB, auto (declaration order)
      2. band              -> service, consumes collected["ticker"]
      3. exchange_ticker   -> DB again, scoped to the service's band "A"
    Both are read_only (the row's value is ignored) and keyed by the row's
    ticker (derived_from): the one it sends, or the stored one on a PATCH.
    validate() reads them via self.cached() with zero I/O.
    """

    ticker = CachedRelatedField(lookup="isin", error="Ticker '{value}' not found.")
    customer = CachedRelatedField(lookup="isin", error="Customer '{value}' not found.")
    band = CachedServiceField(
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
        # Rules that consume the service results (zero I/O). self.cached()
        # works on a PATCH that omits ticker too: attrs["ticker"] would not.
        if self.cached("band") == "B":
            raise ValidationError({"ticker": ["Ticker is suspended on exchange B."]})
        if self.cached("exchange_ticker") is None:
            raise ValidationError(
                {"exchange_ticker": ["Ticker is not listed on exchange A."]}
            )
        return attrs
