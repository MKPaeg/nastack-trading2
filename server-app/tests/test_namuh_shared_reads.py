"""Owner-private NH read coordination; no external traffic or live orders."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from nastack_trading import namuh_reads, user_brokers
from nastack_trading.brokers import NamuhAuth
from nastack_trading.namuh_reads import NamuhReadDeferred, ReadGate, read_lane
from nastack_trading.trade_gateway import TradeGateway


@pytest.fixture
def gateway(monkeypatch):
    calls, state = [], {"quantity": 5, "price": 100, "enabled": True, "token": "synthetic-A"}
    monkeypatch.setattr(namuh_reads, "_gates", {})
    monkeypatch.setattr(user_brokers, "personal_config", lambda db, user, config, broker: config)
    monkeypatch.setattr(NamuhAuth, "scope", lambda _: "synthetic-credentials-scope")
    original = namuh_reads.read_gate

    def gate(user, environment, scope):
        result = original(user, environment, scope)
        result.interval = 0  # Test speed only; production interval is unchanged.
        return result

    monkeypatch.setattr(namuh_reads, "read_gate", gate)

    def transport(request):
        path = request.url.path
        calls.append((path, json.loads(request.content)))
        if path == "/n2/acctinfo":
            value = {"Output_0": [{"acct_no": "12345678901", "acct_type": "01"}]}
        elif path.endswith("/balance"):
            value = {
                "Output_0": {},
                "Output_1": [
                    {"iem_cd": "034220", "rsdl_qty": str(state["quantity"]), "phs_pr": "100"},
                    {"iem_cd": "494310", "rsdl_qty": "9", "phs_pr": "200"},
                ],
            }
        elif path.endswith("/currentPrice"):
            value = {"Output_0": {"stck_prpr": str(state["price"])}}
        elif path.endswith("/buyableQuantity"):
            value = {"Output_0": {"ord_psbl_qty": "5", "ord_psbl_cash": "500"}}
        elif "/order/" in path:
            value = {"Output_0": {"mkt_orr_no": "synthetic-order"}}
        else:
            value = {"Output_0": []}
        return httpx.Response(200, json=value)

    result = TradeGateway(
        SimpleNamespace(live_orders_enabled=True), namuh_transport=httpx.MockTransport(transport)
    )

    def credentials(db, user, broker):
        if not state["enabled"]:
            raise HTTPException(409, "개인 연결 사용 중지")
        return "live", {"authorization": "Bearer " + state["token"]}

    monkeypatch.setattr(result, "credentials", credentials)
    return result, calls, state


def test_two_stocks_share_account_and_full_balance_but_critical_reads_are_fresh(gateway):
    service, calls, state = gateway
    user = SimpleNamespace(id="A", tenant_id="one")

    def plan(code):
        return SimpleNamespace(code=code, broker="namuh")

    with read_lane("display"):
        a = service.position(None, user, plan("034220"), "12345678901")
        b = service.position(None, user, plan("494310"), "12345678901")
    assert a["quantity"] == 5 and b["quantity"] == 9
    assert [path for path, _ in calls] == ["/n2/acctinfo", "/krstock/inquiry/v1/balance"]
    assert a["collected_at"] == b["collected_at"]  # Original source stamp, not cache access time.
    state["quantity"] = 0
    fresh = service.position(None, user, plan("034220"), "12345678901")
    assert fresh["quantity"] == 0
    assert len(calls) == 4  # Order checks revalidate account AND current balance, without reuse.
    with read_lane("monitor"):
        assert service.position(None, user, plan("034220"), "12345678901")["quantity"] == 0
    assert len(calls) == 4  # A freshly confirmed value can serve the next monitor.


def test_owner_tenant_token_account_and_disabled_connection_cannot_share(gateway):
    service, calls, state = gateway
    path = "/krstock/inquiry/v1/balance"
    user = SimpleNamespace(id="A", tenant_id="one")
    with read_lane("display"):
        for identity in [
            user,
            SimpleNamespace(id="B", tenant_id="one"),
            SimpleNamespace(id="A", tenant_id="two"),
        ]:
            service.request(None, identity, "namuh", path, {"act_no": "12345678901"})
        assert len(calls) == 3
        service.request(None, user, "namuh", path, {"act_no": "12345678902"})
        state["token"] = "synthetic-rotated"
        service.request(None, user, "namuh", path, {"act_no": "12345678901"})
        assert len(calls) == 5
        state["enabled"] = False
        with pytest.raises(HTTPException):
            service.request(None, user, "namuh", path, {"act_no": "12345678901"})
    assert len(calls) == 5
    for gate in namuh_reads._gates.values():
        assert "12345678901" not in str(list(gate.cache))
        assert "synthetic-rotated" not in str(list(gate.cache))


def test_singleflight_copy_expiration_errors_partial_and_invalidation():
    gate = ReadGate(interval=0)
    count = [0]
    entered, release = threading.Event(), threading.Event()

    def fetch():
        count[0] += 1
        entered.set()
        assert release.wait(2)
        return {"Output_1": [{"quantity": 5}]}, "live"

    def read():
        with read_lane("display"):
            return gate.read("/krstock/inquiry/v1/balance", {"act_no": "private"}, fetch)

    with ThreadPoolExecutor(max_workers=4) as pool:
        a = pool.submit(read)
        assert entered.wait(1)
        others = [pool.submit(read) for _ in range(3)]
        release.set()
        values = [a.result()] + [f.result() for f in others]
    assert count[0] == 1
    values[0][0]["Output_1"][0]["quantity"] = 999
    assert read()[0]["Output_1"][0]["quantity"] == 5
    gate.clock = lambda: time.monotonic() + 6
    read()
    assert count[0] == 2
    gate.invalidate()
    read()
    assert count[0] == 3
    gate.invalidate()
    with read_lane("display"):
        for _ in range(2):
            with pytest.raises(HTTPException):
                gate.read(
                    "/krstock/inquiry/v1/balance",
                    {},
                    lambda: (_ for _ in ()).throw(HTTPException(502, "failed")),
                )
        assert not gate.cache


def test_late_read_cannot_repopulate_cache_after_order_invalidation():
    gate = ReadGate(interval=0)
    entered, release = threading.Event(), threading.Event()

    def read():
        def fetch():
            entered.set()
            assert release.wait(2)
            return {"Output_1": [{"quantity": 5}]}, "live"

        with read_lane("display"):
            return gate.read("/krstock/inquiry/v1/balance", {}, fetch)

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(read)
        assert entered.wait(1)
        gate.invalidate()
        release.set()
        pending.result()
    assert not gate.cache
    with read_lane("display"):
        gate.read("/krstock/inquiry/v1/balance", {}, lambda: ({"Output_1": [], "_has_more": True}, "live"))
        assert not gate.cache


def test_no_capacity_fill_or_order_response_reuse_and_orders_invalidate(gateway):
    service, calls, state = gateway
    from nastack_trading.order_boundary import transmission_guard

    user = SimpleNamespace(id="A", tenant_id="one")
    with read_lane("display"):
        for _ in range(2):
            service.request(None, user, "namuh", "/krstock/inquiry/v1/buyableQuantity", {})
        assert len(calls) == 2
        service.request(None, user, "namuh", "/krstock/inquiry/v1/balance", {})
        gate = namuh_reads.read_gate(user, "live", "synthetic-credentials-scope")
        assert gate.cache
        with transmission_guard(lambda: None):
            service.request(None, user, "namuh", "/krstock/order/v1/cashBuy", {})
        assert not gate.cache
        service.request(None, user, "namuh", "/krstock/inquiry/v1/balance", {})
        service.request(None, user, "namuh", "/krstock/inquiry/v1/dailyOrderExecution", {})
        assert not gate.cache


def test_priority_serves_critical_before_display_without_bypassing_pacing():
    occupied, release = threading.Event(), threading.Event()
    gate = ReadGate(interval=0)
    order = []

    def sleep(delay):
        occupied.set()
        assert release.wait(2)

    gate.sleep = sleep
    gate.next_at = gate.clock() + 0.05

    def acquire(lane):
        with read_lane(lane):
            gate.acquire()
            order.append(lane)

    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(acquire, "display")
        assert occupied.wait(1)
        lower = pool.submit(acquire, "display")
        critical = pool.submit(acquire, "critical")
        deadline = time.monotonic() + 1
        while len(gate.waiters) != 2 and time.monotonic() < deadline:
            time.sleep(0.001)
        release.set()
        first.result()
        lower.result()
        critical.result()
    assert order == ["display", "critical", "display"]


def test_business_rate_limit_is_not_successfully_cached(gateway, monkeypatch):
    service, calls, state = gateway
    service.transports["namuh"] = httpx.MockTransport(
        lambda r: httpx.Response(200, json={"message": {"rsp_cd": "IGW42902"}})
    )
    with read_lane("display"), pytest.raises(NamuhReadDeferred) as error:
        service.request(None, SimpleNamespace(id="A", tenant_id="one"), "namuh", "/n2/acctinfo", {})
    assert error.value.headers["X-Trading-Defer-Reason"] == "provider"
    assert not namuh_reads.read_gate(
        SimpleNamespace(id="A", tenant_id="one"), "live", "synthetic-credentials-scope"
    ).cache
