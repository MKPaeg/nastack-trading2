from datetime import timedelta
from types import SimpleNamespace

from conftest import CODE, EMAIL, ORIGIN
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from nastack_trading.app import create_app
from nastack_trading.db import now
from nastack_trading.models import BrokerSnapshot, TradePlan, User
from nastack_trading.trade_gateway import TradeGateway


def test_namuh_display_defers_with_original_same_broker_stamp_and_slower_cadence(setup, monkeypatch):
    from nastack_trading.namuh_reads import NamuhReadDeferred

    config, factory = setup
    stamp = now() - timedelta(seconds=30)
    with factory.begin() as db:
        user = db.scalar(select(User))
        plan = TradePlan(
            owner_id=user.id,
            tenant_id=user.tenant_id,
            broker="namuh",
            code="034220",
            value={"name": "Synthetic", "mode": "automatic"},
        )
        db.add(plan)
        db.flush()
        pid = plan.id
        db.add(
            BrokerSnapshot(
                owner_id=user.id,
                tenant_id=user.tenant_id,
                kind="trade_quote",
                symbol=plan.code,
                environment="live",
                created_at=stamp,
                value={"plan_id": pid, "broker": "namuh", "price": 7000},
            )
        )
    monkeypatch.setattr("nastack_trading.trading.market_session", lambda: {"is_open": True})
    monkeypatch.setattr(TradeGateway, "quote", lambda *args: (_ for _ in ()).throw(NamuhReadDeferred(30)))
    with TestClient(create_app(config), headers={"origin": ORIGIN}) as client:
        client.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE})
        result = client.get(f"/api/trading/plans/{pid}/quote")
        assert result.status_code == 200
        value = result.json()["snapshot"]
        assert value["broker"] == "namuh" and value["price"] == 7000
        assert value["stale"] and value["refresh_after_ms"] == 30000
        assert value["refresh_note"]
        assert value["collected_at"] != now().isoformat()


def test_plan_quote_uses_same_broker_snapshot_and_owner_scope(setup, monkeypatch):
    config, factory = setup
    with factory() as db:
        user = db.scalar(select(User))
        plan = TradePlan(
            owner_id=user.id,
            tenant_id=user.tenant_id,
            broker="kiwoom",
            code="046120",
            value={"name": "Synthetic"},
        )
        db.add(plan)
        db.flush()
        pid = plan.id
        db.add(
            BrokerSnapshot(
                owner_id=user.id,
                tenant_id=user.tenant_id,
                kind="trade_quote",
                symbol=plan.code,
                environment="live",
                created_at=now(),
                value={"plan_id": pid, "broker": "kiwoom", "price": 3660},
            )
        )
        db.add(
            BrokerSnapshot(
                owner_id=user.id,
                tenant_id=user.tenant_id,
                kind="market",
                symbol=plan.code,
                environment="live",
                value={"price": 3655, "chart": []},
            )
        )
        db.commit()
    monkeypatch.setattr(
        TradeGateway, "quote", lambda *args: (_ for _ in ()).throw(AssertionError("cached quote expected"))
    )
    with TestClient(create_app(config), headers={"origin": ORIGIN}) as client:
        assert client.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE}).status_code == 200
        result = client.get(f"/api/trading/plans/{pid}/quote")
        assert result.status_code == 200
        assert result.json()["snapshot"]["price"] == 3660
        assert result.json()["snapshot"]["broker"] == "kiwoom"
        assert client.get("/api/trading/plans/foreign-plan/quote").status_code == 404


def test_gateway_records_exact_quote_snapshot_without_order_calls(setup, monkeypatch):
    config, factory = setup
    gateway = TradeGateway(config)
    calls = []

    def request(*args):
        calls.append(args[-1])
        return {"cur_prc": "-3660"}, "live"

    monkeypatch.setattr(gateway, "request", request)
    with factory() as db:
        user = db.scalar(select(User))
        plan = SimpleNamespace(id="synthetic-plan", code="046120", broker="kiwoom")
        assert gateway.quote(db, user, plan) == 3660
        row = db.scalar(select(BrokerSnapshot).where(BrokerSnapshot.kind == "trade_quote"))
        assert row.owner_id == user.id and row.tenant_id == user.tenant_id
        assert row.value["price"] == 3660
        assert row.value["plan_id"] == plan.id
    assert calls == ["ka10001"]


def test_expired_quote_failure_does_not_fallback_to_other_broker(setup, monkeypatch):
    config, factory = setup
    with factory() as db:
        user = db.scalar(select(User))
        plan = TradePlan(
            owner_id=user.id,
            tenant_id=user.tenant_id,
            broker="kiwoom",
            code="046120",
            value={"name": "Synthetic"},
        )
        db.add(plan)
        db.flush()
        pid = plan.id
        db.add(
            BrokerSnapshot(
                owner_id=user.id,
                tenant_id=user.tenant_id,
                kind="trade_quote",
                symbol=plan.code,
                environment="live",
                created_at=now() - timedelta(minutes=30),
                value={"plan_id": pid, "broker": "kiwoom", "price": 100},
            )
        )
        db.add(
            BrokerSnapshot(
                owner_id=user.id,
                tenant_id=user.tenant_id,
                kind="market",
                symbol=plan.code,
                environment="live",
                value={"price": 999},
            )
        )
        db.commit()
    import nastack_trading.trading as trading

    monkeypatch.setattr(trading, "market_session", lambda: {"is_open": True})

    def unavailable(*args):
        raise HTTPException(409, "본인 증권사 인증 만료")

    monkeypatch.setattr(TradeGateway, "quote", unavailable)
    with TestClient(create_app(config), headers={"origin": ORIGIN}) as client:
        client.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE})
        result = client.get(f"/api/trading/plans/{pid}/quote")
        assert result.status_code == 409
        assert "인증 만료" in result.json()["detail"]
