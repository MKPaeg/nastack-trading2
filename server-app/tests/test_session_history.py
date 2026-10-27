from datetime import timedelta

from conftest import CODE, EMAIL, ORIGIN
from fastapi.testclient import TestClient
from sqlalchemy import select

from nastack_trading.app import create_app
from nastack_trading.db import now
from nastack_trading.models import AuthSession, Tenant, User
from nastack_trading.security import create_session, token_digest

PATH = "/api/admin/sessions"
IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1"


def test_history_devices_revocation_expiry_no_tokens(admin, setup):
    with TestClient(create_app(setup[0]), headers={"origin": ORIGIN, "user-agent": IPHONE}) as phone:
        phone.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE})
        items = admin.get(PATH + "?status=active").json()["items"]
        assert len(items) == 2
        row = next(row for row in items if row["device"] == "아이폰")
        assert row["browser"] == "Safari" and not row["is_current"]
        assert row["created_at"].endswith("+09:00")
        token = phone.cookies.get("trading_session")
        assert token not in admin.get(PATH).text and token_digest(token) not in admin.get(PATH).text
        assert "user_agent" not in row
        phone.post("/api/auth/logout")
        assert phone.get("/api/me").status_code == 401
        assert admin.get(PATH + "?status=active").json()["total"] == 1
        row = admin.get(PATH + "?status=revoked").json()["items"][0]
        assert row["end_reason"] == "logout" and row["revoked_at"]
        with setup[1].begin() as db:
            user = db.scalar(select(User))
            expired = create_session(db, user, 1)
            db.flush()
            db.get(AuthSession, token_digest(expired)).expires_at = now() - timedelta(seconds=1)
        assert admin.get(PATH + "?status=expired").json()["total"] == 1
        assert admin.get(PATH).json()["total"] == 3
        assert admin.get(PATH + "?page_size=6").status_code == 422
        assert admin.get(PATH + "?status=invalid").status_code == 422


def test_history_tenant_and_role_isolation(admin, setup):
    with setup[1].begin() as db:
        existing = db.scalar(select(User))
        tenant = Tenant(name="Other", timezone="Asia/Seoul")
        db.add(tenant)
        db.flush()
        user = User(
            tenant_id=tenant.id, email="other@example.com", role="admin", code_hash=existing.code_hash
        )
        db.add(user)
        db.flush()
        foreign = create_session(db, user, 24)
    assert admin.get(PATH).json()["total"] == 1
    assert admin.get(PATH + f"?user_id={user.id}").json()["total"] == 0
    with TestClient(create_app(setup[0]), headers={"origin": ORIGIN}) as other:
        other.cookies.set("trading_session", foreign)
        assert other.get(PATH).json()["total"] == 1
        assert other.get(PATH).json()["items"][0]["email"] == "other@example.com"
    invited = admin.post("/api/users", json={"email": "viewer@example.com"}).json()
    with TestClient(create_app(setup[0]), headers={"origin": ORIGIN}) as viewer:
        viewer.post(
            "/api/auth/login", json={"email": invited["email"], "access_code": invited["access_code"]}
        )
        assert viewer.get(PATH).status_code == 403


def test_legacy_session_first_observation_keeps_unknown_creation(admin, setup):
    token = admin.cookies.get("trading_session")
    with setup[1].begin() as db:
        session = db.get(AuthSession, token_digest(token))
        session.created_at = None
        session.metadata_recorded_at = None
        session.device = "기존 세션"
    response = admin.get(PATH, headers={"user-agent": IPHONE}).json()["items"][0]
    assert response["created_at"] is None
    assert response["device"] == "아이폰" and response["metadata_recorded_at"]
