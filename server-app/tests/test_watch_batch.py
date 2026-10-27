from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from test_vectors import seed_vector_data

from nastack_trading.models import User


def test_worker_collects_without_page_and_rechecks_owner(setup):
    from nastack_trading.db import connect
    from nastack_trading.watch_quotes import WatchQuoteWorker

    seed_vector_data(setup[1])
    engine, _ = connect(setup[0].database_url)
    worker = WatchQuoteWorker(setup[0], engine, setup[1])
    calls = []
    worker.refresh = lambda code, **kwargs: calls.append((code, kwargs["user"].id))
    worker.cycle()
    assert [c[0] for c in calls] == ["123456"]
    worker.cycle()
    assert len(calls) == 1
    with setup[1]() as db:
        user = db.scalar(select(User))
        user.active = False
        db.commit()
    worker.due.clear()
    worker.cycle()
    assert len(calls) == 1
    engine.dispose()


def test_watch_batch_owner_cache_viewer_and_foreign_codes(admin, setup, monkeypatch):
    seed_vector_data(setup[1])
    from nastack_trading.market import NamuhAuth

    monkeypatch.setattr(
        NamuhAuth, "missing", lambda *a: (_ for _ in ()).throw(AssertionError("DB-only endpoint"))
    )
    body = {"codes": ["123456"], "refresh": True}
    first = admin.post("/api/watchlist/quotes", json=body)
    assert first.status_code == 200, first.text
    assert first.json()["broker"] == "namuh"
    assert admin.post("/api/watchlist/quotes", json=body).status_code == 200
    assert admin.post("/api/watchlist/quotes", json={"codes": ["999999"], "refresh": True}).status_code == 404
    assert admin.post("/api/watchlist/quotes", json={**body, "owner_id": "other"}).status_code == 422
    with setup[1]() as db:
        user = db.scalar(select(User))
        user.role = "viewer"
        db.commit()
    from conftest import CODE, EMAIL

    assert admin.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE}).status_code == 200
    assert admin.post("/api/watchlist/quotes", json=body).status_code == 200
    cached = admin.post("/api/watchlist/quotes", json={**body, "refresh": False})
    assert cached.status_code == 200


def test_same_stock_cache_is_not_shared_between_owners(admin, setup):
    from nastack_trading.db import now
    from nastack_trading.models import BrokerSnapshot, WatchlistItem
    from nastack_trading.watch_quotes import BatchQuotes, watch_quotes_router

    seed_vector_data(setup[1])
    with setup[1]() as db:
        owner = db.scalar(select(User))
        other = User(
            email="batch-other@example.com",
            tenant_id=owner.tenant_id,
            role="trader",
            active=True,
            code_hash="synthetic-not-a-login",
        )
        db.add(other)
        db.flush()
        watched = db.scalar(select(WatchlistItem).where(WatchlistItem.owner_id == owner.id))
        db.add(
            WatchlistItem(owner_id=other.id, tenant_id=other.tenant_id, instrument_id=watched.instrument_id)
        )
        db.add(
            BrokerSnapshot(
                owner_id=owner.id,
                tenant_id=owner.tenant_id,
                kind="market",
                symbol="123456",
                environment="live",
                value={"price": 999, "collected_at": now().isoformat()},
            )
        )
        db.commit()
        endpoint = watch_quotes_router(None, None, setup[0]).routes[0].endpoint
        result = endpoint(BatchQuotes(codes=["123456"]), user=other, db=db)
        assert result["items"]["123456"]["snapshot"] is None
        with pytest.raises(HTTPException) as error:
            endpoint(
                BatchQuotes(codes=["123456"]),
                user=SimpleNamespace(id=owner.id, tenant_id="foreign-tenant", role="trader", active=True),
                db=db,
            )
        assert error.value.status_code == 404


def test_kiwoom_ip_error_is_specific_and_redacted(setup):
    import httpx

    from nastack_trading.kiwoom import KiwoomReader

    reader = KiwoomReader(
        setup[0],
        httpx.MockTransport(
            lambda r: httpx.Response(
                200,
                json={
                    "return_code": 3,
                    "return_msg": "8050:IP가 등록되지 않았습니다. synthetic-secret 192.0.2.1",
                },
            )
        ),
    )
    with pytest.raises(HTTPException) as error:
        reader.post("live", "/api/dostk/stkinfo", {"stk_cd": "123456"})
    assert error.value.status_code == 403
    assert "IP 미등록" in error.value.detail
    assert "synthetic-secret" not in error.value.detail and "192.0.2.1" not in error.value.detail
