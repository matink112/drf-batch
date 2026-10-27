from __future__ import annotations

from typing import Any, ClassVar

from django.db import OperationalError
from rest_framework.authentication import BasicAuthentication, SessionAuthentication
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.viewsets import GenericViewSet, ReadOnlyModelViewSet

from drf_batch import BatchCollector, BatchMixin, BatchSerializer, FixedRetryPolicy

from .collectors import (
    AsyncSpyCollector,
    FailingAsyncCollector,
    SlowAsyncCollector,
    SpyCollector,
)
from .models import Order, SymbolOrder
from .serializers import (
    AuditedUpdateOrderSerializer,
    ConflictOrderSerializer,
    ExternalRefOrderSerializer,
    OrderSerializer,
    RawOrderSerializer,
    ScopedTickerOrderSerializer,
    ServiceOrderSerializer,
    SymbolOrderSerializer,
)


class FlakyRetryPolicy(FixedRetryPolicy):
    """Test policy: the first attempt fails with ``OperationalError``, then the
    write runs. Records how many times the attempt was made."""

    instances: list[FlakyRetryPolicy] = []

    def __init__(self) -> None:
        super().__init__(attempts=3, delay=0, retry_on=(OperationalError,))
        self.attempts_made = 0
        type(self).instances.append(self)

    def run(self, func: Any) -> Any:
        def flaky() -> Any:
            self.attempts_made += 1
            if self.attempts_made == 1:
                raise OperationalError("simulated transient failure")
            return func()

        return super().run(flaky)


class OrderViewSet(BatchMixin, GenericViewSet):
    """Auto collection: the serializer's cached fields fetch their own lookups."""

    queryset = Order.objects.all()
    batch_serializer_class: type[BatchSerializer] | None = OrderSerializer
    # BasicAuthentication first: in DRF 3.18 a NotAuthenticated is only a
    # 401 when the first authenticator supplies a WWW-Authenticate header;
    # SessionAuthentication's returns None (which coerces it to a 403).
    authentication_classes = [BasicAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]


class RawOrderViewSet(BatchMixin, GenericViewSet):
    """Batch create with no cached fields: ``collected`` stays ``{}``."""

    queryset = Order.objects.all()
    batch_serializer_class: type[BatchSerializer] | None = RawOrderSerializer
    authentication_classes = [SessionAuthentication, BasicAuthentication]
    permission_classes = [IsAuthenticated]


class SmallOrderViewSet(OrderViewSet):
    """Same endpoint with a small ``batch_max_size`` (413 guard demo)."""

    batch_max_size = 2


class SpyOrderViewSet(OrderViewSet):
    """Same endpoint with a replacement collector (test assertions)."""

    batch_collector_class: type[BatchCollector] = SpyCollector


class NoBulkOrderViewSet(OrderViewSet):
    """Same endpoint with the per-row ``child.create`` opt-out."""

    batch_use_bulk = False


class SymbolOrderViewSet(BatchMixin, GenericViewSet):
    """Auto collection with a UUID primary-key lookup."""

    queryset = SymbolOrder.objects.all()
    batch_serializer_class: type[BatchSerializer] | None = SymbolOrderSerializer
    authentication_classes = [BasicAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]


class ServiceOrderViewSet(BatchMixin, GenericViewSet):
    """Auto collection with service + depends_on (service-after-DB, then DB)."""

    queryset = Order.objects.all()
    batch_serializer_class: type[BatchSerializer] | None = ServiceOrderSerializer
    authentication_classes = [BasicAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]


class AsyncOrderViewSet(BatchMixin, GenericViewSet):
    """Batch create with an ``async def collect`` replacement collector."""

    queryset = Order.objects.all()
    batch_serializer_class: type[BatchSerializer] | None = OrderSerializer
    batch_collector_class: type[BatchCollector] = AsyncSpyCollector
    authentication_classes = [BasicAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]


class AsyncSpyOrderViewSet(AsyncOrderViewSet):
    """Async endpoint with a call-recording collector (test assertions)."""

    batch_collector_class = AsyncSpyCollector


class SlowAsyncOrderViewSet(AsyncOrderViewSet):
    """Async endpoint whose collector outlives its timeout (504 demo)."""

    batch_collector_class = SlowAsyncCollector


class FailingAsyncOrderViewSet(AsyncOrderViewSet):
    """Async endpoint whose collector raises (500 demo)."""

    batch_collector_class = FailingAsyncCollector


class ConflictOrderViewSet(OrderViewSet):
    """Endpoint whose serializer declares a cross-row uniqueness rule (409)."""

    queryset = Order.objects.all()
    batch_serializer_class: type[BatchSerializer] | None = ConflictOrderSerializer


class FlakyOrderViewSet(OrderViewSet):
    """Create endpoint with a retry policy: 201 after one simulated failure."""

    queryset = Order.objects.all()
    batch_serializer_class: type[BatchSerializer] | None = OrderSerializer
    batch_retry_policy = FlakyRetryPolicy


class CreateOnlyOrderViewSet(OrderViewSet):
    """Endpoint with only the create operation enabled (PATCH/DELETE -> 405)."""

    batch_actions = ("create",)


class OwnedOrderViewSet(OrderViewSet):
    """Tenant scoping: users only see (and may only batch-modify) their rows.

    The perform hook stamps the owner on create, DRF-style.
    """

    batch_actions = ("create", "update", "replace", "destroy")

    def get_queryset(self) -> Any:
        return Order.objects.filter(owner_id=self.request.user.pk)

    def perform_batch_create(self, serializer: Any) -> None:
        serializer.save(owner=self.request.user)


class RejectVolumeTenObjects(BasePermission):
    """Object permission: orders with volume 10 may not be modified."""

    message = "Orders with volume 10 are frozen."

    def has_object_permission(self, request: Any, view: Any, obj: Any) -> bool:
        return bool(obj.volume != 10)


class GuardedOrderViewSet(OrderViewSet):
    permission_classes = [IsAuthenticated, RejectVolumeTenObjects]
    batch_actions = ("create", "update", "replace", "destroy")


class RecordingPermission(BasePermission):
    """Records ``view.action`` for each request (action-name assertions)."""

    seen: ClassVar[list[str | None]] = []

    def has_permission(self, request: Any, view: Any) -> bool:
        type(self).seen.append(view.action)
        return True


class ActionRecordingOrderViewSet(OrderViewSet):
    permission_classes = [IsAuthenticated, RecordingPermission]
    batch_actions = ("create", "update", "replace", "destroy")


class DataResponseOrderViewSet(OrderViewSet):
    batch_response = "data"


class ReadWriteOrderViewSet(BatchMixin, ReadOnlyModelViewSet):
    """One serializer for the read path and the batch path."""

    queryset = Order.objects.all()
    serializer_class = OrderSerializer
    authentication_classes = [BasicAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]


class ExternalRefOrderViewSet(OrderViewSet):
    batch_serializer_class: type[BatchSerializer] | None = ExternalRefOrderSerializer


class ScopedTickerOrderViewSet(OrderViewSet):
    batch_serializer_class: type[BatchSerializer] | None = ScopedTickerOrderSerializer


class AuditedUpdateOrderViewSet(OrderViewSet):
    batch_serializer_class: type[BatchSerializer] | None = AuditedUpdateOrderSerializer
    batch_use_bulk = False


class ReplaceOrderViewSet(OrderViewSet):
    """PUT enabled; the update hook stamps the owner (PUT reuses it)."""

    batch_actions = ("create", "update", "replace", "destroy")

    def perform_batch_update(self, serializer: Any) -> None:
        serializer.save(owner=self.request.user)
