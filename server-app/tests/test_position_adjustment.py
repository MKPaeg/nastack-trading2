"""Explicit position adjustments coexist with, but never impersonate, automatic orders."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
import test_live_worker
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from nastack_trading.models import RealizedSale, TradeOrder, TradePlan, User, UserSettings
from nastack_trading.trading import OrderPreview, PlanInput, PlanSettingUpdate


@pytest.fixture
def runner(setup, monkeypatch):
    return test_live_worker.runner.__wrapped__(setup, monkeypatch)


def adjustment(service, db, user, pid, side, quantity, price=None):
    return service.preview(
        db,
        user,
        OrderPreview(
            plan_id=pid, side=side, quantity=quantity, limit_price=price, purpose="position_adjustment"
        ),
    )


@pytest.mark.parametrize("role", ["admin", "trader"])
def test_adjust_buy_sell_keeps_running_round_updates_broker_basis_and_profit(runner, role, monkeypatch):
    worker, gateway, factory, uid, tid, pid = runner
    gateway.held = 5
    basis = [100.0]
    original = gateway.submit

    def submit(db, user, plan, account, side, quantity, **kwargs):
        previous = gateway.held
        result = original(db, user, plan, account, side, quantity, **kwargs)
        if side == "buy":
            basis[0] = (previous * basis[0] + quantity * gateway.price) / gateway.held
        return result

    monkeypatch.setattr(gateway, "submit", submit)
    monkeypatch.setattr(
        gateway,
        "position",
        lambda *args: {"quantity": gateway.held, "purchase_price": basis[0], "environment": "live"},
    )
    with factory() as db:
        user = db.get(User, uid)
        user.role = role
        plan = db.get(TradePlan, pid)
        plan.value = {**plan.value, "take_profit": 15, "buy_enabled": False}
        db.commit()
        gateway.price = 110
        intent = adjustment(worker.service, db, user, pid, "buy", 5, 110)
        assert gateway.wire == []  # Preview never sends.
        order = worker.service.execute(db, user, intent.id)
        assert order.status == "accepted"
        worker.service.reconcile(db, user, order.id)
        assert gateway.held == 10 and basis[0] == 105
        assert db.get(TradePlan, pid).value["active"]
        assert plan.value["auto_control"]["generation"] == "test-run"
        sale = adjustment(worker.service, db, user, pid, "sell", 3)
        order = worker.service.execute(db, user, sale.id)
        worker.service.reconcile(db, user, order.id)
        row = db.scalar(select(RealizedSale).where(RealizedSale.order_id == order.id))
        assert row.profit == 15 and gateway.held == 7 and basis[0] == 105
        assert order.value["pre_sale_quantity"] == 10
        assert plan.value["active"]
    worker.tick()
    assert len(gateway.wire) == 2  # Partial manual exit does not stop monitoring or rebuy.
    gateway.price = 121
    worker.tick()
    assert gateway.wire[-1][2:4] == ("sell", 7)  # New broker basis, not old 100, and all remaining shares.


def test_manual_confirmation_reserves_automatic_order_until_dismissed(runner):
    worker, gateway, factory, uid, tid, pid = runner
    with factory() as db:
        user = db.get(User, uid)
        intent = adjustment(worker.service, db, user, pid, "buy", 2, 100)
        identity = intent.id
    worker.tick()
    assert not gateway.wire
    with factory() as db:
        intent = db.get(TradeOrder, identity)
        intent.value = {
            **intent.value,
            "expires_at": (test_live_worker.AT - timedelta(seconds=1)).isoformat(),
        }
        db.commit()
    worker.tick()
    assert len(gateway.wire) == 1  # Expired previews do not permanently pause monitoring.


def test_fresh_position_recheck_blocks_automatic_buy_after_monitor_race(runner):
    worker, gateway, factory, uid, tid, pid = runner
    with factory() as db:
        user = db.get(User, uid)
        intent = worker.service.preview(
            db, user, OrderPreview(plan_id=pid, side="buy", quantity=1, limit_price=100), automatic=True
        )
        gateway.held = 2  # Another confirmed trade appeared after the monitor/preview.
        with pytest.raises(HTTPException) as error:
            worker.service.execute(db, user, intent.id)
        assert "보유분" in error.value.detail
        assert not gateway.wire


def test_adjustment_blocks_preexisting_automatic_intent_at_final_gate(runner):
    worker, gateway, factory, uid, tid, pid = runner
    with factory() as db:
        user = db.get(User, uid)
        auto = worker.service.preview(
            db, user, OrderPreview(plan_id=pid, side="buy", quantity=1, limit_price=100), automatic=True
        )
        adjustment(worker.service, db, user, pid, "buy", 2, 100)
        with pytest.raises(HTTPException) as error:
            worker.service.final_gate(db, user, auto)
        assert error.value.status_code == 409
        assert not gateway.wire


def test_manual_full_exit_stops_without_automatic_reentry(runner):
    worker, gateway, factory, uid, tid, pid = runner
    gateway.held = 5
    with factory() as db:
        user = db.get(User, uid)
        intent = adjustment(worker.service, db, user, pid, "sell", 5)
        order = worker.service.execute(db, user, intent.id)
        worker.service.reconcile(db, user, order.id)
    worker.tick()
    worker.tick()
    assert [o[2] for o in gateway.wire] == ["sell"]
    with factory() as db:
        assert not db.get(TradePlan, pid).value["active"]


def test_new_fill_bypasses_position_cache_cooldown_without_inventing_average(runner, monkeypatch):
    import nastack_trading.trade_performance as metrics
    from nastack_trading.models import BrokerSnapshot

    worker, gateway, factory, uid, tid, pid = runner
    gateway.held = 5
    stamp = test_live_worker.AT
    monkeypatch.setattr(metrics, "now", lambda: stamp + timedelta(seconds=2))
    with factory() as db:
        user = db.get(User, uid)
        first = metrics.performance(db, user, pid, worker.service, refresh=True)
        assert first["position"]["quantity"] == 5
        snapshot = db.scalar(select(BrokerSnapshot).where(BrokerSnapshot.kind == "trade_position"))
        snapshot.created_at = stamp
        db.add(
            TradeOrder(
                owner_id=uid,
                tenant_id=tid,
                plan_id=pid,
                status="filled",
                request_key="cache-filled-order",
                value={
                    "side": "buy",
                    "environment": "live",
                    "filled_quantity": 2,
                    "fill_price": 120,
                    "fill_checked_at": (stamp + timedelta(seconds=1)).isoformat(),
                },
            )
        )
        db.commit()
        old = metrics.performance(db, user, pid, worker.service)
        assert old["position"]["stale"] is True
        monkeypatch.setattr(
            gateway,
            "position",
            lambda *args: {"quantity": 7, "purchase_price": 105.714, "environment": "live"},
        )
        fresh = metrics.performance(db, user, pid, worker.service, refresh=True)
        assert fresh["position"]["quantity"] == 7
        assert fresh["position"]["purchase_price"] == 105.714


def test_adjustments_require_fresh_approval_cash_quantity_and_no_pending_order(runner):
    worker, gateway, factory, uid, tid, pid = runner
    gateway.held = 2
    with factory() as db:
        user = db.get(User, uid)
        for side, quantity, price in [("buy", 1, None), ("buy", 101, 100), ("sell", 3, None)]:
            with pytest.raises(HTTPException):
                adjustment(worker.service, db, user, pid, side, quantity, price)
            db.rollback()
        intent = adjustment(worker.service, db, user, pid, "buy", 1, 100)
        plan = db.get(TradePlan, pid)
        plan.version += 1
        db.commit()
        with pytest.raises(HTTPException):
            worker.service.execute(db, user, intent.id)
        assert not gateway.wire
        second = adjustment(worker.service, db, user, pid, "sell", 1)
        worker.service.execute(db, user, second.id)
        other = adjustment(worker.service, db, user, pid, "sell", 1)
        with pytest.raises(HTTPException):
            worker.service.execute(db, user, other.id)
        assert len(gateway.wire) == 1


def test_user_and_tenant_isolation_viewer_and_automatic_pause(runner):
    worker, gateway, factory, uid, tid, pid = runner
    with factory() as db:
        user = db.get(User, uid)
        for stranger in [
            SimpleNamespace(id="other", tenant_id=tid, active=True, role="trader"),
            SimpleNamespace(id=uid, tenant_id="other", active=True, role="admin"),
        ]:
            with pytest.raises(HTTPException) as error:
                adjustment(worker.service, db, stranger, pid, "buy", 1, 100)
            assert error.value.status_code == 404
            db.rollback()
        user.role = "viewer"
        db.commit()
        with pytest.raises(HTTPException) as error:
            adjustment(worker.service, db, user, pid, "buy", 1, 100)
        assert error.value.status_code == 403
        user.role = "trader"
        settings = db.get(UserSettings, uid)
        settings.value = {**settings.value, "automatic_runtime": {"paused": True}}
        db.commit()
        intent = adjustment(worker.service, db, user, pid, "buy", 1, 100)
        assert (
            worker.service.execute(db, user, intent.id).status == "accepted"
        )  # Individual approval, not auto pause bypass.


@pytest.mark.parametrize(
    "payload",
    [{"repeat_trading": True}, {"buy_rule": "forecast"}, {"forecast_source": "daily"}, {"mode": "manual"}],
)
def test_retired_features_rejected_not_reactivated_by_old_clients(payload):
    with pytest.raises(ValidationError):
        PlanInput(code="034220", broker="kiwoom", **payload)
    for field, value in payload.items():
        with pytest.raises(ValidationError):
            PlanSettingUpdate(version=1, field=field, value=value)
