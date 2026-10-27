"""Synthetic KRX transport only; never use registered keys or the production DB."""

import json
from datetime import date

import httpx
import pytest
from conftest import CODE, EMAIL, ORIGIN
from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from nastack_trading.app import create_app
from nastack_trading.dimension_catalog import BY_ID
from nastack_trading.dimension_krx import MAX_DAYS, read_day, fetch_vkospi
from nastack_trading.models import DimensionCollection, DimensionExclusion, User, UserSettings, Tenant
from nastack_trading.security import create_session, hash_code

KEY = "synthetic-krx-test-only"
PATH = "/api/user/connections/krx"
DIM = "/api/dimensions/control-11"


def transport(calls, callback=None, key=KEY):
    def reply(request):
        calls.append(request.url.params["basDd"])
        assert request.url.host == "data-dbg.krx.co.kr"
        assert request.url.path == "/svc/apis/idx/drvprod_dd_trd"
        assert request.headers["AUTH_KEY"] == key
        assert key not in str(request.url) and "cookie" not in request.headers
        if callback:
            callback()
        return httpx.Response(200, json={"OutBlock_1": [{"BAS_DD": calls[-1], "IDX_NM": "코스피 200 변동성지수", "CLSPRC_IDX": "23.45"}]})
    return httpx.MockTransport(reply)


def config_for(setup):
    return setup[0].model_copy(update={"broker_encryption_key": SecretStr(Fernet.generate_key().decode())})


def login(client):
    assert client.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE}).status_code == 200


def register(client):
    assert client.put(PATH, json={"version": 0, "enabled": True, "fields": {"api_key": KEY}}).status_code == 200


@pytest.mark.parametrize("row", [
    {"IDX_NM": "코스피 200", "CLSPRC_IDX": "20"},
    {"IDX_NM": "코스피 200 변동성 선물", "CLSPRC_IDX": "20"},
    {"IDX_NM": "VKOSPI", "CLSPRC_IDX": "nan"},
    {"IDX_NM": "VKOSPI", "CLSPRC_IDX": "0"},
    {"IDX_NM": "VKOSPI", "CLSPRC_IDX": "1,23.45"},
    {"IDX_NM": "VKOSPI", "CLSPRC_IDX": "-"},
    {"IDX_NM": "VKOSPI", "CLSPRC_IDX": "20", "BAS_DD": "20260105"},
])
def test_invalid_index_date_or_price_never_success(row):
    body = {"OutBlock_1": [{"BAS_DD": "20260102", **row}]}
    with pytest.raises(HTTPException) as err:
        read_day("2026-01-02", KEY, httpx.MockTransport(lambda r: httpx.Response(200, json=body)))
    assert err.value.status_code == 502


@pytest.mark.parametrize("status", [200, 401, 403, 429, 500])
def test_provider_errors_do_not_echo_keys(status, caplog):
    caplog.set_level("DEBUG", logger="httpcore")
    with pytest.raises(HTTPException) as err:
        read_day("2026-01-02", KEY, httpx.MockTransport(lambda r: httpx.Response(status, text=KEY)))
    assert KEY not in str(err.value.detail) and KEY not in caplog.text


@pytest.mark.parametrize("body", [[], {"OutBlock_1": "wrong"}, {"OutBlock_1": [{"BAS_DD":"20260102","IDX_NM":"VKOSPI","CLSPRC_IDX":"20"}] * 2}])
def test_malformed_or_ambiguous_json_fails_closed(body):
    with pytest.raises(HTTPException) as err:
        read_day("2026-01-02", KEY, httpx.MockTransport(lambda r: httpx.Response(200, json=body)))
    assert err.value.status_code == 502


def test_scan_bounded_and_not_filled_or_spliced():
    calls = []
    data = fetch_vkospi(BY_ID["control-11"], date(2026, 1, 1), date(2026, 1, 31), KEY, 3, transport(calls))
    assert len(calls) == len(data["points"]) == MAX_DAYS
    assert data["connection_version"] == 3 and data["scanned_through"] < "2026-01-31"
    assert KEY not in json.dumps(data) and data["daily_analysis_eligible"] is False


def test_connect_then_market_priority_collect_partial_cache_and_key_rotation(setup):
    config = config_for(setup)
    calls = []
    with TestClient(create_app(config, dimension_transport=transport(calls)), headers={"origin": ORIGIN}) as c:
        login(c)
        register(c)
        with setup[1].begin() as db:
            user = db.scalar(select(User).where(User.email == EMAIL))
            db.add(DimensionExclusion(owner_id=user.id, tenant_id=user.tenant_id, dimension_id="control-11", reason="previous_unconnected", definition={}))
        def catalog():
            return c.get("/api/dimensions").json()["items"]
        assert not any(r["id"] == "control-11" for r in catalog())
        assert c.post(DIM + "/collect", json={"start": "2026-01-01", "end": "2026-01-31"}).status_code == 410
        assert calls == []
        result = c.post(PATH + "/test")
        assert result.status_code == 200, result.text
        assert result.json()["latest_value"] == 23.45 and result.json()["observations_saved"] is False
        assert KEY not in result.text
        with setup[1]() as db:
            assert db.scalar(select(DimensionCollection)) is None
        items = catalog(); ids = [r["id"] for r in items]
        row = items[ids.index("control-11")]
        assert ids.index("control-11") + 1 == ids.index("market-01")
        assert row["family"] == "market" and row["connection_verified"]
        assert c.post(PATH + "/test").status_code == 429
        result = c.post(DIM + "/collect", json={"start": "2026-01-01", "end": "2026-01-31"})
        assert result.status_code == 200, result.text
        data = result.json()
        assert len(data["points"]) == MAX_DAYS and data["sync"]["remaining"]
        assert data["sync"]["fetched"][1] < "2026-01-31"
        cached = c.post(DIM + "/collect", json={"start": "2026-01-01", "end": data["sync"]["fetched"][1]})
        assert cached.status_code == 200 and cached.json()["sync"]["mode"] == "cached"
        c.post("/api/auth/logout"); login(c)
        assert next(r for r in catalog() if r["id"] == "control-11")["connection_verified"]
        assert c.get(PATH).json()["checks"]["krx"]["status"] == "connected"
        c.put(PATH, json={"version": 1, "enabled": False, "fields": {}})
        assert not next(r for r in catalog() if r["id"] == "control-11")["connection_verified"]
        assert c.get(DIM + "/series", params={"start": "2026-01-01", "end": "2026-01-31"}).json()["points"] == []


