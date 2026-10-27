"""Credential registration only. No real keys, provider calls, orders or messages."""

import json

import httpx
import pytest
from conftest import CODE, EMAIL, ORIGIN
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from nastack_trading.app import create_app
from nastack_trading.models import Tenant, User, UserSettings
from nastack_trading.personal_connections import values
from nastack_trading.security import hash_code, create_session


@pytest.mark.parametrize("cross_tenant", [False, True])
@pytest.mark.parametrize("role", ["admin", "trader", "viewer"])
def test_krx_registration_isolation_permissions_encryption_and_relogin(setup, cross_tenant, role):
    config, factory = setup
    config = config.model_copy(update={"broker_encryption_key": SecretStr(Fernet.generate_key().decode())})
    def forbidden(request):
        raise AssertionError("Registration must not call any provider")
    with factory.begin() as db:
        owner = db.scalar(select(User).where(User.email == EMAIL))
        tenant_id = owner.tenant_id
        if cross_tenant:
            tenant = Tenant(name="KRX isolated test", timezone="Asia/Seoul")
            db.add(tenant); db.flush(); tenant_id = tenant.id
        other = User(tenant_id=tenant_id, email="krx-other@example.com", role=role,
                     active=True, code_hash=hash_code(CODE))
        db.add(other); db.flush()
        db.add(UserSettings(user_id=other.id, tenant_id=tenant_id, value={"unrelated": "keep"}))
        other_token = create_session(db, other, 24)
        other_id, owner_id = other.id, owner.id
    path = "/api/user/connections/krx"
    key = "synthetic-krx-owner-key-1234567890"
    with TestClient(create_app(config, dimension_transport=httpx.MockTransport(forbidden)), headers={"origin": ORIGIN}) as client:
        assert client.get(path).status_code == 401
        client.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE})
        # A new KRX setting must preserve other service settings and preferences.
        assert client.put("/api/user/connections/ecos", json={"version": 0, "enabled": True, "fields": {"api_key": "synthetic-ecos-preserve"}}).status_code == 200
        with factory() as db:
            before = dict(db.get(UserSettings, owner_id).value)
        saved = client.put(path, json={"version": 0, "enabled": True, "fields": {"api_key": key}})
        assert saved.status_code == 200 and saved.json()["fields"] == {}
        assert key not in saved.text and key not in client.get(path).text
        assert saved.json()["secret_configured"] and saved.json()["enabled"]
        assert client.put(path, json={"version": 0, "enabled": True, "fields": {}}).status_code == 409
        client.post("/api/auth/logout")
        client.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE})
        assert client.get(path).json()["secret_configured"]
        # Blank secret preserves the key; explicit disable persists without a fallback.
        assert client.put(path, json={"version": 1, "enabled": False, "fields": {"api_key": ""}}).status_code == 200
        with factory() as db:
            owner = db.get(User, owner_id)
            setting = db.get(UserSettings, owner_id).value
            assert key not in json.dumps(setting)
            assert values(db, owner, config, "krx") == {"enabled": False, "api_key": key}
            for name, value in before.items():
                if name != "personal_connections":
                    assert setting[name] == value
            assert setting["personal_connections"]["ecos"] == before["personal_connections"]["ecos"]
        client.post("/api/auth/logout")
        # The login form is configured for the local tenant. A separately
        # authenticated cross-tenant session exercises ownership independently.
        client.cookies.clear()
        client.cookies.set("trading_session", other_token)
        assert not client.get(path, params={"owner_id": owner_id, "tenant_id": "foreign"}).json()["configured"]
        assert client.get(path + "/" + owner_id).status_code == 404
        response = client.put(path, json={"version": 0, "enabled": True, "fields": {"api_key": "synthetic-other-key"}})
        assert response.status_code == (403 if role == "viewer" else 200)
        with factory() as db:
            assert values(db, db.get(User, owner_id), config, "krx")["api_key"] == key
            assert db.get(UserSettings, other_id).value["unrelated"] == "keep"
        # An account switch never exposes the previous account's secret or state.
        client.post("/api/auth/logout")
        client.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE})
        assert client.get(path).json()["enabled"] is False


@pytest.mark.parametrize("key", ["", "sample", "has space in key", "x" * 513, 12345678, "한글인증키테스트"])
def test_krx_invalid_input_is_rejected_without_secret_echo(setup, key):
    config = setup[0].model_copy(update={"broker_encryption_key": SecretStr(Fernet.generate_key().decode())})
    with TestClient(create_app(config), headers={"origin": ORIGIN}) as client:
        client.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE})
        response = client.put("/api/user/connections/krx", json={"version": 0, "enabled": True, "fields": {"api_key": key}})
        assert response.status_code == 422
        assert not client.get("/api/user/connections/krx").json()["configured"]
