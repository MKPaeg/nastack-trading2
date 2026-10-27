from pathlib import Path

from conftest import EMAIL, ORIGIN, enqueue
from sqlalchemy import select

from nastack_trading.catalog import catalog_changed, ensure_personal_sources, import_note
from nastack_trading.config import ROOT
from nastack_trading.models import AnalysisRun, AuditEvent, PersonalSource, SourceCatalog, Tenant, User
from nastack_trading.security import create_session, hash_code

SOURCE = {
    "name": "시험 출처",
    "domain": "example.com",
    "url": "https://example.com/",
    "region": "KR",
    "category": "공시",
    "method": "api",
    "access_notes": "키 필요",
    "status": "candidate",
}


def test_catalog_crud_versions_audit_and_snapshot(admin, setup):
    row = admin.post("/api/sources", json=SOURCE)
    assert row.status_code == 201
    source = row.json()
    assert admin.post("/api/sources", json=SOURCE).status_code == 409
    run = enqueue(admin)
    assert run["source_catalog_snapshot"][0]["version"] == 1
    update = {**SOURCE, "version": 1, "reason": "이용조건 변경", "status": "retired"}
    saved = admin.put(f"/api/sources/{source['id']}", json=update)
    assert saved.status_code == 200 and saved.json()["version"] == 2
    assert admin.put(f"/api/sources/{source['id']}", json=update).status_code == 409
    assert admin.get(f"/api/runs/{run['id']}").json()["source_catalog_snapshot"][0]["status"] == "candidate"
    assert admin.get("/api/sources?page_size=6").status_code == 422
    assert admin.get("/api/sources").json()["total"] == 1
    with setup[1]() as db:
        event = db.scalar(select(AuditEvent).where(AuditEvent.action == "source.updated"))
        assert event.detail["reason"] == "이용조건 변경"
        assert event.detail["previous"]["status"] == "candidate"


def test_catalog_permissions_and_invalid_urls(admin, client):
    for change in [
        {"url": "http://127.0.0.1/", "domain": "127.0.0.1"},
        {"url": "https://user:secret@example.com/"},
        {"url": "https://example.com/?key=secret"},
        {"url": "https://other.com/"},
    ]:
        assert admin.post("/api/sources", json={**SOURCE, **change}).status_code == 422
    invitation = admin.post("/api/users", json={"email": "viewer@example.com"}).json()
    admin.post("/api/auth/logout")
    assert (
        client.post(
            "/api/auth/login",
            json={"email": invitation["email"], "access_code": invitation["access_code"]},
            headers={"origin": ORIGIN},
        ).status_code
        == 200
    )
    assert client.get("/api/sources").status_code == 200
    assert client.post("/api/sources", json=SOURCE).status_code == 201
    assert (
        client.put("/api/sources/missing", json={**SOURCE, "version": 1, "reason": "test"}).status_code == 404
    )


def test_defaults_independent_per_user(admin, setup):
    with setup[1].begin() as db:
        owner = db.scalar(select(User).where(User.email == EMAIL))
        db.add(SourceCatalog(tenant_id=owner.tenant_id, domain=SOURCE["domain"], value=SOURCE))
    original = admin.get("/api/sources").json()["items"][0]
    assert (
        admin.put(
            f"/api/sources/{original['id']}",
            json={**SOURCE, "name": "내 수정", "version": 1, "reason": "개인 편집"},
        ).status_code
        == 200
    )
    other = admin.post("/api/users", json={"email": "sources-other@example.com"}).json()
    with setup[1].begin() as db:
        user = db.get(User, other["id"])
        token = create_session(db, user, 24)
        ensure_personal_sources(db, user)
        assert db.scalar(select(SourceCatalog)).value["name"] == SOURCE["name"]
    admin.cookies.set("trading_session", token)
    copied = admin.get("/api/sources").json()["items"][0]
    assert copied["id"] != original["id"] and copied["name"] == SOURCE["name"]
    assert (
        admin.put(
            f"/api/sources/{original['id']}", json={**SOURCE, "version": 2, "reason": "침범"}
        ).status_code
        == 404
    )
    assert (
        admin.put(
            f"/api/sources/{copied['id']}",
            json={**SOURCE, "version": 1, "reason": "개인 편집", "status": "retired"},
        ).status_code
        == 200
    )
    assert (
        admin.post(
            "/api/sources",
            json={**SOURCE, "domain": "personal.example.com", "url": "https://personal.example.com/"},
        ).status_code
        == 201
    )
    assert (
        admin.post("/api/runs", json={"symbol": "005930", "request_key": "viewer-no-analysis"}).status_code
        == 403
    )
    with setup[1]() as db:
        mine = db.get(PersonalSource, original["id"])
        assert mine.value["name"] == "내 수정" and mine.value["status"] == "candidate"


def test_empty_list_initialized_once(admin, setup):
    assert admin.get("/api/sources").json()["total"] == 0
    with setup[1].begin() as db:
        user = db.scalar(select(User).where(User.email == EMAIL))
        db.add(SourceCatalog(tenant_id=user.tenant_id, domain=SOURCE["domain"], value=SOURCE))
    assert admin.get("/api/sources").json()["total"] == 0


def test_import_note_is_candidate_idempotent_no_overwrite(setup):
    note = next((ROOT / "docs").glob("*ETF*API*.md"))
    with setup[1].begin() as db:
        admin = db.scalar(select(User).where(User.email == EMAIL))
        count = import_note(db, admin, Path(note))
        assert count > 30
        assert import_note(db, admin, Path(note)) == 0
        rows = db.scalars(select(SourceCatalog)).all()
        assert all(row.value["status"] == "candidate" for row in rows)
        rows[0].value = {**rows[0].value, "name": "관리자 편집"}
        assert import_note(db, admin, Path(note)) == 0
        assert rows[0].value["name"] == "관리자 편집"


def test_catalog_tenant_isolation(admin, setup):
    source = admin.post("/api/sources", json=SOURCE).json()
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
    assert admin.get("/api/sources").json()["total"] == 0
    assert (
        admin.put(f"/api/sources/{source['id']}", json={**SOURCE, "version": 1, "reason": "test"}).status_code
        == 404
    )


def test_retired_sources_excluded_and_relevant_change_invalidates(admin, setup):
    source = admin.post("/api/sources", json=SOURCE).json()
    preferences = admin.get("/api/settings").json()
    preferences["value"]["sources"] = [{"domain": "example.com", "disposition": "use"}]
    assert admin.put("/api/settings", json=preferences).status_code == 200
    run = enqueue(admin)
    with setup[1].begin() as db:
        record = db.get(AnalysisRun, run["id"])
        record.kind = "research"
        assert not catalog_changed(db, record)
    assert (
        admin.put(
            f"/api/sources/{source['id']}",
            json={**SOURCE, "status": "retired", "version": 1, "reason": "停止"},
        ).status_code
        == 200
    )
    with setup[1]() as db:
        assert catalog_changed(db, db.get(AnalysisRun, run["id"]))
    next_run = enqueue(admin, key="second-run-key")
    with setup[1]() as db:
        assert db.get(AnalysisRun, next_run["id"]).settings_snapshot["sources"] == [
            {"domain": "example.com", "disposition": "exclude"}
        ]
