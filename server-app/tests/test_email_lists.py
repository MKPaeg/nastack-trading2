from conftest import CODE, EMAIL, ORIGIN
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr

from nastack_trading.app import create_app


def test_email_lists_validation_sending_and_versions(setup, monkeypatch):
    import nastack_trading.email_lists as module

    calls = []

    def fake(config, recipients, subject, body):
        calls.append((recipients, subject, body))
        return len(recipients)

    monkeypatch.setattr(module, "send_email_message", fake)
    config = setup[0].model_copy(update={"broker_encryption_key": SecretStr(Fernet.generate_key().decode())})
    with TestClient(create_app(config), headers={"origin": ORIGIN}) as client:
        path = "/api/admin/email-lists"
        assert client.get(path).status_code == 401
        client.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE})
        payload = {"version": 0, "name": "테스트", "emails": ["User@example.com", "user@example.com"]}
        item = client.put(path + "/new", json=payload).json()
        assert item["emails"] == ["user@example.com"]
        assert item["trade_notifications"] is False
        assert calls == []
        assert (
            client.put(path + "/new", json={**payload, "emails": ["bad\r\nBcc:a@b.com"]}).status_code == 422
        )
        assert client.put(path + "/" + item["id"], json=payload).status_code == 409
        send_path = path + "/" + item["id"] + "/send"
        assert client.post(send_path, json={"subject": "test", "body": "body"}).status_code == 409
        assert calls == []
        assert (
            client.put(
                "/api/user/connections/smtp",
                json={
                    "version": 0,
                    "enabled": True,
                    "fields": {
                        "host": "smtp.gmail.com",
                        "port": 587,
                        "username": EMAIL,
                        "password": "synthetic-only",
                        "from_email": EMAIL,
                    },
                },
            ).status_code
            == 200
        )
        assert client.post(send_path, json={"subject": "bad\r\nX:test", "body": "test"}).status_code == 422
        assert client.post(send_path, json={"subject": "test", "body": "body"}).json()["recipient_count"] == 1
        assert len(calls) == 1
        assert client.delete(path + "/" + item["id"] + "?version=0").status_code == 409
        assert client.delete(path + "/" + item["id"] + "?version=1").status_code == 200
        assert client.get(path).json()["items"] == []


def test_telegram_env_and_legacy_chat_do_not_override_personal_settings(setup):
    from sqlalchemy import select

    from nastack_trading.models import User, UserSettings
    from nastack_trading.telegram_settings import preferences

    with setup[1]() as db:
        user = db.scalar(select(User).where(User.email == EMAIL))
        row = db.get(UserSettings, user.id)
        row.value = {**row.value, "telegram_trading": {"chat_id": "-111"}}
        db.commit()
        config = setup[0].model_copy(update={"telegram_chat_id": "-222"})
        assert preferences(db, user, config)["chat_id"] == ""
        assert preferences(db, user, config)["chat_id_source"] == "not_configured"
