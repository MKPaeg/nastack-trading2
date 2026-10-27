from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from nastack_trading.models import BrokerSnapshot, TradePlan, User
from nastack_trading.trade_gateway import TradeGateway
from nastack_trading.trade_performance import performance, realized


def order(side, quantity, price, day="2026-10-22", **extra):
    return SimpleNamespace(
        status="filled",
        created_at=datetime.fromisoformat(day + "T10:00:00+09:00"),
        value={
            "side": side,
            "filled_quantity": quantity,
            "fill_price": price,
            "fill_checked_at": day + "T10:01:00+09:00",
            **extra,
        },
    )


def test_realized_uses_actual_fills_not_limit_prices_and_preserves_missing():
    result = realized(
        [order("buy", 1, 7190, limit_price=7200), order("sell", 1, 7180, limit_price=7170)], "2026-10-22"
    )
    assert result["profit"] == -10
    assert result["return_rate"] == pytest.approx(-10 / 7190 * 100)
    assert not result["incomplete"]
    assert realized([order("sell", 1, 7180)], "2026-10-22")["profit"] is None
    assert realized([order("buy", 1, 100), order("sell", 2, 110)], "2026-10-22")["profit"] is None
    assert realized([order("buy", 1, 100, fill_checked_at="2026-10-23T10:00:00+09:00")], "2026-10-23")[
        "incomplete"
    ]
    partial = order("buy", 2, 100)
    partial.status = "canceled"
    assert realized([partial, order("sell", 1, 105)], "2026-10-22")["profit"] == 5
    preview = order("sell", 1, 999)
    preview.status = "preview"
    assert realized([preview], "2026-10-22")["profit"] == 0


def test_realized_uses_saved_broker_average_for_old_and_new_holdings():
    result = realized([order("sell", 7, 105, sale_cost_basis=100)], "2026-10-22")
    assert result["profit"] == 35
    assert result["return_rate"] == pytest.approx(5)
    assert not result["incomplete"]


@pytest.mark.parametrize("broker", ["namuh", "kiwoom"])
def test_position_is_broker_read_only_and_rejects_partial(monkeypatch, broker):
    gateway = TradeGateway(SimpleNamespace())
    plan = SimpleNamespace(broker=broker, code="034220")
    monkeypatch.setattr(gateway, "validate_account", lambda *args: None)
    if broker == "namuh":
        data = {"Output_1": [{"iem_cd": "034220", "itg_bnc_qty": "0", "ny_stl_qty": "2", "rsdl_qty": "1"}]}
    else:
        data = {"acnt_evlt_remn_indv_tot": [{"stk_cd": "A034220", "rmnd_qty": "1"}]}
    calls = []
    monkeypatch.setattr(gateway, "request", lambda *args: (calls.append(args) or data, "live"))
    assert gateway.position(None, None, plan, "private-account")["quantity"] == 1
    if broker == "namuh":
        assert calls[-1][-1]["bnc_bse_cd"] == "1"
        data["Output_1"][0].update(itg_bnc_qty=0, ny_stl_qty=1, rsdl_qty=0)
        assert gateway.position(None, None, plan, "private-account")["quantity"] == 0
    assert not any("/order/" in str(a) or "/ordr" in str(a) for a in calls)
    data["_has_more"] = True
    with pytest.raises(HTTPException):
        gateway.position(None, None, plan, "private-account")


def test_performance_owner_tenant_viewer_and_cache(setup, monkeypatch):
    with setup[1]() as db:
        user = db.scalar(select(User))
        plan = TradePlan(
            owner_id=user.id,
            tenant_id=user.tenant_id,
            broker="kiwoom",
            code="034220",
            value={"name": "Synthetic"},
        )
        db.add(plan)
        db.commit()
        calls = []

        def position(*args):
            calls.append(args[1].id)
            return {"quantity": 1, "environment": "live", "collected_at": "2026-10-22T10:00:00+09:00"}

        service = SimpleNamespace(gateway=SimpleNamespace(position=position), account=lambda p: "")
        for stranger in [
            SimpleNamespace(id="other", tenant_id=user.tenant_id),
            SimpleNamespace(id=user.id, tenant_id="other"),
        ]:
            with pytest.raises(HTTPException) as error:
                performance(db, stranger, plan.id, service, True)
            assert error.value.status_code == 404
        assert not calls
        assert performance(db, user, plan.id, service, True)["position"]["quantity"] == 1
        assert performance(db, user, plan.id, service, True)["position"]["quantity"] == 1
        assert calls == [user.id]
        user.role = "viewer"
        assert performance(db, user, plan.id, service)["position"]["quantity"] == 1
        with pytest.raises(HTTPException) as error:
            performance(db, user, plan.id, service, True)
        assert error.value.status_code == 403


def test_namuh_old_inferred_position_is_not_reused(setup):
    with setup[1]() as db:
        user = db.scalar(select(User))
        plan = TradePlan(owner_id=user.id, tenant_id=user.tenant_id, broker="namuh", code="034220", value={})
        db.add(plan)
        db.flush()
        db.add(
            BrokerSnapshot(
                owner_id=user.id,
                tenant_id=user.tenant_id,
                kind="trade_position",
                symbol=plan.code,
                environment="live",
                value={"plan_id": plan.id, "account_identity": None, "quantity": 1},
            )
        )
        db.commit()
        calls = []

        def position(*args):
            calls.append(True)
            return {"quantity": 0, "environment": "live", "quantity_basis": "broker_residual_v3"}

        service = SimpleNamespace(gateway=SimpleNamespace(position=position), account=lambda p: "")
        assert performance(db, user, plan.id, service)["position"] is None
        assert performance(db, user, plan.id, service, True)["position"]["quantity"] == 0
        assert performance(db, user, plan.id, service, True)["position"]["quantity"] == 0
        assert calls == [True]
