"""Explicit C1 transaction-boundary policy.

These helpers deliberately contain no provider, callback, verification,
inventory-consumption, refund-execution, or fulfillment behavior.
"""

from contextlib import contextmanager

from django.db import connection, transaction


class ExternalIOInsideTransaction(RuntimeError):
    pass


def assert_external_io_allowed():
    if connection.in_atomic_block:
        raise ExternalIOInsideTransaction("External I/O is forbidden inside a database transaction.")


@contextmanager
def financial_atomic_boundary():
    with transaction.atomic():
        yield
