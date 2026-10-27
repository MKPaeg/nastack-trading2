import pytest
import test_live_worker
from conftest import ORIGIN
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from nastack_trading.app import create_app
from nastack_trading.models import AuditEvent, Instrument, RealizedSale, Tenant, TradeOrder, TradePlan, User
from nastack_trading.realized_sales import save_sale
from nastack_trading.security import create_session
from nastack_trading.trade_gateway import TradeGateway
from nastack_trading.trading import OrderPreview, PlanControl, change_plan_listing, control_plan


def seed(setup):
    with setup[1].begin() as db:
        user = db.scalar(select(User))
        db.add(
            Instrument(
                tenant_id=user.tenant_id, code="123456", value={"name": "보존 종목", "status": "active"}
            )
        )
        plan = TradePlan(
            owner_id=user.id,
            tenant_id=user.tenant_id,
            broker="kiwoom",
            code="123456",
            value={
                "name": "보존 종목",
                "mode": "automatic",
                "active": True,
                "fraction": 20,
                "buy_rule": "price",
                "buy_price": 100,
                "take_profit": 5,
                "account_ciphertext": "DO-NOT-EXPOSE",
                "auto_authorization": {"old": True},
                "auto_control": {"generation": "old-round"},
            },
        )
        db.add(plan)
        db.flush()
        order = TradeOrder(
            owner_id=user.id,
            tenant_id=user.tenant_id,
            plan_id=plan.id,
            request_key="historical-sale",
            status="filled",
            value={
                "side": "sell",
                "broker": "kiwoom",
                "code": plan.code,
                "name": "보존 종목",
                "environment": "live",
                "purpose": "automatic",
                "auto_exit_reason": "stop_loss",
                "filled_quantity": 2,
                "quantity": 2,
                "fill_price": 90,
                "sale_cost_basis": 100,
            },
        )
        db.add(order)
        db.flush()
        sale = save_sale(db, user, order)
        db.flush()
        pending = TradeOrder(
            owner_id=user.id,
            tenant_id=user.tenant_id,
            plan_id=plan.id,
            request_key="existing-pending",
            status="accepted",
            value={"side": "buy", "quantity": 1, "code": plan.code, "broker": "kiwoom", "name": "보존 종목"},
        )
        db.add(pending)
        return user.id, user.tenant_id, plan.id, order.id, sale.id


def client_for(setup, role="admin"):
    with setup[1].begin() as db:
        user = db.scalar(select(User))
        user.role = role
        token = create_session(db, user, 24)
    config = setup[0].model_copy(update={"live_worker_enabled": False})
    client = TestClient(create_app(config), headers={"origin": ORIGIN})
    client.cookies.set("trading_session", token)
    return client


@pytest.mark.parametrize("role", ["admin", "trader"])
def test_archive_restore_keeps_ledger_settings_identity_and_never_calls_broker(setup, monkeypatch, role):
    uid, tid, pid, oid, sid = seed(setup)

    def forbidden(*args, **kwargs):
        raise AssertionError("Listing must never call a broker")

    for name in ["request", "validate_account", "submit", "cancel"]:
        monkeypatch.setattr(TradeGateway, name, forbidden)
    with client_for(setup, role) as client:
        archive = client.post(f"/api/trading/plans/{pid}/archive", json={"version": 1})
        assert archive.status_code == 200, archive.text
        archived = archive.json()
        assert archived["archived"] and not archived["active"]
        assert "DO-NOT-EXPOSE" not in archive.text and "auto_authorization" not in archived
        listing = client.get("/api/trading/plans").json()
        assert listing["items"] == [] and listing["archived_items"][0]["id"] == pid
        assert len(listing["orders"]) == 2
        assert (
            client.patch(
                f"/api/trading/plans/{pid}/settings", json={"version": 2, "field": "fraction", "value": 10}
            ).status_code
            == 409
        )
        assert (
            client.put(
                "/api/trading/plans", json={"code": "123456", "broker": "kiwoom", "version": 2}
            ).status_code
            == 409
        )
        assert (
            client.post(
                f"/api/trading/plans/{pid}/control",
                json={"version": 2, "action": "start", "confirmation": "실전 자동매매 시작 확인"},
            ).status_code
            == 409
        )
        assert (
            client.post(
                "/api/trading/orders/preview", json={"plan_id": pid, "side": "sell", "quantity": 1}
            ).status_code
            == 409
        )
        assert client.post(f"/api/trading/plans/{pid}/restore", json={"version": 1}).status_code == 409
        report = client.get("/api/trading/realized?start=2026-01-01&end=2026-12-31").json()
        assert report["items"][0]["id"] == sid and report["items"][0]["profit"] == -20
        restored = client.post(f"/api/trading/plans/{pid}/restore", json={"version": 2})
        assert restored.status_code == 200, restored.text
        assert restored.json()["id"] == pid and restored.json()["fraction"] == 20
        assert not restored.json()["active"] and not restored.json()["archived"]
        assert restored.json()["auto_control"]["generation"] != "old-round"
        assert len(client.get("/api/trading/plans").json()["items"]) == 1
        # A later sale accumulates under the same plan; the prior sale is not overwritten.
        with setup[1].begin() as db:
            user = db.get(User, uid)
            old = db.get(TradeOrder, oid)
            later = TradeOrder(
                owner_id=uid,
                tenant_id=tid,
                plan_id=pid,
                request_key="later-sale",
                status="filled",
                value={**old.value, "filled_quantity": 1, "quantity": 1, "fill_price": 110},
            )
            db.add(later)
            db.flush()
            save_sale(db, user, later)
        report = client.get("/api/trading/realized?start=2026-01-01&end=2026-12-31").json()
        assert len(report["items"]) == 2 and report["summary"]["profit"] == -10
    with setup[1]() as db:
        assert db.scalar(select(func.count()).select_from(TradePlan)) == 1
        assert db.get(RealizedSale, sid).profit == -20
        assert db.get(TradeOrder, oid).status == "filled"
        assert (
            db.scalar(select(TradeOrder).where(TradeOrder.request_key == "existing-pending")).status
            == "accepted"
        )
        events = list(
            db.scalars(
                select(AuditEvent).where(
                    AuditEvent.action.in_(["trading.plan_archived", "trading.plan_restored"])
                )
            )
        )
        assert len(events) == 2 and all("DO-NOT-EXPOSE" not in str(r.detail) for r in events)


