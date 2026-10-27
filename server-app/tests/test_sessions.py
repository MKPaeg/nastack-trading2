from datetime import datetime, timedelta

from conftest import CODE, EMAIL, ORIGIN
from fastapi.testclient import TestClient
from sqlalchemy import select

from nastack_trading.app import create_app
from nastack_trading.db import aware, now
from nastack_trading.models import AuthSession


def test_selected_duration_cookie_db_and_reload(client, setup):
    options = client.get("/api/auth/options").json()
    assert options == {"default_session_hours": 24, "max_session_hours": 168}
    response = client.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE})
    assert response.status_code == 200 and "Max-Age=31536000" in response.headers["set-cookie"]
    assert "HttpOnly" in response.headers["set-cookie"]
    assert response.json()["session_expires_at"] is None
    assert response.json()["session_unlimited"] is True
    with TestClient(create_app(setup[0]), headers={"origin": ORIGIN}) as restarted:
        restarted.cookies.update(client.cookies)
        assert restarted.get("/api/me").status_code == 200
        assert restarted.get("/api/me").json()["session_expires_at"] == response.json()["session_expires_at"]
    with setup[1].begin() as db:
        session = db.scalar(select(AuthSession))
        assert session.expires_at is None
        session.expires_at = now() - timedelta(seconds=1)
    assert client.get("/api/me").status_code == 401


def test_admin_default_and_maximum_persist(admin, setup):
    policy = admin.get("/api/admin/settings").json()
    response = admin.put(
        "/api/admin/settings", json={**policy, "default_session_hours": 4, "max_session_hours": 24}
    )
    assert response.status_code == 200
    assert admin.put("/api/admin/settings", json=policy).status_code == 409
    assert (
        admin.post("/api/users", json={"email": "four@example.com", "role": "trader"}).json()["session_hours"]
        == 4
    )
    for hours in (0, 25, 73):
        assert (
            admin.post(
                "/api/users", json={"email": "invalid@example.com", "session_hours": hours}
            ).status_code
            == 422
        )
    with TestClient(create_app(setup[0]), headers={"origin": ORIGIN}) as restarted:
        assert restarted.get("/api/auth/options").json() == {
            "default_session_hours": 4,
            "max_session_hours": 24,
        }


def test_only_admin_assigns_duration_and_applies_to_active_sessions(admin, setup):
    invited = admin.post(
        "/api/users", json={"email": "session@example.com", "role": "trader", "session_hours": 4}
    ).json()
    with TestClient(create_app(setup[0]), headers={"origin": ORIGIN}) as other:
        response = other.post(
            "/api/auth/login", json={"email": invited["email"], "access_code": invited["access_code"]}
        )
        assert "Max-Age=14400" in response.headers["set-cookie"]
        assert other.get("/api/admin/settings").status_code == 403
        payload = {"role": "trader", "active": True, "session_hours": 1}
        assert other.patch(f"/api/users/{invited['id']}", json=payload).status_code == 403
        assert admin.patch(f"/api/users/{invited['id']}", json=payload).status_code == 200
        refreshed = other.get("/api/me")
        assert refreshed.status_code == 200
        expires = datetime.fromisoformat(refreshed.json()["session_expires_at"])
        assert 3500 < (expires - now()).total_seconds() <= 3600
        assert refreshed.json()["session_unlimited"] is False
        response = other.post(
            "/api/auth/login", json={"email": invited["email"], "access_code": invited["access_code"]}
        )
        assert "Max-Age=3600" in response.headers["set-cookie"]
        assert admin.get("/api/me").status_code == 200


def test_login_cannot_choose_duration(client):
    response = client.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE, "session_hours": 72})
    assert response.status_code == 422


def test_db_remembered_email_is_not_authentication(client, setup):
    response = client.post(
        "/api/auth/login", json={"email": EMAIL, "access_code": CODE, "remember_email": True}
    )
    assert response.status_code == 200
    assert client.get("/api/auth/remembered-email").json()["email"] == EMAIL
    client.post("/api/auth/logout")
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/auth/remembered-email").json()["email"] == EMAIL
    with TestClient(create_app(setup[0]), headers={"origin": ORIGIN}) as restarted:
        restarted.cookies.update(client.cookies)
        assert restarted.get("/api/auth/remembered-email").json()["email"] == EMAIL
        assert restarted.delete("/api/auth/remembered-email").status_code == 204
        assert restarted.get("/api/auth/remembered-email").json()["email"] == ""


def test_logout_revokes_session_early(admin):
    old_cookies = dict(admin.cookies)
    assert admin.post("/api/auth/logout").status_code == 204
    admin.cookies.update(old_cookies)
    assert admin.get("/api/me").status_code == 401


def test_168_limit_admin_self_save_and_no_expired_session_revival(admin, setup):
    me = admin.get("/api/me").json()
    assert me["session_unlimited"] and me["session_expires_at"] is None
    assert (
        admin.patch(
            f"/api/users/{me['id']}", json={"role": "admin", "active": True, "session_hours": 168}
        ).status_code
        == 200
    )
    assert admin.get("/api/me").status_code == 200
    invited = admin.post(
        "/api/users", json={"email": "long@example.com", "role": "trader", "session_hours": 168}
    ).json()
    assert invited["session_hours"] == 168
    assert (
        admin.patch(
            f"/api/users/{invited['id']}", json={"role": "trader", "active": True, "session_hours": 169}
        ).status_code
        == 422
    )
    expired = now() - timedelta(hours=1)
    with setup[1].begin() as db:
        db.add(AuthSession(token_hash="expired-never-revive", user_id=invited["id"], expires_at=expired))
        db.add(
            AuthSession(
                token_hash="revoked-never-revive",
                user_id=invited["id"],
                expires_at=now() + timedelta(hours=1),
                revoked_at=now(),
            )
        )
    assert (
        admin.patch(
            f"/api/users/{invited['id']}", json={"role": "trader", "active": True, "session_hours": 72}
        ).status_code
        == 200
    )
    with setup[1]() as db:
        assert aware(db.get(AuthSession, "expired-never-revive").expires_at) == expired
        assert db.get(AuthSession, "revoked-never-revive").revoked_at is not None
    rows = admin.get("/api/admin/sessions?status=active").json()["items"]
    assert any(r["user_id"] == me["id"] and r["unlimited"] for r in rows)


def test_sessions_count_separate_browsers_expiry_and_logout(admin, setup):
    def count():
        return next(row["valid_sessions"] for row in admin.get("/api/users").json() if row["email"] == EMAIL)

    assert count() == 1
    with TestClient(create_app(setup[0]), headers={"origin": ORIGIN}) as phone:
        phone.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE})
        assert phone.get("/api/me").status_code == 200
        assert admin.get("/api/me").status_code == 200
        assert count() == 2
        # A re-login on the same browser replaces only its own previous session.
        phone.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE})
        assert count() == 2
        with setup[1].begin() as db:
            user_id = db.scalar(select(AuthSession.user_id))
            db.add(
                AuthSession(
                    token_hash="expired-synthetic-session",
                    user_id=user_id,
                    expires_at=now() - timedelta(seconds=1),
                )
            )
        assert count() == 2
        phone.post("/api/auth/logout")
        assert count() == 1 and admin.get("/api/me").status_code == 200
