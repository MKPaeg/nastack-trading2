import copy
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from conftest import CODE, EMAIL, ORIGIN
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from nastack_trading import dimension_twse as twse
from nastack_trading.app import create_app
from nastack_trading.db import now
from nastack_trading.dimension_catalog import BY_ID
from nastack_trading.models import DimensionCollection, Tenant, User, UserSettings
from nastack_trading.security import create_session, hash_code

PAYLOAD = {"start": "2026-01-01", "end": "2026-01-06"}
PATH = "/api/dimensions/market-15"


def body():
    return {"stat": "OK", "date": "20260101", "title": "115年01月 發行量加權股價指數歷史資料",
            "fields": twse.FIELDS, "data": [
                ["115/01/02", "29,016.68", "29,363.43", "29,007.75", "29,349.81"],
                ["115/01/06", "29,350.00", "29,500.00", "29,300.00", "29,400.00"]]}


def transport(callback=None, failing=False):
    def respond(request):
        assert request.url.host == "www.twse.com.tw"
        assert request.url.path == "/rwd/zh/TAIEX/MI_5MINS_HIST"
        assert request.url.params["date"] == "20260101"
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        if callback:
            callback()
        return httpx.Response(200, json={"stat": "bad"} if failing else body())
    return httpx.MockTransport(respond)


def test_real_definition_is_index_not_etf():
    item = BY_ID["market-15"]
    assert item["can_collect"] and item["representation"] == "original_series"
    assert item["provider"] == "twse_taiex" and item["series_code"] == "TAIEX"
    assert twse.months(date(2025, 12, 31), date(2026, 2, 1)) == [
        date(2025, 12, 1), date(2026, 1, 1), date(2026, 2, 1)]


@pytest.mark.parametrize("kind", ["title", "fields", "month", "duplicate", "nan", "bounds", "date"])
def test_parser_rejects_wrong_definition_or_values(kind):
    import json
    data = copy.deepcopy(body())
    if kind == "title":
        data["title"] = "ETF history"
    elif kind == "fields":
        data["fields"] = ["日期", "收盤指數"]
    elif kind == "month":
        data["date"] = "20260201"
    elif kind == "duplicate":
        data["data"].append(data["data"][0])
    elif kind == "nan":
        data["data"][0][4] = "NaN"
    elif kind == "bounds":
        data["data"][0][4] = "999,999.99"
    else:
        data["data"][0][0] = "115/02/02"
    with pytest.raises(ValueError):
        twse.monthly_points(json.dumps(data).encode(), date(2026, 1, 1), date(2026, 1, 1), date(2026, 1, 6))


def test_midday_bar_excluded_and_no_gap_fill(monkeypatch):
    monkeypatch.setattr(twse, "now", lambda: datetime(2026, 1, 6, 4, tzinfo=timezone.utc))
    result = twse.fetch_taiex(BY_ID["market-15"], date(2026, 1, 1), date(2026, 1, 6), transport())
    assert result["points"] == [{"date": "2026-01-02", "value": 29349.81}]
    assert result["completed_cutoff"] == "2026-01-05"
    assert result["publication_times_verified"] is False
    assert len(result["sha256"]) == 64
    with pytest.raises(HTTPException) as caught:
        twse.fetch_taiex(BY_ID["market-15"], date(2026, 1, 6), date(2026, 1, 6),
                         httpx.MockTransport(lambda r: pytest.fail("no I/O before cutoff")))
    assert caught.value.headers["X-Collection-State"] == "not_published"


@pytest.mark.parametrize("cross_tenant", [False, True])
def test_collection_roles_owner_isolation_persistence_and_failed_retry(setup, cross_tenant):
    config, factory = setup
    calls = []
    with TestClient(create_app(config, dimension_transport=transport(lambda: calls.append(1))),
                    headers={"origin": ORIGIN}) as client:
        assert client.post(PATH + "/collect", json=PAYLOAD).status_code == 401
        client.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE})
        assert client.post(PATH + "/collect", json={**PAYLOAD, "owner_id": "someone"}).status_code == 422
        result = client.post(PATH + "/collect", json=PAYLOAD)
        assert result.status_code == 200, result.text
        assert result.json()["coverage"]["source_observations"] == 2
        original = result.json()["points"]
        assert client.post(PATH + "/collect", json=PAYLOAD).json()["sync"]["mode"] == "cached"
        assert len(calls) == 1
        client.post("/api/auth/logout")
        client.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE})
        assert client.get(PATH + "/series", params=PAYLOAD).json()["points"] == original
        with factory.begin() as db:
            owner = db.scalar(select(User).where(User.email == EMAIL))
            batch = db.scalar(select(DimensionCollection).where(DimensionCollection.owner_id == owner.id))
            batch_id, owner_id = batch.id, owner.id
            tenant_id = owner.tenant_id
            if cross_tenant:
                tenant = Tenant(name="TWSE other tenant", timezone="Asia/Seoul")
                db.add(tenant)
                db.flush()
                tenant_id = tenant.id
            other = User(tenant_id=tenant_id, email="twse-other@example.com", role="viewer",
                         active=True, code_hash=hash_code(CODE))
            db.add(other)
            db.flush()
            other_id = other.id
            db.add(UserSettings(user_id=other.id, tenant_id=tenant_id, value={}))
            token = create_session(db, other, 24)
        client.cookies.clear()
        client.cookies.set("trading_session", token)
        assert client.get(PATH + "/series", params={**PAYLOAD, "owner_id": owner_id}).json()["points"] == []
        assert client.get(f"/api/dimensions/{batch_id}/series", params=PAYLOAD).status_code == 404
        assert client.post(PATH + "/collect", json=PAYLOAD).status_code == 403
        assert len(calls) == 1
        with factory.begin() as db:
            db.get(User, other_id).role = "trader"
        assert client.post(PATH + "/collect", json=PAYLOAD).status_code == 200
        assert len(calls) == 2  # No cross-owner stored-result/cache reuse.
        with factory() as db:
            rows = db.scalars(select(DimensionCollection)).all()
            assert {r.owner_id for r in rows} == {owner_id, other_id}
        client.cookies.clear()
        client.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE})
    with factory.begin() as db:
        db.execute(update(DimensionCollection).values(created_at=now() - timedelta(minutes=1)))
    with TestClient(create_app(config, dimension_transport=transport(failing=True)), headers={"origin": ORIGIN}) as c:
        c.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE})
        assert c.post(PATH + "/collect", json={**PAYLOAD, "force": True}).status_code == 502
        view = c.get(PATH + "/series", params=PAYLOAD).json()
        assert [(p["date"], p["value"]) for p in view["points"]] == [
            (p["date"], p["value"]) for p in original]
        assert view["last_attempt"]["status"] == "failed"


def test_revoked_user_does_not_store_completed_result(setup):
    config, factory = setup
    def revoke():
        with factory.begin() as db:
            user = db.scalar(select(User).where(User.email == EMAIL))
            user.active = False
    with TestClient(create_app(config, dimension_transport=transport(revoke)), headers={"origin": ORIGIN}) as c:
        c.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE})
        assert c.post(PATH + "/collect", json=PAYLOAD).status_code == 403
    with factory() as db:
        batch = db.scalar(select(DimensionCollection))
        assert batch.status == "failed"
        # Validated public reply can remain in failure audit JSON but is never a successful series.
