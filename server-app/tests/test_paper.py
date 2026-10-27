from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from conftest import CODE, EMAIL, ORIGIN
from fastapi.testclient import TestClient
from sqlalchemy import select

from nastack_trading.app import create_app
from nastack_trading.models import PaperWallet, Tenant, TradePlan, User
from nastack_trading.paper import evaluate, initial, scenario_suite, tick, work_once
from nastack_trading.security import create_session, hash_code
from nastack_trading.trade_gateway import TradeGateway


def test_postgres_expired_token_audit_does_not_deadlock_wallet(setup):
    """Reproduces production's two DB sessions, using only synthetic broker HTTP."""
    from datetime import timedelta
    from types import SimpleNamespace

    import httpx
    from cryptography.fernet import Fernet
    from pydantic import SecretStr
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session

    from nastack_trading.db import now
    from nastack_trading.models import AuditEvent, KiwoomConnection
    from nastack_trading.paper import locked_wallet, read_quote

    config, factory = setup
    if factory.kw["bind"].dialect.name != "postgresql":
        pytest.skip("Requires isolated PostgreSQL to exercise row/FK locks")
    config = config.model_copy(
        update={
            "kiwoom_owner_email": EMAIL,
            "kiwoom_app_key": SecretStr("synthetic-key"),
            "kiwoom_app_secret": SecretStr("synthetic-secret"),
            "broker_encryption_key": SecretStr(Fernet.generate_key().decode()),
        }
    )
    with factory.begin() as db:
        user = db.scalar(select(User).where(User.email == EMAIL))
        identity = user.id
        db.add(PaperWallet(owner_id=user.id, tenant_id=user.tenant_id, value=initial()))
        db.add(KiwoomConnection(owner_id=user.id, tenant_id=user.tenant_id, enabled=True, environment="live"))
    calls = []

    def transport(request):
        calls.append(request.url.path)
        if request.url.path == "/oauth2/token":
            return httpx.Response(
                200,
                json={
                    "token": "synthetic-token",
                    "return_code": 0,
                    "expires_dt": (now() + timedelta(hours=4))
                    .astimezone(ZoneInfo("Asia/Seoul"))
                    .strftime("%Y%m%d%H%M%S"),
                },
            )
        assert request.url.path == "/api/dostk/stkinfo"
        return httpx.Response(200, json={"return_code": 0, "cur_prc": "400000"})

    gateway = TradeGateway(config, kiwoom_transport=httpx.MockTransport(transport))
    with factory() as db:
        user = db.get(User, identity)
        locked_wallet(db, user)

        # Bound all external-session SQL to fail quickly with the old FOR UPDATE.
        def quote(external, owner, plan):
            external.execute(text("SET statement_timeout = '2000ms'"))
            return gateway.quote(external, owner, plan)

        plan = SimpleNamespace(broker="kiwoom", code="373220")
        assert read_quote(db, user, plan, quote) == 400000
        assert read_quote(db, user, plan, quote) == 400000
        assert calls.count("/oauth2/token") == 1
        # The replacement lock must STILL serialize wallet mutation by another session.
        with Session(db.bind) as competing:
            competing.execute(text("SET lock_timeout = '100ms'"))
            with pytest.raises(OperationalError):
                locked_wallet(competing, user)
            competing.rollback()
        db.rollback()
    with factory() as db:
        assert db.scalar(
            select(AuditEvent.id).where(
                AuditEvent.actor_id == identity, AuditEvent.action == "kiwoom.auth_verified"
            )
        )


def at(clock="09:00:00"):
    return datetime.fromisoformat("2026-10-22T" + clock).replace(tzinfo=ZoneInfo("Asia/Seoul"))


def test_scenario_suite_finishes_all_paths_without_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("No external API allowed")

    import httpx

    monkeypatch.setattr(httpx.Client, "send", forbidden)
    result = scenario_suite()
    assert result["total"] == 20
    assert result["passed"] == result["total"], result
    assert result["failed"] == 0
    assert result["orders_sent"] is False
    # A failed case must not prevent remaining scenarios from finishing.
    import nastack_trading.paper as paper

    original = paper.evaluate

    def fail_one(wallet, identity, rule, *args, **kwargs):
        if rule.get("fraction") == 20:
            raise ValueError("synthetic failure")
        return original(wallet, identity, rule, *args, **kwargs)

    monkeypatch.setattr(paper, "evaluate", fail_one)
    failed = scenario_suite()
    assert failed["total"] == 20 and failed["failed"] == 1


