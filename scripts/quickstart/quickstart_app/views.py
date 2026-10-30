from __future__ import annotations

from rest_framework.viewsets import ModelViewSet

from drf_batch import BatchMixin

from .models import Order
from .serializers import OrderSerializer


class OrderViewSet(BatchMixin, ModelViewSet):
    queryset = Order.objects.all()
    serializer_class = OrderSerializer
