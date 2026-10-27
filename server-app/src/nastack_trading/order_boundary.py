"""Per-call final authorization, invoked after credentials and immediately before HTTP."""

from contextlib import contextmanager
from contextvars import ContextVar

_guard = ContextVar("trading_transmission_guard", default=None)


@contextmanager
def transmission_guard(callback):
    token = _guard.set(callback)
    try:
        yield
    finally:
        _guard.reset(token)


def before_transmission():
    callback = _guard.get()
    if callback:
        callback()
