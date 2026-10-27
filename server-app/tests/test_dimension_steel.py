from datetime import date

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from conftest import CODE, EMAIL, ORIGIN
from nastack_trading.app import create_app
from nastack_trading.dimension_catalog import BY_ID
from nastack_trading.dimension_steel import fetch_steel, parse_steel
from nastack_trading.models import DimensionCollection, User, Tenant, UserSettings
from nastack_trading.security import create_session, hash_code


def transport():
    def reply(request):
        assert request.url.host == "fred.stlouisfed.org"
        assert "authorization" not in request.headers and "cookie" not in request.headers
        if request.url.path.startswith("/series/"):
            return httpx.Response(200, text="WPU101 Iron and Steel Index 1982=100 Monthly U.S. Bureau of Labor Statistics Not Seasonally Adjusted")
        assert str(request.url) == "https://fred.stlouisfed.org/graph/fredgraph.csv?id=WPU101"
        return httpx.Response(200, text="observation_date,WPU101\n2026-01-01,300\n2026-02-01,.\n2026-03-01,310\n")
    return httpx.MockTransport(reply)


@pytest.mark.parametrize("body", [
    "observation_date,WRONG\n2026-01-01,1\n",
    "observation_date,WPU101\n2026-01-02,1\n",
    "observation_date,WPU101\n2026-01-01,1\n2026-01-01,2\n",
    "observation_date,WPU101\n2026-01-01,nan\n",
    "observation_date,WPU101\n2099-01-01,2\n",
    "observation_date,WPU101\n2026-01-01,1,extra\n",
])
def test_bad_steel_identity_or_values(body):
    with pytest.raises(ValueError):
        parse_steel(body.encode(), date(2026, 1, 1), date(2026, 3, 31))


def test_steel_not_spot_or_daily_filled():
    row = BY_ID["commodity-37"]
    assert row["monthly_measure"] == "producer_price_index" and not row["daily_analysis_eligible"]
    result = fetch_steel(row, date(2026, 1, 1), date(2026, 3, 31), transport())
    assert len(result["points"]) == 3 and result["points"][1]["value"] is None
    assert result["points"][0]["date"] == "2026-01-31"
    with pytest.raises(HTTPException):
        fetch_steel(row, date(2026, 1, 1), date(2026, 3, 31), httpx.MockTransport(lambda _: httpx.Response(200, text="wrong metadata")))


@pytest.mark.parametrize("cross_tenant", [False, True])
def test_steel_collection_cache_and_availability_is_owner_scoped(setup, cross_tenant):
    config, factory = setup
    with TestClient(create_app(config, dimension_transport=transport()), headers={"origin": ORIGIN}) as c:
        c.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE})
        payload = {"start": "2026-01-01", "end": "2026-03-31"}
        path = "/api/dimensions/commodity-37"
        assert c.post(path + "/collect", json=payload).status_code == 200
        saved = c.post(path + "/collect", json=payload).json()
        assert saved["sync"]["mode"] == "cached" and saved["coverage"]["source_observations"] == 2
        row = next(r for r in c.get("/api/dimensions").json()["items"] if r["id"] == "commodity-37")
        assert row["stored_observations"] == 2 and not row["vector_available"]
        assert "일별 분석 미지원" in row["vector_unavailable_reason"]
        with factory.begin() as db:
            owner = db.scalar(select(User).where(User.email == EMAIL))
            tenant_id = owner.tenant_id
            if cross_tenant:
                tenant = Tenant(name="Other steel", timezone="Asia/Seoul");db.add(tenant);db.flush();tenant_id=tenant.id
            other = User(tenant_id=tenant_id, email="steel-other@example.com", role="viewer", active=True, code_hash=hash_code(CODE))
            db.add(other);db.flush();db.add(UserSettings(user_id=other.id,tenant_id=tenant_id,value={}))
            token = create_session(db, other, 24)
        c.cookies.clear();c.cookies.set("trading_session",token)
        assert c.post(path + "/collect", json=payload).status_code == 403
        assert c.get(path + "/series",params=payload).json()["points"] == []
        other_row=next(r for r in c.get("/api/dimensions").json()["items"] if r["id"] == "commodity-37")
        assert other_row["stored_observations"] == 0 and not other_row["vector_available"]


def test_catalog_daily_availability_counts_only_own_valid_current_data(admin, setup):
    factory=setup[1]
    key="control-07";dimension=BY_ID[key]
    with factory.begin() as db:
        owner=db.scalar(select(User).where(User.email==EMAIL))
        def batch(provider, points):
            return DimensionCollection(owner_id=owner.id,tenant_id=owner.tenant_id,dimension_id=key,
                start_date=date(2026,1,1),end_date=date(2026,1,31),status="completed",
                value={"provider":provider,"series_code":dimension["series_code"],"points":points})
        db.add(batch(dimension["provider"],[{"date":"2026-01-02","value":1},{"date":"2026-01-03","value":None}]))
        db.add(batch(dimension["provider"],[{"date":"2026-01-02","value":2},{"date":"2026-01-05","value":3}]))
        db.add(batch("wrong",[{"date":"2026-01-06","value":4}]))
    catalog={r["id"]:r for r in admin.get("/api/dimensions").json()["items"]}
    assert catalog[key]["vector_available"] and catalog[key]["stored_observations"]==2
    assert not catalog["sector-42"]["vector_available"] and "미연결" in catalog["sector-42"]["vector_unavailable_reason"]
    assert not catalog["sector-01"]["vector_available"] and "저장 데이터 없음" in catalog["sector-01"]["vector_unavailable_reason"]
