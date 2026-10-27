import pytest
from conftest import ORIGIN
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_vectors import payload, seed_vector_data

from nastack_trading.app import create_app
from nastack_trading.db import now
from nastack_trading.models import Instrument, Tenant, User, VectorResult, WatchlistItem
from nastack_trading.security import create_session
from nastack_trading.vectors import VectorRequest, forecast_clock


def saved(factory):
    seed_vector_data(factory)
    with factory.begin() as db:
        user = db.scalar(select(User))
        row = VectorResult(
            owner_id=user.id,
            tenant_id=user.tenant_id,
            fingerprint="preview-fixture",
            value={
                "request": VectorRequest(**payload()).model_dump(mode="json"),
                "today": str(forecast_clock(now())[1]),
                "status": "completed",
                "freshness": {"needs_refresh": False},
            },
        )
        db.add(row)
        db.flush()
        return row.id, user.tenant_id


def test_preview_never_computes_or_claims_freshness(admin, setup, monkeypatch):
    import nastack_trading.vectors as module

    identity, _ = saved(setup[1])

    def forbidden(*args, **kwargs):
        raise AssertionError("Preview must not scan observations or run inference")

    monkeypatch.setattr(module, "analyze", forbidden)
    monkeypatch.setattr(module, "series_view", forbidden)
    result = admin.post("/api/vectors/preview", json=payload())
    assert result.status_code == 200, result.text
    assert result.json()["persistence"]["id"] == identity
    assert result.json()["freshness"]["verification_pending"] is True
    assert admin.post("/api/vectors/preview", json=payload(window=120)).json() == {"cache_hit": False}
    assert admin.post("/api/vectors/preview", json={**payload(), "owner_id": "foreign"}).status_code == 422


@pytest.mark.parametrize("same_tenant", [True, False])
@pytest.mark.parametrize("role", ["admin", "trader", "viewer"])
def test_saved_preview_owner_scope_even_with_same_owned_symbol(setup, same_tenant, role):
    _, tenant_id = saved(setup[1])
    with setup[1].begin() as db:
        if not same_tenant:
            tenant = Tenant(name="Preview isolation", timezone="Asia/Seoul")
            db.add(tenant)
            db.flush()
            tenant_id = tenant.id
        user = User(
            tenant_id=tenant_id, email="preview-other@example.com", role=role, active=True, code_hash="unused"
        )
        db.add(user)
        db.flush()
        inst = db.scalar(
            select(Instrument).where(Instrument.tenant_id == tenant_id, Instrument.code == "123456")
        )
        if not inst:
            inst = Instrument(
                tenant_id=tenant_id, code="123456", value={"status": "active", "name": "Public symbol"}
            )
            db.add(inst)
            db.flush()
        db.add(WatchlistItem(owner_id=user.id, tenant_id=tenant_id, instrument_id=inst.id))
        token = create_session(db, user, 24)
    with TestClient(create_app(setup[0]), headers={"origin": ORIGIN}) as client:
        client.cookies.set("trading_session", token)
        assert client.post("/api/vectors/preview", json=payload()).json() == {"cache_hit": False}
