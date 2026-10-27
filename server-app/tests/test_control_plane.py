from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from conftest import CODE, EMAIL, ORIGIN, enqueue
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from nastack_trading.app import create_app
from nastack_trading.db import now
from nastack_trading.jobs import claim_job, transition
from nastack_trading.models import AnalysisRun, AuditEvent, Base, SystemLog, User, UserSettings
from nastack_trading.security import bootstrap
from nastack_trading.worker import work_once


def test_migration_matches_models(setup):
    _, factory = setup
    with factory().bind.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []


def test_auth_and_origin_and_asset_boundaries(client):
    assert client.get("/api/runs").status_code == 401
    assert client.get("/api/events").status_code == 401
    assert (
        client.post(
            "/api/auth/login",
            headers={"origin": "https://attacker.example"},
            json={"email": EMAIL, "access_code": CODE},
        ).status_code
        == 403
    )
    assert client.post("/api/auth/login", json={"email": EMAIL, "access_code": "wrong"}).status_code == 401
    login = client.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE})
    assert login.status_code == 200
    assert "HttpOnly" in login.headers["set-cookie"] and "SameSite=strict" in login.headers["set-cookie"]
    for path in ("/.env", "/.git/config", "/docs/NaStack_Stock_Agent_Design_Notes.md", "/pyproject.toml"):
        assert client.get(path).status_code == 404
    assert client.get("/app.js").status_code == 200
    assert client.get("/api/health").json()["timezone"] == "Asia/Seoul"
    assert client.post("/api/auth/logout").status_code == 204
    assert client.get("/api/me").status_code == 401


def test_login_throttle_survives_api_restart(client, setup):
    for _ in range(10):
        assert client.post("/api/auth/login", json={"email": EMAIL, "access_code": "bad"}).status_code == 401
    with TestClient(create_app(setup[0]), headers={"origin": ORIGIN}) as second:
        assert second.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE}).status_code == 429


def test_bootstrap_does_not_reset_credentials(admin, setup):
    config, factory = setup
    bootstrap(factory, config.model_copy(update={"admin_email": "other@example.com"}))
    assert len(admin.get("/api/users").json()) == 1


def test_settings_persist_and_conflicting_write_rejected(admin, setup):
    current = admin.get("/api/settings").json()
    current["value"].update(theme="slate", sources=[{"domain": "dart.fss.or.kr", "disposition": "use"}])
    assert admin.put("/api/settings", json=current).json()["version"] == 2
    assert admin.put("/api/settings", json=current).status_code == 409
    with TestClient(create_app(setup[0]), headers={"origin": ORIGIN}) as second:
        second.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE})
        saved = second.get("/api/settings").json()
        assert saved["value"]["theme"] == "slate"
        assert saved["value"]["sources"][0]["domain"] == "dart.fss.or.kr"


@pytest.mark.parametrize(
    "field,value",
    [
        ("page_size", 7),
        ("analysis_model", {"provider": "ollama"}),
        ("analysis_model", {"base_url": "http://user:secret@server/v1"}),
        ("api_key", "must-not-be-stored"),
    ],
)
def test_settings_validation(admin, field, value):
    current = admin.get("/api/settings").json()
    current["value"][field] = value
    assert admin.put("/api/settings", json=current).status_code == 422


def test_job_idempotency_and_persistence_after_restart(admin, setup):
    run = enqueue(admin)
    assert enqueue(admin)["id"] == run["id"]
    assert (
        admin.post("/api/runs", json={"symbol": "000660", "request_key": "request-key-one"}).status_code
        == 409
    )
    assert work_once(setup[1], pause=lambda _: None)
    done = admin.get(f"/api/runs/{run['id']}").json()
    assert done["status"] == "completed" and done["progress"] == 100
    assert done["result"]["simulation"] and done["result"]["action"] == "abstain"
    assert done["finished_at"].endswith("+09:00")
    with TestClient(create_app(setup[0]), headers={"origin": ORIGIN}) as restarted:
        restarted.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE})
        assert restarted.get(f"/api/runs/{run['id']}").json()["result"] == done["result"]
        assert restarted.get("/api/logs").json()["total"] >= 4
        actions = [entry["action"] for entry in restarted.get("/api/audit").json()]
        assert "analysis.completed" in actions and "analysis.started" in actions


def test_failure_persisted_and_provider_secret_not_logged(admin, setup):
    run = enqueue(admin)

    def fail(_):
        raise RuntimeError("api_key=secret-account-token")

    assert work_once(setup[1], pause=lambda _: None, evaluator=fail)
    failed = admin.get(f"/api/runs/{run['id']}").json()
    assert failed["status"] == "failed" and failed["error_code"] == "PROCESSING_FAILED"
    assert "secret-account-token" not in admin.get("/api/logs").text + admin.get("/api/audit").text


