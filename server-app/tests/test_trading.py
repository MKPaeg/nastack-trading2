from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from conftest import CODE, EMAIL, ORIGIN
from fastapi.testclient import TestClient

from nastack_trading.app import create_app
from nastack_trading.trade_gateway import TradeGateway
from nastack_trading.trading import automatic_signal


@pytest.mark.parametrize(
    "status", ["preview", "submitting", "accepted", "partial", "unknown", "cancel_pending", "cancel_unknown"]
)
def test_only_completed_orders_can_notify(setup, status):
    from types import SimpleNamespace

    from nastack_trading.trading import TradingService

    # No database or network is needed for a non-completion state.
    TradingService(setup[0], None).notify(None, None, SimpleNamespace(status=status))


def test_trader_can_read_personal_channels_but_not_admin_controls(setup):
    from sqlalchemy import select

    from nastack_trading.models import User

    config, factory = setup
    with TestClient(create_app(config), headers={"origin": ORIGIN}) as client:
        assert client.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE}).status_code == 200
        with factory() as db:
            user = db.scalar(select(User).where(User.email == EMAIL))
            user.role = "trader"
            db.commit()
        # A former administrator's unlimited token is invalid after demotion.
        assert client.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE}).status_code == 200
        assert client.get("/api/admin/settings").status_code == 403
        assert client.get("/api/users").status_code == 403
        assert client.get("/api/trading/notification-channels").status_code == 200


def test_channels_owner_scope_version_and_viewer(setup):
    from fastapi import HTTPException
    from sqlalchemy import select

    from nastack_trading.models import User, UserSettings
    from nastack_trading.trade_notifications import Channels, channels, save_channels

    with setup[1]() as db:
        user = db.scalar(select(User).where(User.email == EMAIL))
        if not db.get(UserSettings, user.id):
            db.add(UserSettings(user_id=user.id, tenant_id=user.tenant_id, value={"unrelated": 42}))
            db.commit()
        value = channels(db, user)
        saved = save_channels(db, user, Channels(version=value["version"], email=False, telegram=True))
        assert saved["email"] is False and saved["telegram"] is True
        assert channels(db, user) == saved
        with pytest.raises(HTTPException):
            save_channels(db, user, Channels(version=value["version"], email=True, telegram=True))
        db.rollback()
        user.role = "viewer"
        with pytest.raises(HTTPException):
            save_channels(db, user, Channels(version=saved["version"], email=True, telegram=True))
        db.rollback()


def test_cancel_wire_and_verified_ledger(setup, monkeypatch):
    from types import SimpleNamespace

    from fastapi import HTTPException

    from nastack_trading.db import now

    gateway = TradeGateway(setup[0].model_copy(update={"live_orders_enabled": True, "live_order_scope": {}}))
    order = SimpleNamespace(
        created_at=now(),
        value={
            "broker": "kiwoom",
            "code": "046120",
            "side": "sell",
            "quantity": 1,
            "limit_price": 4000,
            "broker_order_no": "0000012",
            "environment": "live",
            "cancel_quantity": 1,
        },
    )
    calls = []

    def request(*args):
        calls.append(args)
        return {"ord_no": "0000013"}, "live"

    monkeypatch.setattr(gateway, "request", request)
    assert gateway.cancel(None, SimpleNamespace(email=EMAIL), order) == "0000013"
    assert calls[-1][-1] == "kt10003"
    assert calls[-1][-2]["cncl_qty"] == "1"
    order.value["cancel_order_no"] = "0000013"
    original = {
        "ord_no": "12",
        "stk_cd": "A046120",
        "ord_qty": "1",
        "cntr_qty": "0",
        "ord_remnq": "0",
        "cntr_uv": "0",
        "io_tp_nm": "현금매도",
    }
    cancel = {
        "ord_no": "13",
        "ori_ord": "12",
        "stk_cd": "A046120",
        "mdfy_cncl": "취소확인",
        "acpt_tp": "접수",
        "cnfm_qty": "1",
    }
    monkeypatch.setattr(
        gateway, "request", lambda *args: ({"acnt_ord_cntr_prps_dtl": [original, cancel]}, "live")
    )
    assert gateway.fills(None, None, order)["cancel_confirmed"] is True
    cancel["mdfy_cncl"] = "취소"
    with pytest.raises(HTTPException):
        gateway.fills(None, None, order)


