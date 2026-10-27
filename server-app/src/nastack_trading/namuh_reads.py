"""Process-local pacing for owner-scoped NH reads, not an order retry queue.

All read adapters in this server share the same priority gate. Bounded, owner-private
read reuse is permitted only for display/monitor contexts. Critical checks never reuse
responses. Credentials are not cached. This is not a claim about NH's contractual quota.
Each additional server process would need a shared/distributed gate and invalidation.
"""

import copy
import hashlib
import json
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

from fastapi import HTTPException

from .db import now

_lane = ContextVar("namuh_read_lane", default="critical")


@contextmanager
def read_lane(lane):
    token = _lane.set(lane)
    try:
        yield
    finally:
        _lane.reset(token)


def critical_reads(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with read_lane("critical"):
            return function(*args, **kwargs)

    return wrapped


class NamuhReadDeferred(HTTPException):
    def __init__(self, seconds=30, reason="cooldown"):
        super().__init__(
            429,
            ("나무증권 조회 순서 대기" if reason == "queue" else "나무증권 조회 호출 제한")
            + " · 잠시 후 재점검 · 이번 주기 매매 판단 보류",
            headers={
                "Retry-After": str(seconds),
                "X-Trading-Error": "NAMUH_READ_THROTTLED",
                "X-Trading-Defer-Reason": reason,
            },
        )


class ReadGate:
    def __init__(self, interval=1.05, clock=time.monotonic, sleep=time.sleep):
        self.interval, self.clock, self.sleep = interval, clock, sleep
        self.lock = threading.Lock()
        self.condition = threading.Condition(self.lock)
        self.waiters = []
        self.sequence = 0
        self.busy = False
        self.cache_lock = threading.Lock()
        self.cache = {}
        self.inflight = {}
        self.epoch = 0
        self.next_at = self.cooldown_until = 0.0

    def acquire(self):
        # Orders/fills precede monitor reads, which precede display and collection.
        priority = {"critical": 0, "monitor": 1, "display": 2, "background": 3}[_lane.get()]
        deadline = time.monotonic() + (8 if priority == 0 else 4 if priority == 1 else 2)
        with self.condition:
            self.sequence += 1
            ticket = (priority, self.sequence)
            self.waiters.append(ticket)
            try:
                while self.busy or ticket != min(self.waiters):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise NamuhReadDeferred(2, "queue")
                    self.condition.wait(remaining)
                stamp = self.clock()
                if self.cooldown_until > stamp:
                    raise NamuhReadDeferred(max(1, int(self.cooldown_until - stamp) + 1))
                self.busy = True
            finally:
                self.waiters.remove(ticket)
                self.condition.notify_all()
        try:
            delay = max(0, self.next_at - self.clock())
            if delay:
                self.sleep(delay)
            self.next_at = self.clock() + self.interval
        finally:
            with self.condition:
                self.busy = False
                self.condition.notify_all()

    def limited(self, retry_after=None):
        # Only a bounded numeric duration is used; never store a broker error body.
        seconds = min(300, max(30, int(retry_after))) if str(retry_after).isdigit() else 30
        with self.lock:
            self.cooldown_until = max(self.cooldown_until, self.clock() + seconds)
        raise NamuhReadDeferred(seconds, "provider")

    def invalidate(self):
        with self.cache_lock:
            self.epoch += 1
            self.cache.clear()

    def read(self, path, body, fetch, authorization=""):
        """Only explicitly permitted read bodies; critical order checks are never reused.

        Credentials/connection ownership must be checked by the caller BEFORE this method.
        No keys/accounts in cache keys, no failed/partial replies cached, no stale fallback.
        """
        ttl = {"/n2/acctinfo": 60, "/krstock/inquiry/v1/balance": 5, "/krstock/quote/v1/currentPrice": 2}.get(
            path, 0
        )
        shared = _lane.get() in {"monitor", "display"} and ttl > 0
        digest = hashlib.sha256(json.dumps([path, body, authorization], sort_keys=True).encode()).hexdigest()
        with self.lock:
            if self.cooldown_until > self.clock():
                raise NamuhReadDeferred(max(1, int(self.cooldown_until - self.clock()) + 1))
        with self.cache_lock:
            epoch = self.epoch
            cached = self.cache.get(digest)
            if shared and cached and self.clock() - cached[0] < ttl:
                return copy.deepcopy(cached[1])
            event = self.inflight.get(digest) if shared else None
            producer = event is None
            if shared and producer:
                event = threading.Event()
                self.inflight[digest] = event
        if shared and not producer:
            if not event.wait(6):
                raise NamuhReadDeferred(2, "queue")
            # Recheck credentials at the caller on the next request; never retry a failed HTTP call.
            with self.cache_lock:
                cached = self.cache.get(digest)
                if cached and self.clock() - cached[0] < ttl and epoch == self.epoch:
                    return copy.deepcopy(cached[1])
            raise NamuhReadDeferred(2, "queue")
        try:
            value, environment = fetch()
            result = ({**value, "_read_at": now().isoformat()}, environment)
            expected = (
                isinstance(value.get("Output_0"), list)
                if path == "/n2/acctinfo"
                else isinstance(value.get("Output_1"), list)
                if path.endswith("/balance")
                else isinstance(value.get("Output_0"), dict)
            )
            message = value.get("message")
            response_code = value.get("rsp_cd") or (
                message.get("rsp_cd") if isinstance(message, dict) else None
            )
            if str(response_code).startswith("IGW") or value.get("error_code"):
                expected = False
            if (
                ttl
                and expected
                and not value.get("_has_more")
                and value.get("cts_flag") in (None, "", "N", "0")
            ):
                with self.cache_lock:
                    if epoch == self.epoch:
                        if len(self.cache) >= 256:
                            self.cache.pop(next(iter(self.cache)))
                        self.cache[digest] = (self.clock(), copy.deepcopy(result))
            return result
        finally:
            if shared and producer:
                with self.cache_lock:
                    if self.inflight.get(digest) is event:
                        self.inflight.pop(digest)
                    event.set()


_gates = {}
_lock = threading.Lock()


def read_gate(user, environment, credential_scope):
    key = (user.tenant_id, user.id, environment, credential_scope)
    with _lock:
        return _gates.setdefault(key, ReadGate())
