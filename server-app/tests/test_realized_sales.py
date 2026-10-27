from datetime import date, datetime
from types import SimpleNamespace

import pytest
import test_live_worker
from fastapi import HTTPException
from sqlalchemy import func, select

from nastack_trading.models import AuditEvent, RealizedSale, TradeOrder, TradePlan, User
from nastack_trading.realized_sales import exclude_missing_sale, realized_router, save_sale
from nastack_trading.trading import OrderPreview


@pytest.fixture
def runner(setup, monkeypatch):
    return test_live_worker.runner.__wrapped__(setup, monkeypatch)


def sell(db, uid, tid, pid, key="sell", basis=100, quantity=2, price=110, at="2026-10-23T01:00:00+00:00"):
    order = TradeOrder(
        owner_id=uid,
        tenant_id=tid,
        plan_id=pid,
        request_key=key,
        status="partial",
        created_at=datetime.fromisoformat(at),
        value={
            "side": "sell",
            "broker": "kiwoom",
            "code": "034220",
            "environment": "live",
            "purpose": "manual",
            "quantity": 10,
            "filled_quantity": quantity,
            "fill_price": price,
            "sale_cost_basis": basis,
            "name": "Synthetic",
        },
    )
    db.add(order)
    db.flush()
    return order


def test_partial_cumulative_ledger_idempotent_and_audited(runner):
    _, _, factory, uid, tid, pid = runner
    with factory() as db:
        user = db.get(User, uid)
        order = sell(db, uid, tid, pid)
        row = save_sale(db, user, order)
        db.commit()
        assert row.profit == 20
        for _ in range(3):
            save_sale(db, user, order)
            db.commit()
        assert db.scalar(select(func.count()).select_from(RealizedSale)) == 1
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "trading.realized_updated")
            )
            == 1
        )
        order.value = {**order.value, "filled_quantity": 5, "fill_price": 112}
        row = save_sale(db, user, order)
        db.commit()
        assert row.quantity == 5 and row.profit == 60 and row.cost == 500
        order.status = "canceled"  # Remaining shares canceled: confirmed fills still count.
        save_sale(db, user, order)
        db.commit()
        assert db.scalar(select(func.count()).select_from(RealizedSale)) == 1
        order.value = {**order.value, "filled_quantity": 1}
        with pytest.raises(HTTPException):
            save_sale(db, user, order)


def test_excluded_missing_history_stays_out_of_report_and_sync(runner):
    _, _, factory, uid, tid, pid = runner
    report = next(r.endpoint for r in realized_router(None, None).routes if r.path == "/api/trading/realized")
    with factory() as db:
        user = db.get(User, uid)
        order = sell(db, uid, tid, pid, basis=None)
        order.status = "filled"
        row = save_sale(db, user, order)
        db.commit()
        with pytest.raises(HTTPException):
            exclude_missing_sale(db, SimpleNamespace(id="foreign", tenant_id=tid), row.id)
        exclude_missing_sale(db, user, row.id)
        db.commit()
        assert save_sale(db, user, order) is None
        result = report(
            start=date(2026, 1, 1),
            end=date(2026, 12, 31),
            period="day",
            broker="all",
            code=None,
            user=user,
            db=db,
        )
        assert result["items"] == [] and result["summary"]["missing"] == 0
        assert db.get(TradeOrder, order.id).status == "filled"


@pytest.mark.parametrize("reason", ["take_profit", "stop_loss", "manual"])
def test_reason_loss_and_zero_profit(runner, reason):
    _, _, factory, uid, tid, pid = runner
    with factory() as db:
        user = db.get(User, uid)
        order = sell(db, uid, tid, pid, price=95 if reason == "stop_loss" else 100)
        order.value = {
            **order.value,
            "purpose": "manual" if reason == "manual" else "automatic",
            "auto_exit_reason": reason,
        }
        row = save_sale(db, user, order)
        db.commit()
        assert row.value["reason"] == reason
        assert row.profit == (-10 if reason == "stop_loss" else 0)


def test_report_weighted_periods_missing_basis_and_owner_isolation(runner):
    _, _, factory, uid, tid, pid = runner
    routes = realized_router(None, None).routes
    report = next(r.endpoint for r in routes if r.path == "/api/trading/realized")
    with factory() as db:
        user = db.get(User, uid)
        # UTC date differs: attribution must use KST order day, not detection time.
        a = sell(db, uid, tid, pid, key="a", quantity=1, price=110, at="2025-12-31T16:00:00+00:00")
        b = sell(db, uid, tid, pid, key="b", quantity=9, price=90, at="2026-01-02T01:00:00+00:00")
        save_sale(db, user, a)
        save_sale(db, user, b)
        db.commit()
        args = dict(start=date(2026, 1, 1), end=date(2026, 12, 31), broker="all", code=None, user=user, db=db)
        for period in ["day", "week", "month", "year"]:
            result = report(period=period, **args)
            assert result["summary"]["profit"] == -80
            assert result["summary"]["return_rate"] == -8
            assert result["stocks"][0]["profit"] == -80
            assert result["stocks"][0]["return_rate"] == -8
            assert result["stocks"][0]["sales"] == 2
            assert result["stocks"][0]["cost"] == 1000
            assert len(result["groups"]) == (2 if period == "day" else 1)
        assert report(period="day", **args)["items"][-1]["day"] == "2026-01-01"
        assert not report(period="day", **{**args, "broker": "namuh"})["items"]
        for other in [
            SimpleNamespace(id="other", tenant_id=tid),
            SimpleNamespace(id=uid, tenant_id="foreign"),
        ]:
            assert not report(period="day", **{**args, "user": other})["items"]
            assert not report(period="day", **{**args, "user": other})["stocks"]
            with pytest.raises(HTTPException):
                save_sale(db, other, a)
        missing = sell(db, uid, tid, pid, key="missing", basis=None, at="2026-01-03T01:00:00+00:00")
        save_sale(db, user, missing)
        db.commit()
        result = report(period="year", **args)
        assert result["summary"]["profit"] is None
        assert result["summary"]["known_profit"] == -80
        assert result["summary"]["missing"] == 1
        assert result["stocks"][0]["profit"] is None
        assert result["stocks"][0]["cost"] is None
        assert result["stocks"][0]["known_profit"] == -80
        assert result["stocks"][0]["return_rate"] is None


