from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import IntEnum


class LockRank(IntEnum):
    CART = 10
    CHECKOUT = 20
    PAYABLE = 30
    PAYMENT = 40
    PAYMENT_ATTEMPT = 50
    PAYMENT_TRANSACTION = 60
    COMMERCIAL_LINE = 70
    COMMERCIAL_RESOURCE = 80
    RESERVATION = 90
    FULFILLMENT = 100
    JOURNAL_ACCOUNT = 110
    JOURNAL_RECORD = 120
    REVIEW_CASE = 130
    EVENT_OUTBOX = 140


class LockOrderViolation(RuntimeError):
    pass


@dataclass(frozen=True, order=True)
class LockKey:
    rank: int
    stable_key: str


_lock_history = ContextVar("financial_core_lock_history", default=None)


@contextmanager
def ordered_lock_scope():
    token = _lock_history.set([])
    try:
        yield
    finally:
        _lock_history.reset(token)


def register_lock(rank, stable_key):
    history = _lock_history.get()
    if history is None:
        raise LockOrderViolation("Financial row locks require ordered_lock_scope().")
    key = LockKey(int(rank), str(stable_key))
    if history and key < history[-1]:
        raise LockOrderViolation(
            f"Lock order violation: requested {key.rank}:{key.stable_key} "
            f"after {history[-1].rank}:{history[-1].stable_key}."
        )
    history.append(key)
    return key


def lock_one(*, queryset, rank, pk):
    register_lock(rank, f"{int(pk):020d}")
    return queryset.select_for_update().get(pk=pk)


def lock_many(*, queryset, rank, pks):
    ordered_ids = sorted({int(pk) for pk in pks})
    for pk in ordered_ids:
        register_lock(rank, f"{pk:020d}")
    rows = list(queryset.select_for_update().filter(pk__in=ordered_ids).order_by("pk"))
    if [row.pk for row in rows] != ordered_ids:
        raise queryset.model.DoesNotExist("One or more requested lock rows do not exist.")
    return rows
