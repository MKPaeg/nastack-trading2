from datetime import timedelta

import pytest
from conftest import EMAIL, ORIGIN
from fastapi.testclient import TestClient
from sqlalchemy import select

from nastack_trading.app import create_app
from nastack_trading.db import now
from nastack_trading.models import LoginChallenge


@pytest.fixture
def email_client(setup):
    sent = []
    config = setup[0].model_copy(
        update={
            "legacy_access_code_login_enabled": False,
            "smtp_host": "test.mail",
            "smtp_from_email": "sender@example.com",
        }
    )

    def sender(config, email, code, minutes):
        sent.append((email, code, minutes))

    with TestClient(create_app(config, mail_sender=sender), headers={"origin": ORIGIN}) as client:
        yield client, sent


def test_email_code_success_once_and_no_static_code(email_client, setup):
    client, sent = email_client
    response = client.post("/api/auth/request-code", json={"email": EMAIL})
    assert response.status_code == 202
    assert len(sent) == 1 and sent[0][0] == EMAIL and len(sent[0][1]) == 6
    assert sent[0][1] not in response.text
    assert "HttpOnly" in response.headers["set-cookie"]
    cookie = dict(client.cookies)
    verify = {"email": EMAIL, "access_code": sent[0][1], "remember_email": True}
    response = client.post("/api/auth/verify", json=verify)
    assert response.status_code == 200 and "Max-Age=31536000" in response.headers["set-cookie"]
    assert client.get("/api/me").status_code == 200
    client.cookies.update(cookie)
    assert client.post("/api/auth/verify", json=verify).status_code == 401
    assert client.post("/api/auth/login", json=verify).status_code == 410
    with setup[1]() as db:
        assert db.scalar(select(LoginChallenge)) is None


def test_wrong_code_limit_and_expiry(email_client, setup):
    client, sent = email_client
    assert client.post("/api/auth/request-code", json={"email": EMAIL}).status_code == 202
    wrong = sent[0][1][:-1] + str((int(sent[0][1][-1]) + 1) % 10)
    for _ in range(5):
        assert client.post("/api/auth/verify", json={"email": EMAIL, "access_code": wrong}).status_code == 401
    assert (
        client.post("/api/auth/verify", json={"email": EMAIL, "access_code": sent[0][1]}).status_code == 401
    )
    assert client.get("/api/me").status_code == 401


def test_expired_code_and_browser_binding(email_client, setup):
    client, sent = email_client
    client.post("/api/auth/request-code", json={"email": EMAIL})
    with TestClient(create_app(setup[0]), headers={"origin": ORIGIN}) as other:
        assert (
            other.post("/api/auth/verify", json={"email": EMAIL, "access_code": sent[0][1]}).status_code
            == 401
        )
    with setup[1].begin() as db:
        db.scalar(select(LoginChallenge)).expires_at = now() - timedelta(seconds=1)
    assert (
        client.post("/api/auth/verify", json={"email": EMAIL, "access_code": sent[0][1]}).status_code == 401
    )


def test_unknown_email_generic_and_mail_rate_limit(email_client):
    client, sent = email_client
    response = client.post("/api/auth/request-code", json={"email": "unknown@example.com"})
    assert response.status_code == 202 and not sent
    assert client.post("/api/auth/request-code", json={"email": "unknown@example.com"}).status_code == 429


def test_smtp_failure_sanitized_no_usable_code(setup):
    config = setup[0].model_copy(update={"smtp_host": "test.mail", "smtp_from_email": "sender@example.com"})

    def failed(*args):
        raise RuntimeError("SMTP-password-sensitive")

    with TestClient(create_app(config, mail_sender=failed), headers={"origin": ORIGIN}) as client:
        response = client.post("/api/auth/request-code", json={"email": EMAIL})
        assert response.status_code == 503 and "SMTP-password-sensitive" not in response.text
        assert "set-cookie" not in response.headers
    with setup[1]() as db:
        assert db.scalar(select(LoginChallenge)) is None
