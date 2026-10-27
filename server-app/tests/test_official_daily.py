import json
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from conftest import CODE, EMAIL, ORIGIN
from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from nastack_trading.app import create_app
from nastack_trading.db import now
from nastack_trading.dimension_catalog import BY_ID
from nastack_trading.models import DimensionCollection, User, UserSettings
from nastack_trading.official_daily import EIA, eia_daily_points, fetch_ecos
from nastack_trading.security import create_session, hash_code


def ecos_transport(key="owner-key-123", callback=None):
    def respond(request):
        assert request.url.host == "ecos.bok.or.kr"
        assert request.url.path.startswith(f"/api/StatisticSearch/{key}/")
        if callback:
            callback()
        return httpx.Response(
            200,
            json={
                "StatisticSearch": {
                    "list_total_count": 1,
                    "row": [
                        {
                            "STAT_CODE": "731Y001",
                            "ITEM_CODE1": "0000001",
                            "ITEM_NAME1": "원/미국달러(매매기준율)",
                            "UNIT_NAME": "원",
                            "TIME": "20260102",
                            "DATA_VALUE": "1400.2",
                        }
                    ],
                }
            },
        )

    return httpx.MockTransport(respond)


def test_ecos_validates_series_and_does_not_leak_key(caplog):
    caplog.set_level("DEBUG", logger="httpx")
    result = fetch_ecos(
        BY_ID["control-01"], date(2026, 1, 1), date(2026, 1, 6), "owner-key-123", ecos_transport()
    )
    assert result["points"] == [{"date": "2026-01-02", "value": 1400.2}]
    assert "owner-key-123" not in json.dumps(result)
    assert "owner-key-123" not in caplog.text
    assert "[REDACTED]" in caplog.text
    for bad in [
        "<html>error owner-key-123</html>",
        '{"RESULT":{"CODE":"ERROR-100","MESSAGE":"owner-key-123"}}',
    ]:
        with pytest.raises(HTTPException) as caught:
            fetch_ecos(
                BY_ID["control-01"],
                date(2026, 1, 1),
                date(2026, 1, 6),
                "owner-key-123",
                httpx.MockTransport(lambda request: httpx.Response(200, text=bad)),
            )
        assert "owner-key-123" not in caught.value.detail


@pytest.mark.parametrize("code", list(EIA))
def test_eia_daily_keeps_nulls_and_checks_product_week_boundary(code):
    def body(label):
        return (
            f'<h1>{label}</h1>Week Of<table><tr><td class="B6">2025 Dec-29 to Jan- 2</td>'
            '<td class="B3">1</td><td class="B3">2</td><td class="B3">3</td>'
            '<td class="B3"></td><td class="B3">4</td></tr></table>'
        ).encode()

    points = eia_daily_points(body(EIA[code][1]), code, date(2026, 1, 1), date(2026, 1, 2))
    assert points == [{"date": "2026-01-01", "value": None}, {"date": "2026-01-02", "value": 4}]
    with pytest.raises(ValueError):
        eia_daily_points(body("Wrong product"), code, date(2026, 1, 1), date(2026, 1, 2))
    with pytest.raises(HTTPException) as caught:
        eia_daily_points(body(EIA[code][1]), code, date(2026, 1, 5), date(2026, 1, 6))
    assert caught.value.headers["X-Collection-State"] == "not_published"