def test_cancel_idempotent_owner_scoped_and_confirmation_required(setup, monkeypatch):
    from types import SimpleNamespace

    from fastapi import HTTPException
    from sqlalchemy import select

    from nastack_trading.models import TradeOrder, TradePlan, User
    from nastack_trading.trading import TradingService

    gateway = TradeGateway(setup[0])
    calls = []
    ledger = {"filled_quantity": 0, "remaining_quantity": 1, "fill_price": None}
    monkeypatch.setattr(gateway, "fills", lambda *args: dict(ledger))
    monkeypatch.setattr(gateway, "cancel", lambda *args: calls.append("cancel") or "13")
    service = TradingService(
        setup[0].model_copy(update={"live_orders_enabled": True, "live_order_scope": {}}), gateway
    )
    monkeypatch.setattr(service, "notify", lambda *args: calls.append("notify"))
    with setup[1]() as db:
        user = db.scalar(select(User).where(User.email == EMAIL))
        plan = TradePlan(owner_id=user.id, tenant_id=user.tenant_id, broker="kiwoom", code="046120", value={})
        db.add(plan)
        db.flush()
        order = TradeOrder(
            owner_id=user.id,
            tenant_id=user.tenant_id,
            plan_id=plan.id,
            request_key="cancel-synthetic",
            status="accepted",
            value={"broker": "kiwoom", "code": "046120", "side": "sell", "quantity": 1, "limit_price": 4000},
        )
        db.add(order)
        db.commit()
        stranger = SimpleNamespace(id="not-owner", tenant_id=user.tenant_id, active=True, role="user")
        with pytest.raises(HTTPException) as error:
            service.cancel(db, stranger, order.id)
        assert error.value.status_code == 404
        db.rollback()
        assert service.cancel(db, user, order.id).status == "cancel_pending"
        assert service.cancel(db, user, order.id).status == "cancel_pending"
        assert calls == ["cancel"]
        ledger.update(remaining_quantity=0, canceled_quantity=1, cancel_confirmed=True)
        assert service.reconcile(db, user, order.id).status == "canceled"
        assert service.cancel(db, user, order.id).status == "canceled"
        assert calls == ["cancel", "notify"]


def test_exact_fill_lookup_is_read_only_and_fails_closed(setup, monkeypatch):
    from types import SimpleNamespace

    from fastapi import HTTPException

    from nastack_trading.db import now

    gateway = TradeGateway(setup[0])
    order = SimpleNamespace(
        created_at=now(),
        value={
            "broker": "kiwoom",
            "code": "046120",
            "side": "buy",
            "quantity": 1,
            "broker_order_no": "0000012",
            "environment": "live",
        },
    )
    row = {
        "ord_no": "12",
        "stk_cd": "A046120",
        "ord_qty": "1",
        "cntr_qty": "1",
        "ord_remnq": "0",
        "cntr_uv": "3955",
        "io_tp_nm": "현금매수",
    }
    calls = []

    def request(*args):
        calls.append(args)
        return {"acnt_ord_cntr_prps_dtl": [row]}, "live"

    monkeypatch.setattr(gateway, "request", request)
    assert gateway.fills(None, None, order) == {
        "filled_quantity": 1,
        "remaining_quantity": 0,
        "fill_price": 3955,
    }
    assert calls[-1][-1] == "kt00007"
    for key, value in [
        ("ord_no", "13"),
        ("stk_cd", "A005930"),
        ("cntr_qty", "2"),
        ("io_tp_nm", "현금매도"),
        ("mdfy_cncl", "취소"),
    ]:
        original = row.get(key)
        row[key] = value
        with pytest.raises(HTTPException):
            gateway.fills(None, None, order)
        if original is None:
            row.pop(key)
        else:
            row[key] = original


def test_limit_quantity_query_includes_price_and_live_scope_fails_closed(setup, monkeypatch):
    from types import SimpleNamespace

    from fastapi import HTTPException

    scope = {
        "owner_email": EMAIL,
        "broker": "kiwoom",
        "code": "046120",
        "side": "buy",
        "quantity": 1,
        "limit_price": 3985,
    }
    config = setup[0].model_copy(update={"live_orders_enabled": True, "live_order_scope": scope})
    gateway = TradeGateway(config)
    monkeypatch.setattr(gateway, "validate_account", lambda *args: None)
    calls = []

    def request(*args):
        calls.append(args)
        return {
            "ord_alowa": "1571006",
            "min_ord_alow_amt": "1571006",
            "min_ord_alowq": "394",
            "ord_no": "synthetic",
        }, "live"

    monkeypatch.setattr(gateway, "request", request)
    user, plan = SimpleNamespace(email=EMAIL), SimpleNamespace(broker="kiwoom", code="046120")
    assert gateway.capacity(None, user, plan, "", "buy", 3985)[0] == 394
    assert calls[-1][4]["uv"] == "3985"
    assert gateway.submit(None, user, plan, "", "buy", 1, limit_price=3985) == "synthetic"
    before = len(calls)
    for side, qty, price in [("sell", 1, 3985), ("buy", 2, 3985), ("buy", 1, 3986), ("buy", 1, None)]:
        with pytest.raises(HTTPException) as error:
            gateway.submit(None, user, plan, "", side, qty, limit_price=price)
        assert error.value.status_code == 403
    with pytest.raises(HTTPException):
        gateway.submit(None, SimpleNamespace(email="other@example.com"), plan, "", "buy", 1, limit_price=3985)
    assert len(calls) == before


