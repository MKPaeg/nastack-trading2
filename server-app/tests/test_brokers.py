import logging
from datetime import timedelta

import httpx
import pytest
from conftest import CODE, EMAIL, ORIGIN
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

from nastack_trading.app import create_app
from nastack_trading.brokers import AUTH_URL, BrokerFailure, NamuhAuth
from nastack_trading.db import now
from nastack_trading.models import AuditEvent, BrokerConnection, BrokerProbe, Tenant, User

PATH = "/api/admin/brokers/namuh"


def configured(config, **changes):
    from pydantic import SecretStr

    return config.model_copy(
        update={
            "namuh_owner_email": EMAIL,
            "namuh_app_key": SecretStr("synthetic-key"),
            "namuh_app_secret": SecretStr("synthetic-secret"),
            "broker_encryption_key": SecretStr(Fernet.generate_key().decode()),
            **changes,
        }
    )


def logged_in(config, transport):
    client = TestClient(create_app(config, broker_transport=transport), headers={"origin": ORIGIN})
    client.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE})
    return client


def release_cooldown(factory):
    with factory.begin() as db:
        row = db.scalar(select(BrokerConnection))
        row.next_probe_at = now() - timedelta(seconds=1)


def test_auth_contract_encryption_reuse_and_ownership(setup, caplog):
    calls = []

    def respond(request):
        calls.append(request)
        assert str(request.url).split("?")[0] == AUTH_URL
        assert request.method == "POST"
        assert request.url.params["appkey"] == "synthetic-key"
        assert request.url.params["appsecretkey"] == "synthetic-secret"
        assert request.url.params["scope"] == "oob"
        assert request.content == b""
        return httpx.Response(200, json={"access_token": "synthetic-token", "expires_in": 86400})

    config = configured(setup[0])
    with logged_in(config, httpx.MockTransport(respond)) as client:
        policy = client.get(PATH).json()
        assert policy["version"] == 0 and not policy["enabled"] and not policy["orders_supported"]
        assert client.post(PATH + "/verify", json={"version": 1}).status_code == 409
        assert (
            client.put(PATH, json={"version": 0, "enabled": True, "environment": "mock"}).status_code == 200
        )
        assert client.put(PATH, json={"version": 0, "enabled": True}).status_code == 409
        with caplog.at_level(logging.INFO, logger="httpx"):
            result = client.post(PATH + "/verify", json={"version": 1})
        assert result.status_code == 200
        assert result.json()["last_probe"]["status"] == "completed"
        assert "synthetic-secret" not in caplog.text
        assert "synthetic-token" not in result.text and "token_ciphertext" not in result.text
        assert client.post(PATH + "/verify", json={"version": 1}).status_code == 429
        with setup[1]() as db:
            row = db.scalar(select(BrokerConnection))
            assert row.token_ciphertext != "synthetic-token"
            assert (
                Fernet(config.broker_encryption_key.get_secret_value()).decrypt(row.token_ciphertext.encode())
                == b"synthetic-token"
            )
            assert db.scalar(select(BrokerProbe)).status == "completed"
            assert db.scalar(select(AuditEvent).where(AuditEvent.action == "broker.auth_checked"))
        release_cooldown(setup[1])
        assert client.post(PATH + "/verify", json={"version": 1}).json()["last_probe"]["reused_token"]
        assert len(calls) == 1
        assert (
            client.put(PATH, json={"version": 1, "enabled": False, "environment": "live"}).status_code == 200
        )
        assert client.get(PATH).json()["token_expires_at"] is None
        assert client.post(PATH + "/verify", json={"version": 2}).status_code == 409
        other = client.post("/api/users", json={"email": "viewer@example.com"}).json()
        client.post("/api/auth/logout")
        client.post("/api/auth/login", json={"email": other["email"], "access_code": other["access_code"]})
        assert client.get(PATH).status_code == 403
        assert client.post(PATH + "/verify", json={"version": 2}).status_code == 403


@pytest.mark.parametrize(
    "response,expected",
    [
        (httpx.Response(429, text="synthetic-secret"), "RATE_LIMITED"),
        (httpx.Response(302, headers={"location": "https://untrusted.example"}), "AUTH_REJECTED"),
        (httpx.Response(403, text="synthetic-secret"), "AUTH_REJECTED"),
        (httpx.Response(200, json={"rsp_cd": "00000"}), "INVALID_RESPONSE"),
        (httpx.Response(200, json={"access_token": "token", "expires_in": True}), "INVALID_RESPONSE"),
        (httpx.Response(200, json={"access_token": "token", "expires_in": 86401}), "INVALID_RESPONSE"),
        (httpx.Response(200, json={"access_token": "token\n", "expires_in": 86400}), "INVALID_RESPONSE"),
        (httpx.Response(200, text="x" * 65537), "INVALID_RESPONSE"),
        (httpx.Response(200, json=[]), "INVALID_RESPONSE"),
    ],
)
def test_provider_rejects_bad_responses_without_secrets(setup, response, expected):
    provider = NamuhAuth(configured(setup[0]), httpx.MockTransport(lambda request: response))
    with pytest.raises(BrokerFailure) as failure:
        provider.verify(BrokerConnection(), EMAIL)
    assert failure.value.code == expected
    assert "synthetic-secret" not in str(failure.value)


def test_owner_missing_keys_network_and_stale_policy(setup):
    calls = []

    def unexpected(request):
        calls.append(request)
        raise httpx.ReadTimeout("URL containing synthetic-secret")

    config = configured(setup[0])
    provider = NamuhAuth(config, httpx.MockTransport(unexpected))
    with pytest.raises(BrokerFailure, match="소유자"):
        provider.verify(BrokerConnection(), "someone@example.com")
    assert not calls
    with pytest.raises(BrokerFailure) as failure:
        provider.verify(BrokerConnection(), EMAIL)
    assert failure.value.code == "AUTH_TIMEOUT" and "synthetic-secret" not in str(failure.value)
    with logged_in(setup[0], httpx.MockTransport(unexpected)) as client:
        client.put(PATH, json={"version": 0, "enabled": True})
        assert client.post(PATH + "/verify", json={"version": 1}).json()["last_probe"]["status"] == "failed"
    assert len(calls) == 1
    release_cooldown(setup[1])

    def change_policy(request):
        with setup[1].begin() as db:
            row = db.scalar(select(BrokerConnection))
            row.enabled = False
            row.version += 1
        return httpx.Response(200, json={"access_token": "synthetic-token", "expires_in": 86400})

    with logged_in(config, httpx.MockTransport(change_policy)) as client:
        result = client.post(PATH + "/verify", json={"version": 1}).json()
        assert result["last_probe"]["status"] == "failed"
        assert result["token_expires_at"] is None
        assert "변경" in result["last_probe"]["message"]


def test_duplicate_owner_email_across_tenants_does_not_share_server_key(setup):
    with setup[1].begin() as db:
        existing = db.scalar(select(User))
        tenant = Tenant(name="Isolated", timezone="Asia/Seoul")
        db.add(tenant)
        db.flush()
        db.add(User(tenant_id=tenant.id, email=EMAIL, role="admin", code_hash=existing.code_hash))
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"access_token": "never-used", "expires_in": 86400})

    with logged_in(configured(setup[0]), httpx.MockTransport(respond)) as client:
        assert not client.get(PATH).json()["key_owner_matches"]
        client.put(PATH, json={"version": 0, "enabled": True})
        result = client.post(PATH + "/verify", json={"version": 1}).json()
        assert result["last_probe"]["status"] == "failed"
        assert not calls
