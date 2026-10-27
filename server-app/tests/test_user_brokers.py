from conftest import CODE, EMAIL, ORIGIN
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from nastack_trading.app import create_app
from nastack_trading.models import User, UserBrokerKey, UserSettings
from nastack_trading.security import hash_code
from nastack_trading.user_brokers import personal_config


def test_personal_keys_and_settings_are_isolated(setup):
    config = setup[0].model_copy(update={"broker_encryption_key": SecretStr(Fernet.generate_key().decode())})
    with setup[1]() as db:
        admin = db.scalar(select(User).where(User.email == EMAIL))
        other = User(
            tenant_id=admin.tenant_id,
            email="other@example.com",
            role="trader",
            active=True,
            code_hash=hash_code(CODE),
        )
        db.add(other)
        db.flush()
        db.add(
            UserSettings(
                user_id=other.id, tenant_id=other.tenant_id, value=dict(db.get(UserSettings, admin.id).value)
            )
        )
        db.commit()
        other_id = other.id
    app = create_app(config)
    with (
        TestClient(app, headers={"origin": ORIGIN}) as first,
        TestClient(app, headers={"origin": ORIGIN}) as second,
    ):
        first.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE})
        second.post("/api/auth/login", json={"email": "other@example.com", "access_code": CODE})
        for broker in ("namuh", "kiwoom"):
            path = f"/api/user/brokers/{broker}/keys"
            response = second.put(
                path, json={"version": 0, "app_key": "private-key", "app_secret": "private-secret"}
            )
            assert response.status_code == 200, response.text
            assert "private-" not in response.text
            assert first.get(path).json()["configured"] is False
            assert second.get(path).json()["configured"] is True
            assert (
                second.put(
                    path, json={"version": 0, "app_key": "different", "app_secret": "different"}
                ).status_code
                == 409
            )
        assert second.get("/api/user/brokers/namuh").json()["key_owner_matches"] is True
        assert second.get("/api/user/brokers/kiwoom").json()["key_owner_matches"] is True
        assert second.get("/api/users").status_code == 403
        invalid = second.put(
            "/api/user/brokers/namuh/keys", json={"version": 1, "app_key": "", "app_secret": "private-secret"}
        )
        assert invalid.status_code == 422 and "private-secret" not in invalid.text
        assert (
            second.put("/api/settings/screen-state", json={"view": "orders", "code": "123456"}).status_code
            == 200
        )
        assert second.get("/api/settings").json()["value"]["screen_state"]["view"] == "orders"
        assert "screen_state" not in first.get("/api/settings").json()["value"]
        with setup[1]() as db:
            row = db.get(UserSettings, other_id)
            row.value = {**row.value, "notification_email_lists": [{"id": "synthetic"}]}
            db.commit()
        settings = second.get("/api/settings").json()
        from nastack_trading.schemas import Preferences

        settings["value"] = {k: v for k, v in settings["value"].items() if k in Preferences.model_fields}
        assert second.put("/api/settings", json=settings).status_code == 200
        assert second.get("/api/settings").json()["value"]["notification_email_lists"] == [
            {"id": "synthetic"}
        ]
    with setup[1]() as db:
        user = db.get(User, other_id)
        scoped = personal_config(db, user, config, "namuh")
        assert scoped.namuh_app_key.get_secret_value() == "private-key"
        assert db.get(UserBrokerKey, (other_id, "namuh")).key_ciphertext != "private-key"