def test_small_live_test_one_share_only_and_still_locked(setup, monkeypatch):
    import nastack_trading.trading as trading

    monkeypatch.setattr(trading, "market_session", lambda: {"is_open": True})
    monkeypatch.setattr(TradeGateway, "capacity", lambda *args: (100, 100000, "live"))
    monkeypatch.setattr(TradeGateway, "quote", lambda *args: 100)

    def forbidden(*args):
        raise AssertionError("No live order authorized during test")

    monkeypatch.setattr(TradeGateway, "submit", forbidden)
    config = setup[0].model_copy(update={"live_orders_enabled": False})
    with TestClient(create_app(config), headers={"origin": ORIGIN}) as client:
        client.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE})
        assert (
            client.post(
                "/api/admin/instruments",
                json={
                    "code": "123456",
                    "name": "Synthetic",
                    "market": "KOSPI",
                    "asset_type": "stock",
                    "status": "active",
                    "source_note": "synthetic",
                },
            ).status_code
            == 201
        )
        plan = client.put(
            "/api/trading/plans",
            json={"code": "123456", "broker": "kiwoom", "mode": "automatic", "fraction": 100},
        ).json()
        body = {
            "plan_id": plan["id"],
            "side": "buy",
            "quantity": 1,
            "purpose": "small_live_test",
            "limit_price": 100,
        }
        assert client.post("/api/trading/orders/preview", json={**body, "quantity": 2}).status_code == 422
        assert (
            client.post("/api/trading/orders/preview", json={**body, "limit_price": None}).status_code == 422
        )
        assert client.post("/api/trading/orders/preview", json={**body, "limit_price": -1}).status_code == 422
        assert client.post("/api/trading/orders/preview", json={**body, "side": "sell"}).status_code == 200
        intent = client.post("/api/trading/orders/preview", json=body)
        assert intent.status_code == 200, intent.text
        assert intent.json()["quantity"] == 1 and intent.json()["purpose"] == "small_live_test"
        assert intent.json()["status"] == "preview"
        assert intent.json()["limit_price"] == 100 and intent.json()["order_type"] == "limit"
        assert (
            client.post(
                "/api/trading/orders/confirm",
                json={"intent_id": intent.json()["id"], "confirmation": "실전 주문 확인"},
            ).status_code
            == 409
        )
        monkeypatch.setattr(TradeGateway, "capacity", lambda *args: (100, 100000, "mock"))
        assert client.post("/api/trading/orders/preview", json=body).status_code == 409


@pytest.mark.parametrize("broker", ["namuh", "kiwoom"])
@pytest.mark.parametrize("side", ["buy", "sell"])
def test_shared_gateway_limit_and_market_order_mapping(setup, monkeypatch, broker, side):
    from types import SimpleNamespace

    gateway = TradeGateway(setup[0])
    calls = []

    def request(*args):
        calls.append(args)
        return {"Output_0": {"mkt_orr_no": "123"}, "ord_no": "123"}, {}

    monkeypatch.setattr(gateway, "request", request)
    plan = SimpleNamespace(broker=broker, code="373220")
    assert gateway.submit(None, None, plan, "synthetic", side, 1, limit_price=400000) == "123"
    body = calls[-1][4]
    if broker == "namuh":
        assert body["orr_pr"] == 400000 and body["nmn_pr_tp_cd"] == "01"
    else:
        assert body["ord_uv"] == "400000" and body["trde_tp"] == "0"
    gateway.submit(None, None, plan, "synthetic", side, 1)
    body = calls[-1][4]
    if broker == "namuh":
        assert body["orr_pr"] == 0 and body["nmn_pr_tp_cd"] == "05"
    else:
        assert body["ord_uv"] == "" and body["trde_tp"] == "3"


def test_broker_mock_routes_removed_and_live_lock_preserved(setup):
    config = setup[0].model_copy(update={"live_orders_enabled": False})
    app = create_app(config)
    paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert not any(path.startswith("/api/trading/mock") for path in paths)
    with TestClient(app, headers={"origin": ORIGIN}) as client:
        assert client.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE}).status_code == 200
        assert client.get("/api/trading/mock").status_code == 404
        assert client.post("/api/trading/mock/preview", json={}).status_code == 404
        assert client.get("/api/trading/paper").status_code == 200
    assert config.live_orders_enabled is False


