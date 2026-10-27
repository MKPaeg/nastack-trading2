from datetime import timedelta

import pytest
import test_live_worker
from fastapi import HTTPException
from sqlalchemy import select

from nastack_trading.db import now
from nastack_trading.models import AuthSession, Tenant, TradePlan, User, UserSettings
from nastack_trading.trade_safety import (
    ServerSafetyUpdate,
    ensure_new_order_allowed,
    safety,
    save_server_safety,
    server_safety,
)


@pytest.fixture
def runner(setup, monkeypatch):
    return test_live_worker.runner.__wrapped__(setup, monkeypatch)


def test_admin_global_flags_persist_and_automatic_only_allows_manual(runner):
    worker, gateway, factory, uid, tid, pid = runner
    with factory() as db:
        user = db.get(User, uid)
        result = save_server_safety(
            db, user, worker.config, ServerSafetyUpdate(version=0, field="automatic_locked", locked=True)
        )
        assert result["automatic_locked"] and not result["server_locked"]
        ensure_new_order_allowed(db, user, worker.config)
        with pytest.raises(HTTPException):
            worker.service.check_automatic(db, user, db.get(TradePlan, pid))
    worker.tick()
    assert not gateway.wire
    with factory() as db:
        user = db.get(User, uid)
        assert safety(db, user, worker.config)["automatic_locked"]
        save_server_safety(
            db,
            user,
            worker.config,
            ServerSafetyUpdate(
                version=1,
                field="automatic_locked",
                locked=False,
                confirmation="전체 사용자 주문 차단 해제 확인",
            ),
        )
    worker.tick()
    assert len(gateway.wire) == 1


@pytest.mark.parametrize("role", ["trader", "viewer"])
def test_non_admin_cannot_write_global_flags(runner, role):
    worker, gateway, factory, uid, tid, pid = runner
    with factory() as db:
        user = db.get(User, uid)
        user.role = role
        db.commit()
        with pytest.raises(HTTPException) as error:
            save_server_safety(
                db, user, worker.config, ServerSafetyUpdate(version=0, field="server_locked", locked=True)
            )
        assert error.value.status_code == 403
        assert not server_safety(db, worker.config)["server_locked"]


def test_server_lock_blocks_before_wire(runner):
    worker, gateway, factory, uid, tid, pid = runner

    def change(db, user, plan):
        save_server_safety(
            db, user, worker.config, ServerSafetyUpdate(version=0, field="server_locked", locked=True)
        )

    gateway.change = change
    worker.tick()
    assert not gateway.wire
    with factory() as db:
        with pytest.raises(HTTPException):
            ensure_new_order_allowed(db, db.get(User, uid), worker.config)


def test_global_scope_keeps_other_users_personal_options_intact(runner):
    worker, gateway, factory, uid, tid, pid = runner
    with factory() as db:
        tenant = Tenant(name="Other", timezone="Asia/Seoul")
        db.add(tenant)
        db.flush()
        users = [
            User(tenant_id=t, email=f"scope{i}@example.com", role="trader", code_hash="test")
            for i, t in enumerate([tid, tenant.id])
        ]
        db.add_all(users)
        db.flush()
        for user in users:
            db.add(
                UserSettings(
                    user_id=user.id,
                    tenant_id=user.tenant_id,
                    value={
                        "trade_safety": {"new_orders_locked": False},
                        "automatic_runtime": {"paused": True},
                    },
                )
            )
        db.commit()
        save_server_safety(
            db,
            db.get(User, uid),
            worker.config,
            ServerSafetyUpdate(version=0, field="server_locked", locked=True),
        )
        for user in users:
            policy = safety(db, user, worker.config)
            assert policy["server_locked"]
            assert not policy["new_orders_locked"]
            assert policy["automatic_paused"]
            with pytest.raises(HTTPException):
                ensure_new_order_allowed(db, user, worker.config)


def test_cas_confirmation_and_deployment_lock_cannot_be_bypassed(runner):
    worker, gateway, factory, uid, tid, pid = runner
    with factory() as db:
        user = db.get(User, uid)
        save_server_safety(
            db, user, worker.config, ServerSafetyUpdate(version=0, field="server_locked", locked=True)
        )
        for payload, config, status in [
            (ServerSafetyUpdate(version=0, field="server_locked", locked=True), worker.config, 409),
            (ServerSafetyUpdate(version=1, field="server_locked", locked=False), worker.config, 422),
            (
                ServerSafetyUpdate(
                    version=1,
                    field="server_locked",
                    locked=False,
                    confirmation="전체 사용자 주문 차단 해제 확인",
                ),
                worker.config.model_copy(update={"live_orders_enabled": False}),
                409,
            ),
        ]:
            with pytest.raises(HTTPException) as error:
                save_server_safety(db, user, config, payload)
            assert error.value.status_code == status
            db.rollback()
        assert server_safety(db, worker.config)["server_locked"]


def test_admin_api_and_non_admin_denial(admin, setup):
    assert (
        admin.put(
            "/api/trading/server-safety", json={"version": 0, "field": "server_locked", "locked": True}
        ).status_code
        == 200
    )
    assert admin.get("/api/trading/safety").json()["server_locked"] is True
    with setup[1]() as db:
        user = db.scalar(select(User))
        user.role = "trader"
        for session in db.scalars(select(AuthSession)):
            session.expires_at = now() + timedelta(hours=1)
        db.commit()
    assert (
        admin.put(
            "/api/trading/server-safety", json={"version": 1, "field": "server_locked", "locked": True}
        ).status_code
        == 403
    )
