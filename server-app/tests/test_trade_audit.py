import json
from datetime import timedelta
from types import SimpleNamespace

from conftest import ORIGIN
from fastapi.testclient import TestClient
from sqlalchemy import select

from nastack_trading.app import create_app
from nastack_trading.db import now
from nastack_trading.models import AuditEvent, AuthSession, Tenant, TradeOrder, TradePlan, User
from nastack_trading.security import create_session, token_digest
from nastack_trading.trade_audit import clean, order_detail


def test_safe_snapshot_never_serializes_credentials():
    order = SimpleNamespace(
        id="order",
        plan_id="plan",
        status="accepted",
        value={
            "code": "034220",
            "broker": "namuh",
            "account_last4": "1234",
            "side": "buy",
            "quantity": 1,
            "limit_price": 7190,
            "account_ciphertext": "SENSITIVE-CIPHER",
            "credential_scope": "SENSITIVE-SCOPE",
            "authorization": "SENSITIVE-AUTH",
            "token": "SENSITIVE-TOKEN",
            "auto_authorization": {"token_scope": "SENSITIVE-KEY"},
        },
    )
    result = order_detail(order)
    assert result["order_id"] == "order" and result["quantity"] == 1
    assert "SENSITIVE" not in json.dumps(result)
    assert "SENSITIVE" not in json.dumps(clean({"before": order.value, "after": order.value}))


def test_trading_journal_owner_tenant_viewer_immutability_and_selection(setup):
    config, factory = setup
    with factory.begin() as db:
        owner = db.scalar(select(User))
        owner_id, tenant_id = owner.id, owner.tenant_id
        tenant = Tenant(name="journal isolation", timezone="Asia/Seoul")
        db.add(tenant)
        db.flush()
        same = User(
            tenant_id=tenant_id,
            email="journal-same@example.com",
            active=True,
            role="trader",
            code_hash="unused",
        )
        other = User(
            tenant_id=tenant.id,
            email="journal-other@example.com",
            active=True,
            role="trader",
            code_hash="unused",
        )
        db.add_all([same, other])
        db.flush()
        tokens = [create_session(db, u, 24) for u in [owner, same, other]]
        plan_ids = []
        for u in [owner, same, other]:
            p = TradePlan(
                owner_id=u.id,
                tenant_id=u.tenant_id,
                broker="kiwoom",
                code="034220",
                version=1,
                value={"mode": "manual", "name": "Synthetic"},
            )
            db.add(p)
            db.flush()
            plan_ids.append(p.id)
            db.add(
                AuditEvent(
                    actor_id=u.id,
                    tenant_id=u.tenant_id,
                    action="trading.order_accepted",
                    target_id=p.id,
                    detail={
                        "code": "034220",
                        "quantity": 1,
                        "account_ciphertext": "SENSITIVE",
                        "token": "SENSITIVE",
                    },
                )
            )
    with TestClient(create_app(config), headers={"origin": ORIGIN}) as client:
        for i, token in enumerate(tokens):
            client.cookies.set("trading_session", token)
            data = client.get("/api/trading/events").json()
            assert data["total"] == 1 and data["items"][0]["target_id"] == plan_ids[i]
            assert "SENSITIVE" not in json.dumps(data)
            assert client.delete("/api/trading/events").status_code == 405
        client.cookies.set("trading_session", tokens[0])
        response = client.post(
            f"/api/trading/plans/{plan_ids[0]}/selection", json={"field": "buy_price", "value": 7200}
        )
        assert response.status_code == 200
        for foreign in plan_ids[1:]:
            assert (
                client.post(
                    f"/api/trading/plans/{foreign}/selection", json={"field": "fraction", "value": 20}
                ).status_code
                == 404
            )
        assert (
            client.post(
                f"/api/trading/plans/{plan_ids[0]}/selection", json={"field": "token", "value": "SENSITIVE"}
            ).status_code
            == 422
        )
        data = client.get("/api/trading/events?code=034220").json()
        assert data["total"] == 2
        assert (
            next(e for e in data["items"] if e["action"] == "trading.selection_changed")["detail"]["value"]
            == 7200
        )
        # The general admin audit endpoint must not reveal another user's trading history.
        assert not any(e["target_id"] in plan_ids[1:] for e in client.get("/api/audit").json())
        with factory.begin() as db:
            assert db.get(TradePlan, plan_ids[0]).version == 1
            assert db.get(TradePlan, plan_ids[0]).value["mode"] == "manual"
            db.get(User, owner_id).role = "viewer"
            # Represent an authenticated ordinary viewer with a finite session;
            # admin-only unlimited sessions cannot authenticate after demotion.
            db.get(AuthSession, token_digest(tokens[0])).expires_at = now() + timedelta(hours=24)
        assert client.get("/api/trading/events").status_code == 200
        assert (
            client.post(
                f"/api/trading/plans/{plan_ids[0]}/selection", json={"field": "buy_price", "value": 7200}
            ).status_code
            == 403
        )


def test_preview_dismissal_is_owned_durable_and_never_cancels_order(setup):
    config, factory = setup
    with factory.begin() as db:
        user = db.scalar(select(User))
        token = create_session(db, user, 24)
        plan = TradePlan(
            owner_id=user.id,
            tenant_id=user.tenant_id,
            broker="kiwoom",
            code="034220",
            value={"mode": "manual"},
        )
        db.add(plan)
        db.flush()
        order = TradeOrder(
            owner_id=user.id,
            tenant_id=user.tenant_id,
            plan_id=plan.id,
            request_key="dismiss-preview",
            status="preview",
            value={"code": "034220", "side": "buy", "quantity": 1},
        )
        db.add(order)
        db.flush()
        oid = order.id
    with TestClient(create_app(config), headers={"origin": ORIGIN}) as client:
        client.cookies.set("trading_session", token)
        assert client.post(f"/api/trading/orders/{oid}/dismiss").json()["status"] == "dismissed"
        client.post(f"/api/trading/orders/{oid}/dismiss")
        events = client.get("/api/trading/events").json()["items"]
        assert len([e for e in events if e["action"] == "trading.order_preview_dismissed"]) == 1
    with factory() as db:
        assert db.get(TradeOrder, oid).status == "dismissed"
