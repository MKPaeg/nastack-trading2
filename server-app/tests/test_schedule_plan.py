from conftest import EMAIL
from sqlalchemy import select

from nastack_trading.models import AuditEvent, SchedulePolicy, User


def test_schedule_plan_is_db_backed_versioned_and_does_not_run(admin, setup):
    initial = admin.get("/api/schedule-plan")
    assert initial.status_code == 200
    assert initial.json()["execution_state"] == "not_connected"
    assert initial.json()["value"]["holdings_minutes"] == 5
    revised = dict(initial.json()["value"], holdings_minutes=10, news_minutes=30)
    updated = admin.put("/api/schedule-plan", json={"version": 1, "value": revised})
    assert updated.status_code == 200 and updated.json()["version"] == 2
    assert admin.put("/api/schedule-plan", json={"version": 1, "value": revised}).status_code == 409
    assert admin.put("/api/schedule-plan", json={"version": 2, "value": dict(revised, holdings_minutes=1)}).status_code == 422
    assert admin.put("/api/schedule-plan", json={"version": 2, "value": dict(revised, enabled=True)}).status_code == 422
    with setup[1]() as db:
        user = db.scalar(select(User).where(User.email == EMAIL))
        policy = db.get(SchedulePolicy, user.id)
        assert policy.version == 2 and policy.value["holdings_minutes"] == 10
        assert db.scalar(select(AuditEvent).where(AuditEvent.action == "schedule.plan_updated")) is not None


def test_new_user_has_separate_schedule_plan(admin, setup):
    created = admin.post("/api/users", json={"email": "schedule-user@example.com", "role": "viewer"})
    assert created.status_code == 201
    with setup[1]() as db:
        person = db.scalar(select(User).where(User.email == "schedule-user@example.com"))
        policy = db.get(SchedulePolicy, person.id)
        assert policy is not None and policy.value["holdings_minutes"] == 5
    admin.post("/api/auth/logout")
    assert admin.post("/api/auth/login", json={"email": "schedule-user@example.com", "access_code": created.json()["access_code"]}).status_code == 200
    assert admin.get("/api/schedule-plan").status_code == 200
    current = admin.get("/api/schedule-plan").json()
    assert admin.put("/api/schedule-plan", json={"version": current["version"], "value": current["value"]}).status_code == 403