@pytest.mark.parametrize("same_tenant", [True, False])
@pytest.mark.parametrize("role", ["admin", "trader", "viewer"])
def test_archive_and_restore_cannot_cross_owner_or_tenant(setup, same_tenant, role):
    _, tid, pid, _, _ = seed(setup)
    with setup[1].begin() as db:
        if not same_tenant:
            tenant = Tenant(name="other", timezone="Asia/Seoul")
            db.add(tenant)
            db.flush()
            tid = tenant.id
        other = User(tenant_id=tid, email="foreign@example.com", role=role, active=True, code_hash="unused")
        db.add(other)
        db.flush()
        token = create_session(db, other, 24)
    with TestClient(
        create_app(setup[0].model_copy(update={"live_worker_enabled": False})), headers={"origin": ORIGIN}
    ) as client:
        client.cookies.set("trading_session", token)
        listing = client.get("/api/trading/plans").json()
        assert listing["items"] == listing["archived_items"] == []
        for action in ["archive", "restore"]:
            response = client.post(f"/api/trading/plans/{pid}/{action}", json={"version": 1})
            assert response.status_code == (403 if role == "viewer" else 404)


def test_viewer_cannot_modify_own_list_and_owner_injection_is_rejected(setup):
    _, _, pid, _, _ = seed(setup)
    with client_for(setup, "viewer") as client:
        assert len(client.get("/api/trading/plans").json()["items"]) == 1
        assert client.post(f"/api/trading/plans/{pid}/archive", json={"version": 1}).status_code == 403
    with client_for(setup) as client:
        assert (
            client.post(
                f"/api/trading/plans/{pid}/archive", json={"version": 1, "owner_id": "forged"}
            ).status_code
            == 422
        )


@pytest.fixture
def runner(setup, monkeypatch):
    return test_live_worker.runner.__wrapped__(setup, monkeypatch)


def test_removal_at_final_transmission_boundary_blocks_wire(runner):
    worker, gateway, factory, uid, tid, pid = runner
    gateway.change = lambda db, user, plan: change_plan_listing(
        db, user, plan.id, plan.version, archived=True
    )
    with factory() as db:
        worker.evaluate(db, pid, uid, tid)
    assert gateway.wire == []
    with factory() as db:
        plan = db.get(TradePlan, pid)
        assert plan.value["archived"] and not plan.value["active"]
        orders = list(db.scalars(select(TradeOrder)))
        assert len(orders) == 1 and orders[0].status == "blocked"


def test_existing_order_reconciliation_continues_after_removal(runner):
    worker, gateway, factory, uid, tid, pid = runner
    with factory() as db:
        user = db.get(User, uid)
        plan = db.get(TradePlan, pid)
        order = TradeOrder(
            owner_id=uid,
            tenant_id=tid,
            plan_id=pid,
            request_key="already-submitted",
            status="accepted",
            value={
                "broker": "kiwoom",
                "code": plan.code,
                "side": "buy",
                "quantity": 1,
                "reference_price": 100,
                "filled_quantity": 0,
                "remaining_quantity": 1,
                "name": "Synthetic",
                "environment": "live",
            },
        )
        db.add(order)
        db.commit()
        oid = order.id
        change_plan_listing(db, user, pid, plan.version, archived=True)
    worker.tick()
    with factory() as db:
        assert db.get(TradeOrder, oid).status == "filled"
    assert gateway.wire == []


def test_restore_does_not_revive_old_intents_or_approval(runner):
    worker, gateway, factory, uid, tid, pid = runner
    with factory() as db:
        user = db.get(User, uid)
        intent = worker.service.preview(
            db,
            user,
            OrderPreview(plan_id=pid, side="buy", quantity=1, limit_price=100, request_key="before-removal"),
            automatic=True,
        )
        plan = db.get(TradePlan, pid)
        change_plan_listing(db, user, pid, plan.version, archived=True)
        restored = change_plan_listing(db, user, pid, plan.version, archived=False)
        assert not restored["active"] and "auto_authorization" not in restored
        with pytest.raises(HTTPException) as blocked:
            worker.service.execute(db, user, intent.id)
        assert blocked.value.status_code == 409 and gateway.wire == []
        # Only a fresh, explicit approval starts a new generation.
        started = control_plan(
            db,
            user,
            pid,
            PlanControl(version=plan.version, action="start", confirmation="실전 자동매매 시작 확인"),
            worker.config,
        )
        assert started["active"]
        assert started["auto_control"]["generation"] != intent.value["auto_generation"]
