from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from nastack_trading import namuh_reads
from nastack_trading.namuh_reads import NamuhReadDeferred, ReadGate
from nastack_trading.trade_gateway import TradeGateway


class Clock:
    def __init__(self):
        self.value = 100.0
        self.delays = []

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.delays.append(seconds)
        self.value += seconds


def test_read_gate_paces_concurrent_calls_without_replaying_them():
    clock = Clock()
    gate = ReadGate(clock=clock.now, sleep=clock.sleep)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: gate.acquire(), range(8)))
    assert len(clock.delays) == 7
    assert all(seconds == pytest.approx(1.05) for seconds in clock.delays)
    with pytest.raises(NamuhReadDeferred):
        gate.limited("60")
    with pytest.raises(NamuhReadDeferred) as failure:
        gate.acquire()
    assert failure.value.status_code == 429
    clock.value += 61
    gate.acquire()


def test_gate_isolated_by_tenant_owner_environment_and_credential_scope(monkeypatch):
    monkeypatch.setattr(namuh_reads, "_gates", {})
    user = SimpleNamespace(id="A", tenant_id="one")
    gate = namuh_reads.read_gate(user, "live", "hashed-key-version")
    assert gate is namuh_reads.read_gate(user, "live", "hashed-key-version")
    for other, env, scope in [
        (SimpleNamespace(id="B", tenant_id="one"), "live", "hashed-key-version"),
        (SimpleNamespace(id="A", tenant_id="two"), "live", "hashed-key-version"),
        (user, "mock", "hashed-key-version"),
        (user, "live", "new-key-version"),
    ]:
        assert namuh_reads.read_gate(other, env, scope) is not gate


def test_read_429_blocks_following_reads_but_does_not_reclassify_order_failures(monkeypatch):
    from nastack_trading import user_brokers
    from nastack_trading.brokers import NamuhAuth
    from nastack_trading.order_boundary import transmission_guard

    monkeypatch.setattr(namuh_reads, "_gates", {})
    monkeypatch.setattr(user_brokers, "personal_config", lambda db, user, config, broker: config)
    monkeypatch.setattr(NamuhAuth, "scope", lambda _: "key-version-hash")
    calls = []

    def transport(request):
        calls.append(request.url.path)
        return httpx.Response(429, json={"message": "DO-NOT-EXPOSE-secret-account"})

    gateway = TradeGateway(
        SimpleNamespace(live_orders_enabled=True), namuh_transport=httpx.MockTransport(transport)
    )
    monkeypatch.setattr(gateway, "credentials", lambda *args: ("live", {}))
    user = SimpleNamespace(id="A", tenant_id="one")
    for _ in range(2):
        with pytest.raises(NamuhReadDeferred) as failure:
            gateway.request(None, user, "namuh", "/krstock/quote/v1/currentPrice", {})
        assert "DO-NOT-EXPOSE" not in failure.value.detail
    assert len(calls) == 1  # No retry; the next independent read is deferred locally.
    authorized = []
    with transmission_guard(lambda: authorized.append(True)):
        with pytest.raises(HTTPException) as order_failure:
            gateway.request(None, user, "namuh", "/krstock/order/v1/cashBuy", {})
    assert not isinstance(order_failure.value, NamuhReadDeferred)
    assert order_failure.value.status_code == 502
    assert authorized == [True]
    assert calls == ["/krstock/quote/v1/currentPrice", "/krstock/order/v1/cashBuy"]
