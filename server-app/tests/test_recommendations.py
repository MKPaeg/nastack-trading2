import json
from datetime import timedelta

import httpx
import pytest
from conftest import CODE, EMAIL, ORIGIN
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_kiwoom import configuration, provider

from nastack_trading.app import create_app
from nastack_trading.db import now
from nastack_trading.models import BrokerSnapshot, Tenant, TradePlan, User, UserSettings, WatchlistItem
from nastack_trading.security import create_session


@pytest.fixture
def ranked(setup):
    calls, control = [], {"bad": False}
    auth = provider([])

    def respond(request):
        if request.url.path == "/oauth2/token":
            return auth.handle_request(request)
        assert request.url.path == "/api/dostk/rkinfo"
        assert request.headers["api-id"] == "ka10027"
        body = json.loads(request.content)
        calls.append(body)
        assert body["stex_tp"] == "1" and body["stk_cnd"] == "1"
        down = body["sort_tp"] == "3"
        return httpx.Response(200, headers={"cont-yn": "Y"}, json={
            "return_code": 0, "pred_pre_flu_rt_upper": [{"stk_cd": "A005930", "stk_nm": "시험종목",
            "cur_prc": "NaN" if control["bad"] else "-10000", "pred_pre": "500", "flu_rt": "5.00",
            "pred_pre_sig": "5" if down else "2", "now_trde_qty": "1,000"}]
        })

    app = create_app(configuration(setup[0]), kiwoom_transport=httpx.MockTransport(respond))
    with TestClient(app, headers={"origin": ORIGIN}) as client:
        client.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE})
        assert client.put("/api/user/brokers/kiwoom", json={"version": 0, "enabled": True, "environment": "live"}).status_code == 200
        yield client, calls, control, app


def test_closed_session_blocks_only_automatic_refresh(ranked, monkeypatch):
    from nastack_trading import market

    client, calls, _, _ = ranked
    monkeypatch.setattr(market, "market_session", lambda: {"is_open": False})
    assert client.post("/api/recommendations/refresh?auto=true").status_code == 409
    assert calls == []
    assert client.get("/api/recommendations").status_code == 200
    assert calls == []
    assert client.post("/api/recommendations/refresh").status_code == 200
    assert len(calls) == 2


def test_open_session_allows_automatic_refresh(ranked, monkeypatch):
    from nastack_trading import market

    client, calls, _, _ = ranked
    monkeypatch.setattr(market, "market_session", lambda: {"is_open": True})
    assert client.post("/api/recommendations/refresh?auto=true").status_code == 200
    assert len(calls) == 2


def test_rankings_signed_values_cache_failure_and_no_orders(ranked, setup):
    client, calls, control, _ = ranked
    assert client.get("/api/recommendations").json()["snapshot"] is None
    assert calls == []
    response = client.post("/api/recommendations/refresh")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["snapshot"]["up"][0]["rate"] == 5
    assert data["snapshot"]["down"][0]["rate"] == -5
    assert data["snapshot"]["down"][0]["price"] == 10000
    assert "scope" not in data["snapshot"]
    assert "synthetic-" not in response.text
    assert len(calls) == 2
    assert client.post("/api/recommendations/refresh").json()["reused"]
    assert len(calls) == 2
    assert client.post("/api/recommendations/refresh?market=001").status_code == 429
    with setup[1].begin() as db:
        for row in db.scalars(select(BrokerSnapshot).where(BrokerSnapshot.kind.in_(["movers", "movers_attempt"]))):
            row.created_at = now() - timedelta(seconds=40)
    control["bad"] = True
    assert client.post("/api/recommendations/refresh").status_code == 502
    assert client.get("/api/recommendations").json()["snapshot"]["down"][0]["rate"] == -5
    assert client.post("/api/recommendations/refresh").status_code == 429
    assert client.post("/api/recommendations/refresh?market=bad").status_code == 422
    status = client.get("/api/user/brokers/kiwoom").json()
    client.put("/api/user/brokers/kiwoom", json={"version": status["version"], "enabled": False, "environment": "live"})
    assert client.get("/api/recommendations").json()["available"] is False
    assert client.post("/api/recommendations/refresh").status_code == 409