def test_settings_change_invalidates_pending_job(admin, setup):
    run = enqueue(admin)
    current = admin.get("/api/settings").json()
    current["value"]["sources"] = [{"domain": "example.com", "disposition": "exclude"}]
    admin.put("/api/settings", json=current)
    work_once(setup[1], pause=lambda _: None)
    assert admin.get(f"/api/runs/{run['id']}").json()["error_code"] == "SETTINGS_CHANGED"


def test_worker_crash_lease_recovery_and_old_worker_fencing(admin, setup):
    run = enqueue(admin)
    _, old = claim_job(setup[1])
    assert claim_job(setup[1]) is None
    with setup[1].begin() as db:
        db.get(AnalysisRun, run["id"]).lease_until = now() - timedelta(seconds=1)
    _, new = claim_job(setup[1])
    assert old != new
    assert not transition(setup[1], run["id"], old, progress=100, message="old", status="completed")
    assert transition(
        setup[1],
        run["id"],
        new,
        progress=100,
        message="recovered",
        status="completed",
        result={"simulation": True},
    )
    assert admin.get(f"/api/runs/{run['id']}").json()["attempts"] == 2


def test_user_isolation_and_permission_revoke(admin, setup):
    own = enqueue(admin)
    invited = admin.post("/api/users", json={"email": "trader@example.com", "role": "trader"}).json()
    with TestClient(create_app(setup[0]), headers={"origin": ORIGIN}) as trader:
        assert (
            trader.post(
                "/api/auth/login", json={"email": invited["email"], "access_code": invited["access_code"]}
            ).status_code
            == 200
        )
        assert trader.get("/api/runs").json()["total"] == 0
        assert trader.get(f"/api/runs/{own['id']}").status_code == 404
        assert trader.get("/api/users").status_code == 403
        pending = enqueue(trader, key="trader-request")
        assert trader.delete("/api/logs").status_code == 403
        admin.patch(f"/api/users/{invited['id']}", json={"role": "viewer", "active": True})
        assert (
            trader.post("/api/runs", json={"symbol": "005930", "request_key": "viewer-request"}).status_code
            == 403
        )
        work_once(setup[1], pause=lambda _: None)  # administrator's first job
        work_once(setup[1], pause=lambda _: None)
        assert trader.get(f"/api/runs/{pending['id']}").json()["error_code"] == "PERMISSION_CHANGED"
        assert trader.put("/api/settings", json=trader.get("/api/settings").json()).status_code == 403
        admin.patch(f"/api/users/{invited['id']}", json={"role": "viewer", "active": False})
        assert trader.get("/api/me").status_code == 401
    last = admin.get("/api/me").json()
    assert (
        admin.patch(f"/api/users/{last['id']}", json={"role": "viewer", "active": False}).status_code == 409
    )


def test_log_cleanup_preserves_audit_and_run_history(admin, setup):
    run = enqueue(admin)
    user_id = admin.get("/api/me").json()["id"]
    with setup[1].begin() as db:
        user = db.get(User, user_id)
        db.add(
            SystemLog(
                tenant_id=user.tenant_id,
                owner_id=user.id,
                message="old-log",
                created_at=now() - timedelta(days=31),
            )
        )
    assert admin.delete("/api/logs?older_than_days=30").json()["deleted"] == 1
    assert admin.get(f"/api/runs/{run['id']}").status_code == 200
    assert any(entry["action"] == "logs.pruned" for entry in admin.get("/api/audit").json())


def test_pagination(admin):
    for index in range(6):
        enqueue(admin, key=f"pagination-{index}")
    first = admin.get("/api/runs?page=1&page_size=5").json()
    second = admin.get("/api/runs?page=2&page_size=5").json()
    assert first["total"] == 6 and len(first["items"]) == 5 and len(second["items"]) == 1
    assert admin.get("/api/runs?page_size=7").status_code == 422


def test_postgres_concurrent_claims(admin, setup):
    config, factory = setup
    if not config.database_url.startswith("postgresql"):
        pytest.skip("PostgreSQL concurrency check requires TRADING_TEST_DATABASE_URL")
    run = enqueue(admin)
    with ThreadPoolExecutor(max_workers=4) as executor:
        claims = list(executor.map(lambda _: claim_job(factory), range(4)))
    assert len([claim for claim in claims if claim]) == 1
    with factory() as db:
        assert db.get(AnalysisRun, run["id"]).attempts == 1
        assert (
            db.scalar(
                select(func.count()).select_from(AuditEvent).where(AuditEvent.action == "analysis.started")
            )
            == 1
        )
        assert db.scalar(select(func.count()).select_from(UserSettings)) == 1