def test_scenario_suite_history_is_owner_scoped_and_wallet_unchanged(setup, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("No broker API allowed")

    monkeypatch.setattr(TradeGateway, "submit", forbidden)
    monkeypatch.setattr(TradeGateway, "quote", forbidden)
    config, factory = setup
    with factory.begin() as db:
        admin = db.scalar(select(User).where(User.email == EMAIL))
        db.add(PaperWallet(owner_id=admin.id, tenant_id=admin.tenant_id, value=initial()))
        tenant = Tenant(name="Suite isolated", timezone="Asia/Seoul")
        db.add(tenant)
        db.flush()
        other = User(
            tenant_id=tenant.id,
            email="suite@example.com",
            role="trader",
            active=True,
            code_hash=hash_code(CODE),
        )
        db.add(other)
        db.flush()
        token = create_session(db, other, 24)
        other_id = other.id
    app = create_app(config)
    with TestClient(app, headers={"origin": ORIGIN}) as a, TestClient(app, headers={"origin": ORIGIN}) as b:
        a.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE})
        b.cookies.set("trading_session", token)
        before = a.get("/api/trading/paper").json()["wallet"]
        result = a.post("/api/trading/paper/scenario-suite")
        assert result.status_code == 200, result.text
        assert result.json()["suite"]["passed"] == 20
        restored = a.get("/api/trading/paper").json()
        assert restored["wallet"] == before
        assert restored["latest_suite"] == result.json()
        assert b.get("/api/trading/paper").json()["latest_suite"] is None
        assert (
            a.put("/api/trading/paper/schedule", json={"trade_time": "10:00", "version": 0}).status_code
            == 200
        )
        assert a.get("/api/trading/paper").json()["wallet"]["trade_time"] == "10:00"
        assert b.get("/api/trading/paper").json()["wallet"].get("trade_time", "09:00") == "09:00"
        assert (
            a.put("/api/trading/paper/schedule", json={"trade_time": "11:00", "version": 0}).status_code
            == 409
        )
        assert (
            a.put("/api/trading/paper/schedule", json={"trade_time": "08:50", "version": 1}).status_code
            == 422
        )
        assert a.put("/api/trading/paper", json={"enabled": True}).status_code == 200
        assert (
            a.put("/api/trading/paper/schedule", json={"trade_time": "11:00", "version": 1}).status_code
            == 409
        )
        assert b.post("/api/trading/paper/scenario-suite").status_code == 200
        with factory.begin() as db:
            db.get(User, other_id).role = "viewer"
        assert b.post("/api/trading/paper/scenario-suite").status_code == 403
        assert (
            b.put("/api/trading/paper/schedule", json={"trade_time": "11:00", "version": 0}).status_code
            == 403
        )


def test_single_time_forecast_evaluated_at_trade_time_without_checkpoint():
    rule = {"buy_rule": "forecast", "fraction": 10, "take_profit": 3, "trade_time": "10:00"}
    wallet = initial()
    assert evaluate(wallet, "lg", rule, at(), True, 100, 0.02, True)["action"] == "skip"
    assert evaluate(wallet, "lg", rule, at("10:00:00"), True, 100, None, True)["action"] == "skip"
    assert evaluate(wallet, "lg", rule, at("10:00:10"), True, 100, 0.02, True)["action"] == "skip"
    assert evaluate(wallet, "lg", rule, at("10:00:20"), True, 100, 0.02, True)["action"] == "skip"
    assert evaluate(wallet, "lg", rule, at("11:00:00"), True, 110, None, True)["action"] == "skip"
    assert evaluate(initial(), "lg", rule, at("10:01:00"), True, 100, 0.02, True)["action"] == "skip"