def test_manual_sell_captures_average_before_wire_and_books_confirmed_fill(runner):
    worker, gateway, factory, uid, tid, pid = runner
    gateway.held = 10
    gateway.price = 110
    with factory() as db:
        plan = db.get(TradePlan, pid)
        plan.value = {**plan.value, "active": False, "mode": "manual"}
        db.commit()
        user = db.get(User, uid)
        intent = worker.service.preview(db, user, OrderPreview(plan_id=pid, side="sell", quantity=3))
        order = worker.service.execute(db, user, intent.id)
        assert order.value["sale_cost_basis"] == 100
        assert not db.scalar(select(RealizedSale))  # Acceptance is not a fill.
        worker.service.reconcile(db, user, order.id)
        assert db.scalar(select(RealizedSale)).profit == 30


@pytest.mark.parametrize("price,reason,profit", [(105, "take_profit", 5), (95, "stop_loss", -5)])
def test_worker_exit_books_confirmed_profit_and_loss(runner, price, reason, profit):
    worker, gateway, factory, uid, tid, pid = runner
    with factory() as db:
        plan = db.get(TradePlan, pid)
        plan.value = {**plan.value, "stop_loss_enabled": True, "stop_loss_rate": 5}
        db.commit()
    worker.tick()
    worker.tick()
    gateway.price = price
    worker.tick()  # Accepted sell is not yet realized.
    with factory() as db:
        assert db.scalar(select(RealizedSale)) is None
    worker.tick()
    with factory() as db:
        row = db.scalar(select(RealizedSale))
        assert row.profit == profit
        assert row.value["reason"] == reason


def test_sync_readonly_viewer_and_authentication(admin, setup):
    assert admin.get("/api/trading/realized?start=2026-01-01&end=2026-12-31&period=month").status_code == 200
    assert admin.get("/api/trading/realized?start=2026-12-31&end=2026-01-01").status_code == 422
    assert admin.post("/api/trading/realized/sync", json={"owner_id": "other"}).json() == {"sales": 0}
    with setup[1]() as db:
        user = db.scalar(select(User))
        user.role = "viewer"
        db.commit()
    from conftest import CODE, EMAIL

    assert admin.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE}).status_code == 200
    assert admin.get("/api/trading/realized?start=2026-01-01&end=2026-12-31").status_code == 200
    assert admin.post("/api/trading/realized/sync").status_code == 403


def test_stock_summary_separates_codes_filters_and_excluded_records(runner):
    _, _, factory, uid, tid, pid = runner
    report = next(r.endpoint for r in realized_router(None, None).routes if r.path == "/api/trading/realized")
    with factory() as db:
        user = db.get(User, uid)
        for key, stock, broker, basis, price in [
            ("gain", "034220", "kiwoom", 100, 110),
            ("loss", "034220", "namuh", 100, 90),
            ("unknown", "005930", "kiwoom", None, 110),
            ("excluded", "034220", "kiwoom", None, 110),
            ("old", "034220", "kiwoom", 100, 200),
        ]:
            order = sell(
                db,
                uid,
                tid,
                pid,
                key=key,
                basis=basis,
                price=price,
                at="2025-01-01T01:00:00+00:00" if key == "old" else "2026-10-23T01:00:00+00:00",
            )
            order.value = {**order.value, "code": stock, "broker": broker}
            order.status = "filled"
            row = save_sale(db, user, order)
            if key == "excluded":
                db.flush()
                exclude_missing_sale(db, user, row.id)
        db.commit()
        args = dict(
            start=date(2026, 1, 1),
            end=date(2026, 12, 31),
            period="month",
            broker="all",
            code=None,
            user=user,
            db=db,
        )
        result = report(**args)
        stocks = {s["code"]: s for s in result["stocks"]}
        assert stocks["034220"]["profit"] == 0
        assert stocks["034220"]["return_rate"] == 0
        assert stocks["034220"]["sales"] == 2
        assert stocks["034220"]["quantity"] == 4
        assert stocks["034220"]["proceeds"] == stocks["034220"]["cost"] == 400
        assert stocks["005930"]["profit"] is None
        assert stocks["005930"]["missing"] == 1
        filtered = report(**{**args, "code": "034220", "broker": "namuh"})
        assert len(filtered["stocks"]) == 1
        assert filtered["stocks"][0]["profit"] == -20
        assert report(**{**args, "code": "999999"})["stocks"] == []
