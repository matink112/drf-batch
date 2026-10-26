from __future__ import annotations

from rest_framework.authentication import BasicAuthentication, SessionAuthentication
from rest_framework.permissions import IsAuthenticated
from rest_framework.viewsets import GenericViewSet

from drf_batch import BatchMixin

from .models import Order
from .serializers import (
    ConflictOrderSerializer,
    OrderSerializer,
    ServiceOrderSerializer,
)


class OrderViewSet(BatchMixin, GenericViewSet):
    queryset = Order.objects.all()
    batch_serializer_class = OrderSerializer
    authentication_classes = [BasicAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]


class ConflictOrderViewSet(BatchMixin, GenericViewSet):
    """Same model; the serializer declares a cross-row uniqueness rule (409)."""

    queryset = Order.objects.all()
    batch_serializer_class = ConflictOrderSerializer
    authentication_classes = [BasicAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]


class ServiceOrderViewSet(BatchMixin, GenericViewSet):
    """Same model, serializer adds mocked-service + scoped-DB cached fields."""

    queryset = Order.objects.all()
    batch_serializer_class = ServiceOrderSerializer
    authentication_classes = [BasicAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]
