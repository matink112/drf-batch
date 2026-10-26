from __future__ import annotations

from quickstart_app.views import ConflictOrderViewSet, OrderViewSet, ServiceOrderViewSet
from rest_framework.routers import DefaultRouter

router = DefaultRouter()
router.register("orders", OrderViewSet, basename="order")
router.register("service-orders", ServiceOrderViewSet, basename="service-order")
router.register("conflict-orders", ConflictOrderViewSet, basename="conflict-order")

urlpatterns = router.urls
