"""Owner-scoped pause and exit-only monitoring, using synthetic broker orders only."""

import pytest
import test_live_worker
from fastapi import HTTPException
from sqlalchemy import select

from nastack_trading.models import Tenant, TradeOrder, TradePlan, User, UserSettings
from nastack_trading.trade_safety import RuntimeUpdate, automatic_runtime, save_automatic_runtime
from nastack_trading.trading import PlanInput, PlanSettingUpdate, update_plan_setting


@pytest.fixture
def runner(setup, monkeypatch):
    return test_live_worker.runner.__wrapped__(setup, monkeypatch)


def pause(factory, uid, paused):
    with factory() as db:
        user = db.get(User, uid)
        value = automatic_runtime(db, user)
        return save_automatic_runtime(db, user, RuntimeUpdate(version=value["version"], paused=paused))


def test_pause_reconciles_and_resume_preserves_round(runner):
    worker, gateway, factory, uid, tid, pid = runner
    worker.tick()
    assert len(gateway.wire) == 1
    pause(factory, uid, True)
    gateway.price = 110
    worker.tick()
    assert len(gateway.wire) == 1
    with factory() as db:
        assert db.scalar(select(TradeOrder)).status == "filled"
        plan = db.get(TradePlan, pid)
        assert plan.value["active"]
        assert plan.value["auto_control"]["generation"] == "test-run"
    pause(factory, uid, False)
    worker.tick()
    assert [o[2] for o in gateway.wire] == ["buy", "sell"]


@pytest.mark.parametrize("price", [110, 90])
def test_exit_only_existing_position_no_round_buy(runner, price):
    worker, gateway, factory, uid, tid, pid = runner
    with factory() as db:
        plan = db.get(TradePlan, pid)
        plan.value = {
            **plan.value,
            "buy_enabled": False,
            "buy_price": None,
            "entry_policy": "buy_then_sell_all",
            "stop_loss_enabled": True,
            "stop_loss_rate": 5,
        }
        db.commit()
    gateway.held = 7
    worker.tick()
    assert not gateway.wire
    gateway.price = price
    worker.tick()
    assert len(gateway.wire) == 1
    assert gateway.wire[0][2:4] == ("sell", 7)


def test_buy_disabled_validates_and_saves_immediately(runner):
    worker, gateway, factory, uid, tid, pid = runner
    PlanInput(
        code="034220",
        broker="kiwoom",
        mode="automatic",
        active=True,
        confirm_auto=True,
        buy_enabled=False,
        buy_price=None,
    )
    with factory() as db:
        user = db.get(User, uid)
        plan = db.get(TradePlan, pid)
        update_plan_setting(
            db, user, pid, PlanSettingUpdate(version=plan.version, field="buy_enabled", value=0)
        )
    worker.tick()
    assert not gateway.wire


def test_pause_is_final_wire_gate(runner):
    worker, gateway, factory, uid, tid, pid = runner

    def change(db, user, plan):
        save_automatic_runtime(db, user, RuntimeUpdate(version=0, paused=True))

    gateway.change = change
    worker.tick()
    assert not gateway.wire


def test_pause_cas_merge_permissions_and_owner_isolation(runner):
    worker, gateway, factory, uid, tid, pid = runner
    pause(factory, uid, True)
    with factory() as db:
        user = db.get(User, uid)
        assert db.get(UserSettings, uid).value["trade_safety"]["new_orders_locked"] is False
        with pytest.raises(HTTPException) as error:
            save_automatic_runtime(db, user, RuntimeUpdate(version=0, paused=False))
        assert error.value.status_code == 409
        db.rollback()
        for tenant in [tid, "different-tenant"]:
            # Reads must not accidentally return another tenant's settings even for a matching ID.
            from types import SimpleNamespace

            other = SimpleNamespace(id=uid if tenant != tid else "other-user", tenant_id=tenant)
            assert automatic_runtime(db, other) == {"version": 0, "paused": False}
        user.role = "viewer"
        db.commit()
        with pytest.raises(HTTPException) as error:
            save_automatic_runtime(db, user, RuntimeUpdate(version=1, paused=False))
        assert error.value.status_code == 403


def test_actual_users_have_independent_pause_and_persisted_state(runner):
    worker, gateway, factory, uid, tid, pid = runner
    with factory() as db:
        tenant = Tenant(name="Other", timezone="Asia/Seoul")
        db.add(tenant)
        db.flush()
        users = [
            User(tenant_id=t, email=f"user{i}@example.com", role="trader", code_hash="test")
            for i, t in enumerate([tid, tenant.id])
        ]
        db.add_all(users)
        db.commit()
        ids = [u.id for u in users]
        for user in users:
            save_automatic_runtime(db, user, RuntimeUpdate(version=0, paused=True))
    with factory() as db:
        assert not automatic_runtime(db, db.get(User, uid))["paused"]
        for other_id in ids:
            user = db.get(User, other_id)
            assert automatic_runtime(db, user)["paused"]
            save_automatic_runtime(db, user, RuntimeUpdate(version=1, paused=False))
    worker.tick()
    assert len(gateway.wire) == 1


def test_buy_option_checked_again_at_wire(runner):
    worker, gateway, factory, uid, tid, pid = runner

    def change(db, user, plan):
        plan.value = {**plan.value, "buy_enabled": False}
        db.commit()

    gateway.change = change
    worker.tick()
    assert not gateway.wire


def test_runtime_api_scoped_payload(admin):
    before = admin.get("/api/trading/automatic-runtime").json()
    response = admin.put("/api/trading/automatic-runtime", json={**before, "paused": True})
    assert response.status_code == 200
    assert admin.get("/api/trading/automatic-runtime").json()["paused"] is True
    assert admin.put("/api/trading/automatic-runtime", json={**before, "paused": False}).status_code == 409
    assert (
        admin.put(
            "/api/trading/automatic-runtime", json={"version": 1, "paused": False, "user_id": "other"}
        ).status_code
        == 422
    )