def test_automatic_signal_conditions():
    value = {"active": True, "buy_rule": "price", "buy_price": 100, "take_profit": 5}

    def at(clock):
        return datetime.fromisoformat("2026-10-21T" + clock).replace(tzinfo=ZoneInfo("Asia/Seoul"))

    assert automatic_signal(value, at("09:00:00"), True, price=100) == "buy"
    assert automatic_signal(value, at("09:00:00"), True, price=99) == "buy"
    assert automatic_signal(value, at("09:00:00"), True, price=101) is None
    assert automatic_signal(value, at("09:00:00"), True, price=0) is None
    assert automatic_signal(value, at("09:00:00"), False, price=100) is None
    assert automatic_signal(value, at("08:59:59"), True, price=100) is None
    assert automatic_signal(value, at("09:00:00"), True, price=99, profit_rate=5) == "sell"
    assert automatic_signal(value, at("09:00:00"), True, profit_rate=5.01) == "sell"
    value.update(buy_rule="forecast", forecast_checkpoint={"date": "2026-10-21", "positive": True})
    assert automatic_signal(value, at("09:00:00"), True) is None  # Retired forecast entry.
    assert automatic_signal(value, at("09:01:00"), True) is None
    value["forecast_checkpoint"]["date"] = "2026-10-20"
    assert automatic_signal(value, at("09:00:00"), True) is None


@pytest.mark.parametrize("live", [False, True])
def test_plan_order_lock_idempotency_and_cash_basis(setup, monkeypatch, live):
    import nastack_trading.trading as trading

    monkeypatch.setattr(trading, "market_session", lambda: {"is_open": True})
    monkeypatch.setattr(TradeGateway, "validate_account", lambda *args: None)
    monkeypatch.setattr(TradeGateway, "capacity", lambda *args: (100, 10000, "live"))
    monkeypatch.setattr(TradeGateway, "quote", lambda *args: 100)
    calls = []

    def submit(*args, **kwargs):
        calls.append(args[-1])
        return "synthetic-order-1"

    monkeypatch.setattr(TradeGateway, "submit", submit)
    config = setup[0].model_copy(update={"live_orders_enabled": live})
    with TestClient(create_app(config), headers={"origin": ORIGIN}) as client:
        assert client.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE}).status_code == 200
        added = client.post(
            "/api/admin/instruments",
            json={
                "code": "123456",
                "name": "합성 시험 종목",
                "market": "KOSPI",
                "asset_type": "stock",
                "status": "active",
                "source_note": "synthetic",
            },
        )
        assert added.status_code == 201
        plan = client.put(
            "/api/trading/plans",
            json={"code": "123456", "broker": "kiwoom", "fraction": 50},
        )
        assert plan.status_code == 200, plan.text
        plan = plan.json()
        assert plan["repeat_trading"] is False
        assert (
            next(p for p in client.get("/api/trading/plans").json()["items"] if p["id"] == plan["id"])[
                "repeat_trading"
            ]
            is False
        )
        assert "account_ciphertext" not in plan
        assert (
            client.put("/api/trading/plans", json={"code": "123456", "broker": "kiwoom"}).status_code == 409
        )
        active = client.put(
            "/api/trading/plans",
            json={
                "code": "123456",
                "broker": "kiwoom",
                "version": 1,
                "mode": "automatic",
                "active": True,
                "confirm_auto": True,
                "buy_rule": "forecast",
            },
        )
        assert active.status_code == 422  # Forecast API entry is removed.
        payload = {
            "plan_id": plan["id"],
            "side": "buy",
            "request_key": "synthetic-unique-request",
            **({"limit_price": 100} if live else {}),
        }
        preview = client.post("/api/trading/orders/preview", json=payload)
        assert preview.status_code == 200, preview.text
        assert preview.json()["quantity"] == 50
        duplicate = client.post("/api/trading/orders/preview", json=payload)
        assert duplicate.json()["id"] == preview.json()["id"]
        assert not calls
        intent = {"intent_id": preview.json()["id"], "confirmation": "실전 주문 확인"}
        current_safety = client.get("/api/trading/safety").json()
        assert (
            client.put(
                "/api/trading/safety",
                json={
                    "version": current_safety["version"],
                    "new_orders_locked": False,
                    "confirmation": "신규 주문 잠금 해제 확인",
                },
            ).status_code
            == 200
        )
        sent = client.post("/api/trading/orders/confirm", json=intent)
        if not live:
            assert sent.status_code == 409 and not calls
        else:
            assert sent.status_code == 200, sent.text
            assert sent.json()["status"] == "accepted"
            assert sent.json()["notification"] == "not_sent"
            assert client.post("/api/trading/orders/confirm", json=intent).json()["id"] == sent.json()["id"]
            assert calls == [50]
