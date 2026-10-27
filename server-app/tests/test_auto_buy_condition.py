from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from test_live_worker import runner  # noqa: F401
from nastack_trading.models import TradePlan, User
from nastack_trading.trading import OrderPreview, PlanControl, PlanInput, PlanSettingUpdate, TradingService, automatic_signal, control_plan, update_plan_setting


AT = datetime.fromisoformat("2026-10-22T10:05:00+09:00")


@pytest.mark.parametrize("condition,price,expected", [
    ("at_or_below", 49999, "buy"), ("at_or_below", 50000, "buy"), ("at_or_below", 50001, None),
    ("at_or_above", 49999, None), ("at_or_above", 50000, "buy"), ("at_or_above", 50001, "buy"),
    ("invalid", 50000, None), ("at_or_above", float("nan"), None), ("at_or_above", 0, None),
])
def test_threshold_is_inclusive_and_legacy_default_is_unchanged(condition, price, expected):
    value = {"active": True, "buy_rule": "price", "buy_price": 50000, "buy_condition": condition, "take_profit": 5}
    assert automatic_signal(value, AT, True, price=price) == expected
    value.pop("buy_condition")
    assert automatic_signal(value, AT, True, price=50000) == "buy"
    assert automatic_signal(value, AT, True, price=50001) is None
    assert automatic_signal(value, AT, False, price=50000) is None


@pytest.mark.parametrize("price", [100, 101])
def test_above_strategy_sends_market_only_after_explicit_approval(runner, price):  # noqa: F811
    worker, gateway, factory, uid, tid, pid = runner
    with factory.begin() as db:
        plan = db.get(TradePlan, pid)
        plan.value = {**plan.value, "buy_condition": "at_or_above", "auto_market_buy_above": True}
    gateway.price = 99
    worker.tick()
    assert gateway.wire == []
    gateway.price = price
    worker.tick()
    assert gateway.wire == [(uid, "034220", "buy", 1, None)]
    worker.tick()
    worker.tick()
    assert len(gateway.wire) == 1  # No automatic additional buy.


def test_unapproved_market_and_manual_market_still_blocked(runner):  # noqa: F811
    worker, gateway, factory, uid, tid, pid = runner
    with factory.begin() as db:
        plan = db.get(TradePlan, pid)
        plan.value = {**plan.value, "buy_condition": "at_or_above"}
    worker.tick()
    assert gateway.wire == []
    with factory() as db:
        user = db.get(User, uid)
        with pytest.raises(HTTPException) as error:
            TradingService(worker.config, gateway).preview(db, user, OrderPreview(plan_id=pid, side="buy", quantity=1))
        assert error.value.status_code == 422


def test_change_stops_and_start_reauthorizes_without_sending_an_order(runner):  # noqa: F811
    worker, gateway, factory, uid, tid, pid = runner
    with factory() as db:
        user, plan = db.get(User, uid), db.get(TradePlan, pid)
        saved = update_plan_setting(db, user, pid, PlanSettingUpdate(version=plan.version, field="buy_condition", value="at_or_above"))
        assert saved["active"] is False and saved["auto_market_buy_above"] is False
        assert saved["buy_price"] == 100
        generation = plan.value["auto_control"]["generation"]
        approved = control_plan(db, user, pid, PlanControl(version=plan.version, action="start", confirmation="실전 자동매매 시작 확인"), worker.config)
        assert approved["auto_market_buy_above"] is True and approved["active"] is True
        assert approved["auto_control"]["generation"] != generation
    assert gateway.wire == []


def test_price_falls_below_threshold_before_transmission(runner, monkeypatch):  # noqa: F811
    worker, gateway, factory, uid, tid, pid = runner
    with factory.begin() as db:
        plan = db.get(TradePlan, pid)
        plan.value = {**plan.value, "buy_condition": "at_or_above", "auto_market_buy_above": True}
    quotes = iter([101, 101, 99])
    monkeypatch.setattr(gateway, "quote", lambda *args: next(quotes, 99))
    worker.tick()
    assert gateway.wire == []


def test_condition_validation_and_unchanged_sell_signals():
    with pytest.raises(ValidationError):
        PlanInput(code="123456", broker="namuh", buy_condition="anything")
    with pytest.raises(ValidationError):
        PlanSettingUpdate(version=1, field="buy_condition", value="anything")
    value = {"active": True, "buy_rule": "price", "buy_price": 50000, "buy_condition": "at_or_above", "take_profit": 5, "stop_loss_enabled": True, "stop_loss_rate": 10}
    assert automatic_signal(value, AT, True, price=55000, profit_rate=5) == "sell"
    assert automatic_signal(value, AT, True, price=45000, profit_rate=-10) == "sell"


@pytest.mark.parametrize("field,value", [("buy_condition", "at_or_above"), ("manual_buy_price", 125)])
def test_new_settings_are_owner_scoped_and_viewer_cannot_write(runner, field, value):  # noqa: F811
    worker, gateway, factory, uid, tid, pid = runner
    with factory() as db:
        plan = db.get(TradePlan, pid)
        for tenant in [tid, "foreign-tenant"]:
            stranger = SimpleNamespace(id="foreign-user", tenant_id=tenant, role="trader", active=True)
            with pytest.raises(HTTPException) as error:
                update_plan_setting(db, stranger, pid, PlanSettingUpdate(version=plan.version, field=field, value=value))
            assert error.value.status_code == 404
        user = db.get(User, uid)
        user.role = "viewer"
        with pytest.raises(HTTPException) as error:
            update_plan_setting(db, user, pid, PlanSettingUpdate(version=plan.version, field=field, value=value))
        assert error.value.status_code == 403
    assert gateway.wire == []