@pytest.mark.parametrize("cross_tenant", [False, True])
def test_ecos_connection_encrypted_isolation_relogin_and_old_series_exclusion(setup, cross_tenant):
    from nastack_trading.models import Tenant

    config, factory = setup
    config = config.model_copy(update={"broker_encryption_key": SecretStr(Fernet.generate_key().decode())})
    with TestClient(
        create_app(config, dimension_transport=ecos_transport()), headers={"origin": ORIGIN}
    ) as c:
        c.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE})
        path = "/api/user/connections/ecos"
        assert c.get(path).json()["configured"] is False
        assert (
            c.put(path, json={"version": 0, "enabled": True, "fields": {"api_key": "sample"}}).status_code
            == 422
        )
        result = c.put(path, json={"version": 0, "enabled": True, "fields": {"api_key": "owner-key-123"}})
        assert result.status_code == 200 and result.json()["fields"] == {}
        assert "owner-key-123" not in result.text
        assert c.put(path, json={"version": 0, "enabled": False, "fields": {}}).status_code == 409
        params = {"start": "2026-01-01", "end": "2026-01-06"}
        with factory.begin() as db:
            owner = db.scalar(select(User).where(User.email == EMAIL))
            old = DimensionCollection(
                owner_id=owner.id,
                tenant_id=owner.tenant_id,
                dimension_id="control-01",
                start_date=date(2026, 1, 1),
                end_date=date(2026, 1, 6),
                status="completed",
                value={
                    "provider": "fred",
                    "series_code": "DEXKOUS",
                    "points": [{"date": "2026-01-02", "value": 999}],
                },
                created_at=now() - timedelta(minutes=1),
            )
            db.add(old)
        assert c.get("/api/dimensions/control-01/series", params=params).json()["points"] == []
        assert c.post("/api/dimensions/control-01/collect", json=params).status_code == 200
        assert (
            c.get("/api/dimensions/control-01/series", params=params).json()["points"][0]["value"] == 1400.2
        )
        c.post("/api/auth/logout")
        c.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE})
        assert c.get(path).json()["secret_configured"] is True
        with factory.begin() as db:
            owner = db.scalar(select(User).where(User.email == EMAIL))
            settings = db.get(UserSettings, owner.id)
            assert "owner-key-123" not in json.dumps(settings.value)
            assert (
                db.scalar(select(DimensionCollection).where(DimensionCollection.id == old.id)).value[
                    "series_code"
                ]
                == "DEXKOUS"
            )
            tenant_id = owner.tenant_id
            if cross_tenant:
                t = Tenant(name="Separate", timezone="Asia/Seoul")
                db.add(t)
                db.flush()
                tenant_id = t.id
            other = User(
                tenant_id=tenant_id,
                email="ecos-other@example.com",
                role="viewer",
                active=True,
                code_hash=hash_code(CODE),
            )
            db.add(other)
            db.flush()
            db.add(UserSettings(user_id=other.id, tenant_id=tenant_id, value={}))
            token = create_session(db, other, 24)
        c.cookies.clear()
        c.cookies.set("trading_session", token)
        assert c.get(path).json()["configured"] is False
        assert c.get("/api/dimensions/control-01/series", params=params).json()["points"] == []
        assert c.post("/api/dimensions/control-01/collect", json=params).status_code == 403
        assert (
            c.put(
                path, json={"version": 0, "enabled": True, "fields": {"api_key": "other-key-123"}}
            ).status_code
            == 403
        )
        with factory.begin() as db:
            other.role = "trader"
            db.merge(other)
        assert c.post("/api/dimensions/control-01/collect", json=params).status_code == 409
        assert c.put(path, json={"version": 1, "enabled": False, "fields": {}}).status_code == 409
        assert (
            c.put(
                path,
                json={
                    "version": 0,
                    "enabled": True,
                    "fields": {"api_key": "other-key-123", "owner_id": owner.id},
                },
            ).status_code
            == 422
        )


def test_ecos_disabled_does_not_inherit_or_charge(setup):
    config, factory = setup
    config = config.model_copy(update={"broker_encryption_key": SecretStr(Fernet.generate_key().decode())})
    with TestClient(
        create_app(config, dimension_transport=httpx.MockTransport(lambda request: pytest.fail("No I/O"))),
        headers={"origin": ORIGIN},
    ) as c:
        c.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE})
        assert (
            c.put(
                "/api/user/connections/ecos", json={"version": 0, "enabled": False, "fields": {}}
            ).status_code
            == 200
        )
        response = c.post(
            "/api/dimensions/control-01/collect", json={"start": "2026-01-01", "end": "2026-01-06"}
        )
        assert response.status_code == 409
        assert c.get("/api/data-collection").json()["usage"].get("dimension", 0) == 0


