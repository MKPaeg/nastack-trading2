from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from nastack_trading.models import TradePlan, User
from nastack_trading.trading import PlanControl, control_plan


def test_autosave_preserves_round_credentials_and_owner_scope(setup):
    from nastack_trading.trading import PlanSettingUpdate, update_plan_setting

    with setup[1]() as db:
        user = db.scalar(select(User))
        row = TradePlan(
            owner_id=user.id,
            tenant_id=user.tenant_id,
            code="046120",
            broker="kiwoom",
            version=1,
            value={
                "mode": "automatic",
                "active": True,
                "buy_rule": "price",
                "buy_price": 3700,
                "auto_control": {"generation": "existing-round", "state": "running"},
                "auto_authorization": {"key_version": 2},
                "account_ciphertext": "retained",
            },
        )
        db.add(row)
        db.commit()
        for field, value in [
            ("stop_loss_enabled", 1),
            ("buy_price", 3600),
            ("auto_max_quantity", 5),
            ("manual_sell_price", 3550),
        ]:
            saved = update_plan_setting(
                db, user, row.id, PlanSettingUpdate(version=row.version, field=field, value=value)
            )
            assert saved["active"] is True
            assert saved["auto_control"]["generation"] == "existing-round"
            assert row.value["auto_authorization"] == {"key_version": 2}
            assert row.value["account_ciphertext"] == "retained"
            assert "account_ciphertext" not in saved
        with pytest.raises(HTTPException) as error:
            update_plan_setting(db, user, row.id, PlanSettingUpdate(version=1, field="fraction", value=20))
        assert error.value.status_code == 409
        db.rollback()
        for tenant in [user.tenant_id, "other-tenant"]:
            stranger = SimpleNamespace(id="other", tenant_id=tenant, role="trader", active=True)
            with pytest.raises(HTTPException) as error:
                update_plan_setting(
                    db, stranger, row.id, PlanSettingUpdate(version=row.version, field="fraction", value=20)
                )
            assert error.value.status_code == 404
            db.rollback()
        for field, value in [
            ("mode", "manual"),
            ("repeat_trading", 1),
            ("buy_rule", "forecast"),
            ("forecast_source", "daily"),
        ]:
            with pytest.raises(ValidationError):
                PlanSettingUpdate(version=row.version, field=field, value=value)
        assert row.value["active"]  # Removed fields cannot stop or reapprove a running round.
        user.role = "viewer"
        with pytest.raises(HTTPException) as error:
            update_plan_setting(
                db, user, row.id, PlanSettingUpdate(version=row.version, field="fraction", value=20)
            )
        assert error.value.status_code == 403


def test_controls_save_schedule_stop_and_reject_start_and_foreign_owner(setup):
    with setup[1]() as db:
        user = db.scalar(select(User))
        row = TradePlan(
            owner_id=user.id,
            tenant_id=user.tenant_id,
            code="034220",
            broker="namuh",
            version=1,
            value={
                "mode": "automatic",
                "name": "Synthetic",
                "fraction": 10,
                "account_ciphertext": "retained",
                "active": False,
            },
        )
        db.add(row)
        db.commit()
        stranger = SimpleNamespace(id="foreign", tenant_id=user.tenant_id, role="trader", active=True)
        with pytest.raises(HTTPException) as error:
            control_plan(db, stranger, row.id, PlanControl(version=1, action="stop"))
        assert error.value.status_code == 404
        db.rollback()
        with pytest.raises(HTTPException) as error:
            control_plan(db, user, row.id, PlanControl(version=1, action="start"))
        assert error.value.status_code == 409
        db.rollback()
        saved = control_plan(db, user, row.id, PlanControl(version=1, action="schedule", time="10:30"))
        assert saved["trade_time"] == "10:30" and saved["active"] is False
        assert saved["auto_control"]["state"] == "scheduled_blocked"
        assert db.get(TradePlan, row.id).value["account_ciphertext"] == "retained"
        assert "account_ciphertext" not in saved
        with pytest.raises(HTTPException):
            control_plan(db, user, row.id, PlanControl(version=1, action="stop"))
        db.rollback()
        saved = control_plan(db, user, row.id, PlanControl(version=2, action="stop"))
        assert saved["auto_control"]["state"] == "stopped" and saved["trade_time"] == "10:30"
        user.role = "viewer"
        with pytest.raises(HTTPException) as error:
            control_plan(db, user, row.id, PlanControl(version=3, action="stop"))
        assert error.value.status_code == 403
        db.rollback()
        for time in ["08:59", "15:20", "24:00"]:
            with pytest.raises(ValidationError):
                PlanControl(version=3, action="schedule", time=time)
