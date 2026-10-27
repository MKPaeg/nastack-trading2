import pytest
from conftest import CODE, EMAIL, ORIGIN
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from nastack_trading.app import create_app
from nastack_trading.models import User, UserSettings
from nastack_trading.personal_connections import external_config
from nastack_trading.security import create_session, hash_code


@pytest.mark.parametrize("cross_tenant,role", [(False, "trader"), (True, "trader"), (True, "viewer")])
def test_notification_connections_never_inherit_server_or_other_user(setup, cross_tenant, role):
    from nastack_trading.email_lists import saved_lists
    from nastack_trading.models import Tenant
    from nastack_trading.telegram_settings import preferences

    config = setup[0].model_copy(
        update={
            "broker_encryption_key": SecretStr(Fernet.generate_key().decode()),
            "smtp_host": "smtp.gmail.com",
            "smtp_password": SecretStr("server-login-only"),
            "smtp_from_email": EMAIL,
            "telegram_bot_token": SecretStr("server-token"),
            "telegram_chat_id": "-999",
            "telegram_owner_email": EMAIL,
        }
    )
    with setup[1].begin() as db:
        admin = db.scalar(select(User).where(User.email == EMAIL))
        tenant_id = admin.tenant_id
        if cross_tenant:
            tenant = Tenant(name="Notification isolation", timezone="Asia/Seoul")
            db.add(tenant)
            db.flush()
            tenant_id = tenant.id
        other = User(
            tenant_id=tenant_id, email="notify@example.com", role=role, active=True, code_hash=hash_code(CODE)
        )
        db.add(other)
        db.flush()
        other_id = other.id
        db.add(UserSettings(user_id=other.id, tenant_id=tenant_id, value={}))
        other_token = create_session(db, other, 24)
    with TestClient(create_app(config), headers={"origin": ORIGIN}) as client:
        client.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE})
        for service, fields in [
            (
                "smtp",
                {
                    "host": "smtp.gmail.com",
                    "port": 587,
                    "username": EMAIL,
                    "password": "admin-personal",
                    "from_email": EMAIL,
                },
            ),
            ("telegram", {"bot_token": "admin-personal-token", "chat_id": "-123"}),
        ]:
            assert (
                client.put(
                    f"/api/user/connections/{service}", json={"version": 0, "enabled": True, "fields": fields}
                ).status_code
                == 200
            )
        item = client.put(
            "/api/admin/email-lists/new",
            json={"version": 0, "name": "Admin own", "emails": [EMAIL], "trade_notifications": True},
        ).json()
        client.post("/api/auth/logout")
        client.cookies.clear()
        client.cookies.set("trading_session", other_token)
        assert client.get("/api/admin/email-lists").json() == {"items": [], "smtp_configured": False}
        assert client.post(
            f"/api/admin/email-lists/{item['id']}/send", json={"subject": "test", "body": "test"}
        ).status_code in (403, 404)
        if role == "viewer":
            assert (
                client.put(
                    "/api/user/connections/telegram", json={"version": 0, "enabled": False, "fields": {}}
                ).status_code
                == 403
            )
    with setup[1]() as db:
        other = db.get(User, other_id)
        scoped = external_config(db, other, config)
        assert not scoped.smtp_host and not scoped.smtp_password.get_secret_value()
        assert not scoped.telegram_bot_token.get_secret_value()
        assert not preferences(db, other, config)["token_configured"]
        assert saved_lists(db, other) == []
        admin = db.scalar(select(User).where(User.email == EMAIL))
        assert external_config(db, admin, config).smtp_password.get_secret_value() == "admin-personal"


def test_personal_connections_are_encrypted_isolated_and_safe(setup):
    config = setup[0].model_copy(update={"broker_encryption_key": SecretStr(Fernet.generate_key().decode())})
    with setup[1]() as db:
        owner = db.scalar(select(User).where(User.email == EMAIL))
        other = User(
            tenant_id=owner.tenant_id,
            email="private@example.com",
            role="trader",
            active=True,
            code_hash=hash_code(CODE),
        )
        db.add(other)
        db.flush()
        db.add(
            UserSettings(
                user_id=other.id, tenant_id=other.tenant_id, value=dict(db.get(UserSettings, owner.id).value)
            )
        )
        db.commit()
        other_id = other.id
    with TestClient(create_app(config), headers={"origin": ORIGIN}) as client:
        client.post("/api/auth/login", json={"email": "private@example.com", "access_code": CODE})
        for service, fields in [
            ("openai", {"api_key": "synthetic-private-key"}),
            ("telegram", {"bot_token": "synthetic-private-token", "chat_id": "-123"}),
            (
                "smtp",
                {
                    "host": "smtp.gmail.com",
                    "port": 587,
                    "username": "private@example.com",
                    "password": "synthetic-private-pass",
                    "from_email": "private@example.com",
                },
            ),
        ]:
            path = f"/api/user/connections/{service}"
            result = client.put(path, json={"version": 0, "enabled": True, "fields": fields})
            assert result.status_code == 200, result.text
            assert "synthetic-private" not in result.text
            assert client.get(path).json()["configured"]
            assert client.put(path, json={"version": 0, "enabled": True, "fields": fields}).status_code == 409
        bad = client.put(
            "/api/user/connections/smtp",
            json={"version": 1, "enabled": True, "fields": {"host": "127.0.0.1"}},
        )
        assert bad.status_code == 422
        assert "127.0.0.1" not in bad.text
        assert client.get("/api/admin/trading/notifications").json()["owner_matches"]
        with setup[1]() as db:
            user = db.get(User, other_id)
            assert (
                external_config(db, user, config).openai_api_key.get_secret_value() == "synthetic-private-key"
            )
            assert (
                external_config(db, owner, config).openai_api_key.get_secret_value()
                != "synthetic-private-key"
            )
            assert "synthetic-private" not in str(db.get(UserSettings, other_id).value)
        assert (
            client.put(
                "/api/user/connections/openai", json={"version": 1, "enabled": False, "fields": {}}
            ).status_code
            == 200
        )
        with setup[1]() as db:
            assert not external_config(db, db.get(User, other_id), config).openai_api_key.get_secret_value()