def test_ecos_connection_revoked_during_collection_is_not_applied(setup):
    from nastack_trading.personal_connections import values
    from nastack_trading.user_brokers import cipher

    config, factory = setup
    config = config.model_copy(update={"broker_encryption_key": SecretStr(Fernet.generate_key().decode())})

    def revoke():
        with factory.begin() as db:
            user = db.scalar(select(User).where(User.email == EMAIL))
            row = db.get(UserSettings, user.id)
            record = row.value["personal_connections"]["ecos"]
            value = {**values(db, user, config, "ecos"), "enabled": False}
            row.value = {
                **row.value,
                "personal_connections": {
                    "ecos": {
                        "version": record["version"] + 1,
                        "ciphertext": cipher(config).encrypt(json.dumps(value).encode()).decode(),
                    }
                },
            }

    with TestClient(
        create_app(config, dimension_transport=ecos_transport(callback=revoke)), headers={"origin": ORIGIN}
    ) as c:
        c.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE})
        c.put(
            "/api/user/connections/ecos",
            json={"version": 0, "enabled": True, "fields": {"api_key": "owner-key-123"}},
        )
        params = {"start": "2026-01-01", "end": "2026-01-06"}
        assert c.post("/api/dimensions/control-01/collect", json=params).status_code == 409
        assert c.get("/api/dimensions/control-01/series", params=params).json()["points"] == []


def test_ecos_connection_test_is_explicit_bounded_and_does_not_collect(setup, monkeypatch):
    from nastack_trading import personal_connections
    from nastack_trading.models import DataCollectionPolicy

    monkeypatch.setattr(personal_connections, "now", lambda: datetime(2026, 1, 15, tzinfo=timezone.utc))
    config, factory = setup
    config = config.model_copy(update={"broker_encryption_key": SecretStr(Fernet.generate_key().decode())})
    calls = []
    with TestClient(
        create_app(config, dimension_transport=ecos_transport(callback=lambda: calls.append(True))),
        headers={"origin": ORIGIN},
    ) as c:
        path = "/api/user/connections/ecos/test"
        assert c.post(path).status_code == 401
        c.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE})
        assert c.post(path).status_code == 409 and calls == []
        c.put(
            "/api/user/connections/ecos",
            json={"version": 0, "enabled": True, "fields": {"api_key": "owner-key-123"}},
        )
        assert calls == []
        result = c.post(path)
        assert result.status_code == 200, result.text
        assert result.json()["latest_date"] == "2026-01-02"
        assert result.json()["observations_saved"] is False
        assert "owner-key-123" not in result.text
        assert calls == [True]
        assert c.post(path).status_code == 429 and calls == [True]
        with factory() as db:
            user = db.scalar(select(User).where(User.email == EMAIL))
            assert db.scalar(select(DimensionCollection)) is None
            assert db.get(DataCollectionPolicy, user.id) is None
            settings = db.get(UserSettings, user.id)
            assert settings.value["connection_checks"]["ecos"]["status"] == "connected"
        c.post("/api/auth/logout")
        c.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE})
        assert c.get("/api/user/connections/ecos").json()["secret_configured"] is True


@pytest.mark.parametrize(
    "cross_tenant,role", [(False, "trader"), (True, "trader"), (False, "viewer"), (True, "viewer")]
)
def test_ecos_test_endpoint_never_uses_other_owner_key(setup, cross_tenant, role):
    from nastack_trading.models import Tenant

    config, factory = setup
    config = config.model_copy(update={"broker_encryption_key": SecretStr(Fernet.generate_key().decode())})
    with TestClient(
        create_app(
            config, dimension_transport=httpx.MockTransport(lambda request: pytest.fail("No key inheritance"))
        ),
        headers={"origin": ORIGIN},
    ) as c:
        c.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE})
        c.put(
            "/api/user/connections/ecos",
            json={"version": 0, "enabled": True, "fields": {"api_key": "owner-key-123"}},
        )
        with factory.begin() as db:
            user = db.scalar(select(User).where(User.email == EMAIL))
            tenant_id = user.tenant_id
            if cross_tenant:
                tenant = Tenant(name="ECOS test isolation", timezone="Asia/Seoul")
                db.add(tenant)
                db.flush()
                tenant_id = tenant.id
            other = User(
                tenant_id=tenant_id,
                email="ecos-test-other@example.com",
                role=role,
                active=True,
                code_hash=hash_code(CODE),
            )
            db.add(other)
            db.flush()
            db.add(UserSettings(user_id=other.id, tenant_id=tenant_id, value={}))
            token = create_session(db, other, 24)
        c.cookies.clear()
        c.cookies.set("trading_session", token)
        assert c.post("/api/user/connections/ecos/test", json={"owner_id": user.id}).status_code == (
            403 if role == "viewer" else 409
        )