def test_execution_cash_duplicates_sell_and_no_order_gateway():
    w = initial()
    rule = {"buy_rule": "price", "buy_price": 100, "fraction": 100, "take_profit": 3}
    assert evaluate(w, "a", rule, at(), False, 100, execute=True)["reason"] == "휴장일"
    assert evaluate(w, "a", rule, at(), True, None)["action"] == "skip"
    result = evaluate(w, "a", rule, at(), True, 100, execute=True)
    assert result["action"] == "buy" and 0 <= w["cash"] < 101
    assert evaluate(w, "a", rule, at(), True, 100, execute=True)["action"] == "skip"
    assert evaluate(w, "b", rule, at(), True, 100, execute=True)["action"] == "skip"
    result = evaluate(w, "a", rule, at("10:00:00"), True, 110, execute=True)
    assert result["action"] == "sell" and result["realized_pnl"] > 0
    assert evaluate(w, "a", rule, at("10:00:30"), True, 110, execute=True)["action"] == "skip"


def test_forecast_checkpoint_required_and_open_window():
    w = initial()
    rule = {"buy_rule": "forecast", "fraction": 10, "take_profit": 3}
    assert evaluate(w, "a", rule, at(), True, 100, execute=True)["action"] == "skip"
    evaluate(w, "a", rule, at("08:50:10"), True, None, 0.02, True)
    assert evaluate(w, "a", rule, at("09:01:00"), True, 100, execute=True)["action"] == "skip"
    assert evaluate(w, "a", rule, at(), True, 100, execute=True)["action"] == "skip"


def test_api_owner_isolation_viewer_and_no_submit(setup, monkeypatch):
    def forbidden(*args):
        raise AssertionError("Broker order/capacity must never be invoked")

    monkeypatch.setattr(TradeGateway, "submit", forbidden)
    monkeypatch.setattr(TradeGateway, "capacity", forbidden)
    config, factory = setup
    with factory.begin() as db:
        owner = db.scalar(select(User).where(User.email == EMAIL))
        other = User(
            tenant_id=owner.tenant_id,
            email="paper@example.com",
            role="trader",
            active=True,
            code_hash=hash_code(CODE),
        )
        db.add(other)
        plan = TradePlan(
            owner_id=owner.id,
            tenant_id=owner.tenant_id,
            broker="kiwoom",
            code="123456",
            value={
                "mode": "automatic",
                "buy_rule": "price",
                "buy_price": 100,
                "fraction": 10,
                "take_profit": 3,
            },
        )
        db.add(plan)
        db.flush()
        plan_id = plan.id
        other_id = other.id
        tenant = Tenant(name="Paper isolated tenant", timezone="Asia/Seoul")
        db.add(tenant)
        db.flush()
        isolated = User(
            tenant_id=tenant.id,
            email="isolated@example.com",
            role="trader",
            active=True,
            code_hash=hash_code(CODE),
        )
        db.add(isolated)
        db.flush()
        isolated_token = create_session(db, isolated, 24)
    app = create_app(config)
    with TestClient(app, headers={"origin": ORIGIN}) as a, TestClient(app, headers={"origin": ORIGIN}) as b:
        a.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE})
        b.post("/api/auth/login", json={"email": "paper@example.com", "access_code": CODE})
        assert a.put("/api/trading/paper", json={"enabled": True}).status_code == 200
        assert not b.get("/api/trading/paper").json()["wallet"]["enabled"]
        payload = {"plan_id": plan_id, "at": at().isoformat(), "price": 100}
        assert b.post("/api/trading/paper/scenario", json=payload).status_code == 404
        result = a.post("/api/trading/paper/scenario", json=payload)
        assert result.status_code == 200 and not result.json()["orders_sent"]
        assert a.get("/api/trading/paper").json()["wallet"]["cash"] == 10000000
        with factory.begin() as db:
            db.get(User, other_id).role = "viewer"
        assert b.put("/api/trading/paper", json={"enabled": True}).status_code == 403
        assert b.post("/api/trading/paper/check").status_code == 403
        with TestClient(app, headers={"origin": ORIGIN}) as c:
            c.cookies.set("trading_session", isolated_token)
            assert not c.get("/api/trading/paper").json()["events"]
            assert c.post("/api/trading/paper/scenario", json=payload).status_code == 404
    with factory.begin() as db:
        user = db.scalar(select(User).where(User.email == EMAIL))
        wallet = db.get(PaperWallet, user.id)
        wallet.value = {**wallet.value, "start_on_click": False}
        result = tick(db, user, wallet, at(), lambda *args: 100, True)
        assert result[0]["action"] == "buy"
        assert tick(db, user, wallet, at(), lambda *args: 100, True)[0]["action"] == "skip"


