from __future__ import annotations

from quickstart_app.views import OrderViewSet
from rest_framework.routers import DefaultRouter

router = DefaultRouter()
router.register("orders", OrderViewSet, basename="order")
urlpatterns = router.urls