@pytest.mark.parametrize("cross_tenant", [False, True])
def test_checks_keys_and_collected_data_are_owner_scoped(setup, cross_tenant):
    config = config_for(setup); calls = []
    with TestClient(create_app(config, dimension_transport=transport(calls)), headers={"origin": ORIGIN}) as c:
        login(c); register(c)
        assert c.post(PATH + "/test").status_code == 200
        with setup[1].begin() as db:
            owner = db.scalar(select(User).where(User.email == EMAIL))
            tenant_id = owner.tenant_id
            if cross_tenant:
                tenant = Tenant(name="KRX check isolation", timezone="Asia/Seoul"); db.add(tenant); db.flush(); tenant_id = tenant.id
            other = User(tenant_id=tenant_id, email="krx-check-other@example.com", active=True, role="viewer", code_hash=hash_code(CODE))
            db.add(other); db.flush(); db.add(UserSettings(user_id=other.id, tenant_id=tenant_id, value={}))
            token = create_session(db, other, 24)
        c.post("/api/auth/logout"); c.cookies.clear(); c.cookies.set("trading_session", token)
        assert c.post(PATH + "/test").status_code == 403
        assert c.post(DIM + "/collect", json={"start": "2026-01-01", "end": "2026-01-31"}).status_code == 403
        row = next(r for r in c.get("/api/dimensions").json()["items"] if r["id"] == "control-11")
        assert not row["connection_verified"]
        assert c.get(PATH).json()["checks"] == {}


def test_role_revoked_inflight_discards_check(setup):
    config = config_for(setup)
    def revoke():
        with setup[1].begin() as db:
            db.scalar(select(User).where(User.email == EMAIL)).role = "viewer"
    with TestClient(create_app(config, dimension_transport=transport([], callback=revoke)), headers={"origin": ORIGIN}) as c:
        login(c); register(c)
        assert c.post(PATH + "/test").status_code == 403
        with setup[1]() as db:
            user = db.scalar(select(User).where(User.email == EMAIL))
            assert db.get(UserSettings, user.id).value["connection_checks"]["krx"]["status"] != "connected"


def test_key_changes_inflight_stop_further_calls_and_discard_collection(setup):
    config = config_for(setup); calls = []; mutate = {"enabled": False}
    def change_key():
        if mutate["enabled"]:
            with setup[1].begin() as db:
                user = db.scalar(select(User).where(User.email == EMAIL))
                settings = db.get(UserSettings, user.id)
                value = json.loads(json.dumps(settings.value))
                value["personal_connections"]["krx"]["version"] += 1
                settings.value = value
    with TestClient(create_app(config, dimension_transport=transport(calls, callback=change_key)), headers={"origin": ORIGIN}) as c:
        login(c); register(c); assert c.post(PATH + "/test").status_code == 200
        mutate["enabled"] = True
        result = c.post(DIM + "/collect", json={"start": "2026-01-01", "end": "2026-01-31"})
        assert result.status_code == 409 and len(calls) == 2
        with setup[1]() as db:
            batch = db.scalar(select(DimensionCollection))
            assert batch.status == "failed" and not batch.value.get("points")


def test_each_trader_tests_with_own_key_and_separate_check(setup):
    config = config_for(setup); keys = []
    def reply(request):
        key = request.headers["AUTH_KEY"]; keys.append(key)
        return httpx.Response(200, json={"OutBlock_1": [{"BAS_DD": request.url.params["basDd"], "IDX_NM": "VKOSPI", "CLSPRC_IDX": "20" if key == KEY else "30"}]})
    with setup[1].begin() as db:
        owner = db.scalar(select(User).where(User.email == EMAIL))
        other = User(tenant_id=owner.tenant_id, email="krx-trader@example.com", role="trader", active=True, code_hash=hash_code(CODE))
        db.add(other); db.flush(); db.add(UserSettings(user_id=other.id, tenant_id=other.tenant_id, value={}))
        token = create_session(db, other, 24)
    with TestClient(create_app(config, dimension_transport=httpx.MockTransport(reply)), headers={"origin": ORIGIN}) as c:
        login(c); register(c); assert c.post(PATH + "/test").json()["latest_value"] == 20
        c.post("/api/auth/logout"); c.cookies.clear(); c.cookies.set("trading_session", token)
        assert not next(r for r in c.get("/api/dimensions").json()["items"] if r["id"] == "control-11")["connection_verified"]
        c.put(PATH, json={"version": 0, "enabled": True, "fields": {"api_key": "synthetic-second-owner-key"}})
        assert c.post(PATH + "/test").json()["latest_value"] == 30
        assert keys == [KEY, "synthetic-second-owner-key"]