def test_rankings_users_tenants_viewer_and_state_isolation(ranked, setup):
    client, calls, _, app = ranked
    client.post("/api/recommendations/refresh")
    assert client.put("/api/settings/screen-state", json={"view": "recommendations", "recommendations_market": "101"}).status_code == 200
    with setup[1].begin() as db:
        first = db.scalar(select(User).where(User.email == EMAIL))
        tenant = Tenant(name="rank-other", timezone="Asia/Seoul")
        db.add(tenant)
        db.flush()
        tokens = []
        for index, (tid, role) in enumerate([(first.tenant_id, "trader"), (tenant.id, "trader"), (first.tenant_id, "viewer")]):
            user = User(email=f"rank-{index}@example.com", tenant_id=tid, active=True, role=role, code_hash="unused")
            db.add(user)
            db.flush()
            db.add(UserSettings(user_id=user.id, tenant_id=tid, value={}))
            tokens.append(create_session(db, user, 24))
    for token in tokens:
        with TestClient(app, headers={"origin": ORIGIN}) as other:
            other.cookies.set("trading_session", token)
            assert other.get("/api/recommendations").json()["snapshot"] is None
            assert other.post("/api/recommendations/refresh").status_code == 403
            assert other.post("/api/recommendations/005930/open").status_code in (403, 404)
            assert other.get("/api/settings").json()["value"].get("screen_state", {}).get("view") != "recommendations"
    assert len(calls) == 2
    restored = client.get("/api/settings").json()["value"]["screen_state"]
    assert restored["view"] == "recommendations" and restored["recommendations_market"] == "101"


def test_open_enrolls_only_owned_ranked_stock_without_watch_or_trade(ranked, setup):
    client, calls, _, _ = ranked
    assert client.post("/api/recommendations/005930/open").status_code == 404
    client.post("/api/recommendations/refresh")
    entry = client.post("/api/recommendations/005930/open")
    assert entry.status_code == 200, entry.text
    assert entry.json()["initialize"] and not entry.json()["watchlist_added"]
    assert entry.json()["instrument"]["name"] == "시험종목"
    assert client.post("/api/recommendations/005930/open").status_code == 200
    assert client.post("/api/recommendations/000000/open").status_code == 404
    assert client.get("/api/market/instruments/005930").status_code == 200
    assert any(item["code"] == "005930" for item in client.get("/api/vectors/instruments").json()["items"])
    with setup[1]() as db:
        assert len(db.scalars(select(BrokerSnapshot).where(BrokerSnapshot.kind == "analysis_entry")).all()) == 1
        assert db.scalar(select(WatchlistItem)) is None
        assert db.scalar(select(TradePlan)) is None
    assert len(calls) == 2  # Enrollment makes no provider or LLM requests.


def test_recommended_initial_history_uses_explicit_kiwoom_only(ranked, monkeypatch):
    from nastack_trading import price_history

    client, _, _, _ = ranked
    client.post("/api/recommendations/refresh")
    client.post("/api/recommendations/005930/open")
    reads = []

    def history(db, user, config, code, start, end, transport):
        reads.append((user.id, code))
        return {"environment": "live", "provider": "kiwoom_domestic_history", "chart": [
            {"date": "2026-10-21", "close": 100, "provisional": False},
            {"date": "2026-10-22", "close": 101, "provisional": False},
            {"date": "2026-10-23", "close": 102, "provisional": False}]}

    def forbidden(*args, **kwargs):
        raise AssertionError("No silent NH fallback")

    monkeypatch.setattr(price_history, "fetch_history", history)
    monkeypatch.setattr(price_history, "fetch_namuh_history", forbidden)
    body = {"code": "005930", "start": "2026-10-21", "end": "2026-10-23", "provider": "kiwoom"}
    first = client.post("/api/vectors/stock-history", json=body)
    assert first.status_code == 200, first.text
    assert client.post("/api/vectors/stock-history", json=body).status_code == 200
    assert len(reads) == 1