def test_start_immediately_checks_at_click_time_and_runs_suite(setup, monkeypatch):
    import nastack_trading.paper as paper

    monkeypatch.setattr(paper, "now", lambda: at("11:15:00"))
    monkeypatch.setattr(paper, "market_session", lambda *args: {"is_open": True})
    monkeypatch.setattr(TradeGateway, "quote", lambda *args: 1000)

    def forbidden(*args, **kwargs):
        raise AssertionError("No broker order or capacity allowed")

    monkeypatch.setattr(TradeGateway, "submit", forbidden)
    monkeypatch.setattr(TradeGateway, "capacity", forbidden)
    with TestClient(create_app(setup[0]), headers={"origin": ORIGIN}) as client:
        client.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE})
        assert client.put("/api/trading/paper/pilot").status_code == 200
        result = client.put("/api/trading/paper", json={"enabled": True})
        assert result.status_code == 200, result.text
        assert result.json()["results"][0]["action"] == "skip"  # Retired forecast pilot cannot buy.
        state = client.get("/api/trading/paper").json()
        assert state["wallet"]["trade_time"] == "11:15"
        assert state["latest_suite"]["suite"]["failed"] == 0
        assert (
            client.put("/api/trading/paper", json={"enabled": True}).json()["results"][0]["action"] == "skip"
        )


def test_worker_resume_pause_and_disabled_owner(setup, monkeypatch):
    import nastack_trading.paper as paper

    factory = setup[1]
    monkeypatch.setattr(paper, "now", lambda: at())
    calls = []

    def quote(*args):
        calls.append(True)
        return 100

    with factory.begin() as db:
        user = db.scalar(select(User).where(User.email == EMAIL))
        owner = user.id
        db.add(PaperWallet(owner_id=owner, tenant_id=user.tenant_id, value={**initial(), "enabled": True}))
        db.add(
            TradePlan(
                owner_id=owner,
                tenant_id=user.tenant_id,
                broker="kiwoom",
                code="123456",
                value={
                    "mode": "automatic",
                    "buy_rule": "price",
                    "buy_price": 100,
                    "fraction": 100,
                    "take_profit": 3,
                },
            )
        )
    work_once(factory, quote)
    work_once(factory, quote)
    assert len(calls) == 1
    with factory.begin() as db:
        w = db.get(PaperWallet, owner)
        assert len(w.value["executed"]) == 1
        w.value = {**w.value, "enabled": False}
    monkeypatch.setattr(paper, "now", lambda: at("09:05:00"))
    work_once(factory, quote)
    assert len(calls) == 1
    with factory.begin() as db:
        w = db.get(PaperWallet, owner)
        w.value = {**w.value, "enabled": True}
        db.get(User, owner).active = False
    work_once(factory, quote)
    assert len(calls) == 1


def test_lg_pilot_does_not_change_live_plans(setup):
    config, factory = setup
    with TestClient(create_app(config), headers={"origin": ORIGIN}) as c:
        c.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE})
        result = c.put("/api/trading/paper/pilot")
        assert result.status_code == 200 and result.json()["code"] == "373220"
        assert c.get("/api/trading/paper").json()["wallet"]["pilot"]
        for prediction, action in [(0.02, "skip"), (-0.02, "skip"), (None, "skip")]:
            result = c.post(
                "/api/trading/paper/scenario",
                json={
                    "plan_id": "pilot:373220",
                    "at": at().isoformat(),
                    "price": 400000,
                    "predicted_return": prediction,
                },
            )
            assert result.status_code == 200, result.text
            assert result.json()["result"]["action"] == action
            assert not result.json()["orders_sent"]
        assert c.get("/api/trading/paper").json()["wallet"]["cash"] == 10000000
    with factory() as db:
        assert not list(db.scalars(select(TradePlan)))
