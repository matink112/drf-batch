"""Retry policies for the batch write phase.

A policy is applied **around** the write's ``transaction.atomic()``: the
serializer hands the policy a zero-argument callable that opens a fresh
transaction and performs the bulk/per-row write, so each attempt runs in its
own transaction and a failed attempt's partial work is rolled back before the
next attempt begins.

Subclass :class:`RetryPolicy` and override :meth:`run` for custom behavior;
:class:`FixedRetryPolicy` is the built-in "retry up to N times on these
exceptions" policy. There is no external dependency (no tenacity).
"""

from __future__ import annotations

import time
import warnings
from collections.abc import Callable
from typing import TypeVar

from django.db import DatabaseError, IntegrityError, transaction

_T = TypeVar("_T")


class RetryPolicy:
    """Base class: run the write function once (the identity policy).

    ``run`` receives a zero-argument callable (the atomic write) and must
    return its result; any exception it does not handle propagates.
    """

    def run(self, func: Callable[[], _T]) -> _T:
        return func()


class FixedRetryPolicy(RetryPolicy):
    """Retry the write up to ``attempts`` times on ``retry_on`` exceptions.

    :param attempts: total number of attempts (``>= 1``); the first call is
        attempt 1, so ``attempts=1`` never retries.
    :param delay: seconds to sleep between attempts (``0`` = no sleep).
    :param retry_on: exception types that trigger a retry; any other
        exception propagates immediately.
    :param never_retry: exception types that propagate even when they match
        ``retry_on``. Defaults to ``IntegrityError``: a constraint violation
        is deterministic, and the batch turns it into a 409.

    Inside an outer transaction (``ATOMIC_REQUESTS = True`` or a caller's
    ``atomic()``) each attempt is only a savepoint: a lost connection or a
    serialization failure poisons the whole outer transaction, so retrying
    cannot help. The policy then runs the write **once** and emits a
    ``RuntimeWarning``.

    A caveat: a ``save()`` override (non-bulk mode) with side effects outside
    the database is not rolled back between attempts.
    """

    def __init__(
        self,
        attempts: int = 3,
        delay: float = 0.1,
        retry_on: tuple[type[BaseException], ...] = (DatabaseError,),
        never_retry: tuple[type[BaseException], ...] = (IntegrityError,),
    ) -> None:
        if attempts < 1:
            raise ValueError("attempts must be >= 1")
        self.attempts = attempts
        self.delay = delay
        self.retry_on = tuple(retry_on)
        self.never_retry = tuple(never_retry)

    def run(self, func: Callable[[], _T]) -> _T:
        if transaction.get_connection().in_atomic_block:
            warnings.warn(
                "FixedRetryPolicy: the batch write runs inside an outer "
                "transaction (ATOMIC_REQUESTS?), where a retry cannot recover; "
                "running it once.",
                RuntimeWarning,
                stacklevel=2,
            )
            return func()
        for attempt in range(1, self.attempts + 1):
            try:
                return func()
            except self.retry_on as exc:
                if isinstance(exc, self.never_retry) or attempt == self.attempts:
                    raise
                if self.delay:
                    time.sleep(self.delay)
        raise AssertionError("unreachable")
