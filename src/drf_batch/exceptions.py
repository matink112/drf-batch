"""Exceptions raised by the batch pipeline."""

from __future__ import annotations

from django.utils.translation import gettext_lazy
from rest_framework import status
from rest_framework.exceptions import APIException


class BatchSizeLimitExceeded(APIException):
    """The batch payload exceeds ``batch_max_size`` (HTTP 413)."""

    status_code = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
    default_detail = gettext_lazy("Batch size exceeds the maximum allowed size.")
    default_code = "batch_size_limit_exceeded"


class BatchCollectionTimeout(APIException):
    """The async collector did not finish within ``COLLECT_TIMEOUT`` (HTTP 504)."""

    status_code = status.HTTP_504_GATEWAY_TIMEOUT
    default_detail = gettext_lazy(
        "Batch collection timed out; the lookup collector did not finish in time."
    )
    default_code = "batch_collection_timeout"


class BatchNotFound(APIException):
    """An update/destroy target pk was not found in the database (HTTP 404).

    The ``detail`` is the index-aligned row list (``{}`` for rows that would
    have applied), matching the batch error format.
    """

    status_code = status.HTTP_404_NOT_FOUND
    default_detail = gettext_lazy("One or more batch targets were not found.")
    default_code = "batch_target_not_found"


class BatchConflict(APIException):
    """A row conflicts with another row in the batch or with existing data (HTTP 409).

    The ``detail`` is the index-aligned row list (``{}`` for rows that would
    have applied), matching the batch error format.
    """

    status_code: int = status.HTTP_409_CONFLICT
    default_detail = gettext_lazy(
        "The batch conflicts with another row in the batch or with existing data."
    )
    default_code = "batch_conflict"


class BatchPermissionDenied(APIException):
    """An update/destroy target fails the view's object permissions (HTTP 403).

    The ``detail`` is the index-aligned row list (``{}`` for rows that would
    have applied), matching the batch error format.
    """

    status_code = status.HTTP_403_FORBIDDEN
    default_detail = gettext_lazy(
        "You do not have permission to modify one or more batch targets."
    )
    default_code = "batch_permission_denied"
