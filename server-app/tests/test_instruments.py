from conftest import CODE, EMAIL, ORIGIN, enqueue
from fastapi.testclient import TestClient
from sqlalchemy import select

from nastack_trading.app import create_app
from nastack_trading.instruments import instrument_changed
from nastack_trading.jobs import claim_job, transition
from nastack_trading.models import AnalysisRun, AuditEvent, Tenant, User
from nastack_trading.security import create_session, hash_code

SECURITY = {
    "code": "123456",
    "name": "시험 회사",
    "market": "KOSPI",
    "asset_type": "stock",
    "status": "active",
    "source_note": "테스트 전용 합성 자료",
}


def test_registry_search_update_snapshot(admin, setup):
    result = admin.post("/api/admin/instruments", json=SECURITY)
    assert result.status_code == 201
    item = result.json()
    assert admin.post("/api/admin/instruments", json=SECURITY).status_code == 409
    assert admin.get("/api/instruments?q=시험").json()["total"] == 1
    assert admin.get("/api/instruments?q=1234").json()["total"] == 1
    assert admin.get("/api/instruments?q=%25").json()["total"] == 0
    assert admin.get("/api/instruments?page_size=6").status_code == 422
    run = enqueue(admin, symbol="123456")
    assert run["instrument_snapshot"]["name"] == "시험 회사"
    values = {key: value for key, value in SECURITY.items() if key != "code"}
    patch = {**values, "name": "변경 이름", "version": 1, "reason": "명칭 변경"}
    assert admin.put(f"/api/admin/instruments/{item['id']}", json=patch).status_code == 200
    assert admin.put(f"/api/admin/instruments/{item['id']}", json=patch).status_code == 409
    assert admin.get(f"/api/runs/{run['id']}").json()["instrument_snapshot"]["name"] == "시험 회사"
    with setup[1].begin() as db:
        record = db.get(AnalysisRun, run["id"])
        record.kind = "research"
        assert instrument_changed(db, record)
        assert (
            db.scalar(select(AuditEvent).where(AuditEvent.action == "instrument.updated")).detail["reason"]
            == "명칭 변경"
        )
    claimed = claim_job(setup[1])
    assert not transition(setup[1], *claimed, progress=35, message="단계 확인")
    assert admin.get(f"/api/runs/{run['id']}").json()["error_code"] == "INSTRUMENT_CHANGED"


def test_watchlist_owner_isolation_and_permissions(admin, client):
    item = admin.post("/api/admin/instruments", json=SECURITY).json()
    assert admin.post("/api/watchlist", json={"instrument_id": item["id"]}).status_code == 201
    assert admin.post("/api/watchlist", json={"instrument_id": item["id"]}).status_code == 409
    assert admin.get("/api/watchlist").json()["total"] == 1
    other = admin.post("/api/users", json={"email": "trader@example.com", "role": "trader"}).json()
    admin.post("/api/auth/logout")
    client.post("/api/auth/login", json={"email": other["email"], "access_code": other["access_code"]})
    assert client.get("/api/watchlist").json()["total"] == 0
    assert client.delete(f"/api/watchlist/{item['id']}").status_code == 404
    assert client.post("/api/admin/instruments", json=SECURITY).status_code == 403
    assert client.post("/api/watchlist", json={"instrument_id": item["id"]}).status_code == 201
    assert client.delete(f"/api/watchlist/{item['id']}").status_code == 204
    client.post("/api/auth/logout")
    client.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE})
    viewer = client.post("/api/users", json={"email": "viewer@example.com"}).json()
    client.post("/api/auth/logout")
    client.post("/api/auth/login", json={"email": viewer["email"], "access_code": viewer["access_code"]})
    assert client.get("/api/instruments").json()["total"] == 1
    assert client.post("/api/watchlist", json={"instrument_id": item["id"]}).status_code == 403
    assert client.delete(f"/api/watchlist/{item['id']}").status_code == 403


def test_registry_other_tenant_not_accessible(admin, setup):
    item = admin.post("/api/admin/instruments", json=SECURITY).json()
    with setup[1].begin() as db:
        tenant = Tenant(name="Other", timezone="Asia/Seoul")
        db.add(tenant)
        db.flush()
        user = User(
            tenant_id=tenant.id, email="other@example.com", role="admin", code_hash=hash_code("synthetic")
        )
        db.add(user)
        db.flush()
        token = create_session(db, user, 24)
    admin.cookies.set("trading_session", token)
    assert admin.get("/api/instruments").json()["total"] == 0
    assert admin.post("/api/watchlist", json={"instrument_id": item["id"]}).status_code == 404


def test_research_requires_registered_active_security_and_validation(setup):
    config = setup[0].model_copy(update={"research_enabled": True})
    with TestClient(create_app(config), headers={"origin": ORIGIN}) as client:
        client.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE})
        assert (
            client.post("/api/runs", json={"symbol": "123456", "request_key": "unknown-code-key"}).status_code
            == 422
        )
        assert client.post("/api/admin/instruments", json={**SECURITY, "code": "ABCDEF"}).status_code == 422
        assert client.post("/api/admin/instruments", json={**SECURITY, "name": " "}).status_code == 422
        item = client.post("/api/admin/instruments", json={**SECURITY, "status": "inactive"}).json()
        assert client.post("/api/watchlist", json={"instrument_id": item["id"]}).status_code == 409
        assert (
            client.post(
                "/api/runs", json={"symbol": "123456", "request_key": "inactive-code-key"}
            ).status_code
            == 409
        )
