from __future__ import annotations

from rest_framework.routers import DefaultRouter

from .views import (
    ActionRecordingOrderViewSet,
    AsyncOrderViewSet,
    AsyncSpyOrderViewSet,
    AuditedUpdateOrderViewSet,
    ConflictOrderViewSet,
    CreateOnlyOrderViewSet,
    DataResponseOrderViewSet,
    ExternalRefOrderViewSet,
    FailingAsyncOrderViewSet,
    FlakyOrderViewSet,
    GuardedOrderViewSet,
    NoBulkOrderViewSet,
    OrderViewSet,
    OwnedOrderViewSet,
    RawOrderViewSet,
    ReadWriteOrderViewSet,
    ReplaceOrderViewSet,
    ScopedTickerOrderViewSet,
    ServiceOrderViewSet,
    SlowAsyncOrderViewSet,
    SmallOrderViewSet,
    SpyOrderViewSet,
    SymbolOrderViewSet,
)

router = DefaultRouter()
router.register("orders", OrderViewSet, basename="order")
router.register("raw-orders", RawOrderViewSet, basename="raw-order")
router.register("small-orders", SmallOrderViewSet, basename="small-order")
router.register("spy-orders", SpyOrderViewSet, basename="spy-order")
router.register("no-bulk-orders", NoBulkOrderViewSet, basename="no-bulk-order")
router.register("symbol-orders", SymbolOrderViewSet, basename="symbol-order")
router.register("service-orders", ServiceOrderViewSet, basename="service-order")
router.register("async-orders", AsyncOrderViewSet, basename="async-order")
router.register("async-spy-orders", AsyncSpyOrderViewSet, basename="async-spy-order")
router.register("slow-async-orders", SlowAsyncOrderViewSet, basename="slow-async-order")
router.register(
    "failing-async-orders", FailingAsyncOrderViewSet, basename="failing-async-order"
)
router.register("conflict-orders", ConflictOrderViewSet, basename="conflict-order")
router.register("flaky-orders", FlakyOrderViewSet, basename="flaky-order")
router.register(
    "create-only-orders", CreateOnlyOrderViewSet, basename="create-only-order"
)
router.register("owned-orders", OwnedOrderViewSet, basename="owned-order")
router.register("guarded-orders", GuardedOrderViewSet, basename="guarded-order")
router.register(
    "recording-orders", ActionRecordingOrderViewSet, basename="recording-order"
)
router.register("data-orders", DataResponseOrderViewSet, basename="data-order")
router.register("rw-orders", ReadWriteOrderViewSet, basename="rw-order")
router.register("extref-orders", ExternalRefOrderViewSet, basename="extref-order")
router.register(
    "scoped-ticker-orders", ScopedTickerOrderViewSet, basename="scoped-ticker-order"
)
router.register("audited-orders", AuditedUpdateOrderViewSet, basename="audited-order")

router.register("replace-orders", ReplaceOrderViewSet, basename="replace-order")

urlpatterns = router.urls
