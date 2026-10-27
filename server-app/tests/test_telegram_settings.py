from conftest import CODE, EMAIL, ORIGIN
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr

from nastack_trading.app import create_app


def test_telegram_preferences_and_explicit_test(setup, monkeypatch):
    import nastack_trading.telegram_settings as telegram

    calls = []

    def request(config, method, body):
        calls.append((method, body))
        return {"username": "synthetic_bot"}

    monkeypatch.setattr(telegram, "telegram_request", request)
    config = setup[0].model_copy(
        update={
            "telegram_bot_token": SecretStr("synthetic-secret"),
            "telegram_owner_email": EMAIL,
            "broker_encryption_key": SecretStr(Fernet.generate_key().decode()),
        }
    )
    with TestClient(create_app(config), headers={"origin": ORIGIN}) as client:
        client.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE})
        path = "/api/admin/trading/notifications"
        status = client.get(path)
        assert status.status_code == 200
        assert not status.json()["token_configured"]
        assert "synthetic-secret" not in status.text
        assert client.put(path, json={"version": 0, "enabled": True, "chat_id": ""}).status_code == 422
        assert client.post(path + "/test").status_code == 409
        assert calls == []
        assert (
            client.put(
                "/api/user/connections/telegram",
                json={
                    "version": 0,
                    "enabled": True,
                    "fields": {"bot_token": "personal-token", "chat_id": "-123456"},
                },
            ).status_code
            == 200
        )
        assert (
            client.put(path, json={"version": 0, "enabled": False, "chat_id": "-123456"}).status_code == 200
        )
        assert client.post(path + "/test").status_code == 409
        assert client.post(path + "/verify").json()["message_sent"] is False
        assert calls == [("getMe", {})]
        saved = client.put(path, json={"version": 1, "enabled": True, "chat_id": "-123456"})
        assert saved.status_code == 200, saved.text
        assert saved.json()["version"] == 2
        assert client.put(path, json={"version": 0, "enabled": False, "chat_id": ""}).status_code == 409
        assert client.post(path + "/test").status_code == 200
        assert calls[-1][0] == "sendMessage"
        assert calls[-1][1]["chat_id"] == "-123456"
