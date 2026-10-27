from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from nastack_trading.models import Tenant, User, UserSettings
from nastack_trading.trade_safety import SafetyUpdate, ensure_new_order_allowed, safety, save_safety


def test_personal_safety_defaults_confirmation_scope_and_merge(setup):
    config, factory = setup
    with factory() as db:
        owner = db.scalar(select(User))
        settings = db.get(UserSettings, owner.id)
        if not settings:
            settings = UserSettings(user_id=owner.id, tenant_id=owner.tenant_id, value={})
            db.add(settings)
        settings.value = {**settings.value, "retained": {"setting": 1}}
        tenant = Tenant(name="Other", timezone="Asia/Seoul")
        db.add(tenant)
        db.flush()
        same = User(
            tenant_id=owner.tenant_id, email="other@example.com", role="trader", active=True, code_hash="test"
        )
        other = User(
            tenant_id=tenant.id, email="foreign@example.com", role="trader", active=True, code_hash="test"
        )
        db.add_all([same, other])
        db.commit()
        assert safety(db, owner, config)["new_orders_locked"]
        with pytest.raises(HTTPException):
            ensure_new_order_allowed(db, owner, config)
        with pytest.raises(HTTPException):
            save_safety(db, owner, config, SafetyUpdate(version=0, new_orders_locked=False))
        db.rollback()
        changed = save_safety(
            db,
            owner,
            config,
            SafetyUpdate(version=0, new_orders_locked=False, confirmation="신규 주문 잠금 해제 확인"),
        )
        assert changed["server_locked"] and changed["automatic_locked"]
        assert safety(db, same, config)["new_orders_locked"]
        assert safety(db, other, config)["new_orders_locked"]
        assert db.get(UserSettings, owner.id).value["retained"] == {"setting": 1}
        with pytest.raises(HTTPException):
            save_safety(db, owner, config, SafetyUpdate(version=0, new_orders_locked=True))
        db.rollback()
        forged = SimpleNamespace(id=owner.id, tenant_id=other.tenant_id, active=True, role="trader")
        assert safety(db, forged, config)["new_orders_locked"]
        with pytest.raises(HTTPException):
            save_safety(db, forged, config, SafetyUpdate(version=1, new_orders_locked=True))
        db.rollback()
        owner.role = "viewer"
        with pytest.raises(HTTPException):
            save_safety(db, owner, config, SafetyUpdate(version=1, new_orders_locked=True))
        db.rollback()
        value = save_safety(db, owner, config, SafetyUpdate(version=1, new_orders_locked=True))
        assert value["new_orders_locked"]
        with pytest.raises(HTTPException):
            ensure_new_order_allowed(db, owner, config)
