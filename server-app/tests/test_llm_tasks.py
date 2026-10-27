from sqlalchemy import select

from nastack_trading.llm_tasks import TASKS
from nastack_trading.models import Tenant, User, UserSettings
from nastack_trading.security import create_session


def test_task_prompts_are_independent_versioned_and_keep_existing_settings(admin, setup):
    catalog = admin.get("/api/user/llm-tasks").json()
    assert {item["id"] for item in catalog["items"]} == set(TASKS)
    original = admin.get("/api/settings").json()["value"]
    for task in TASKS:
        policy = next(item["policy"] for item in catalog["items"] if item["id"] == task)
        text = policy["text"] + f"\n작업별 사용자 지침 {task}: 제공된 근거와 불확실성을 명확하게 작성하세요."
        response = admin.put(f"/api/user/llm-tasks/{task}", json={"version": policy["version"], "text": text})
        assert response.status_code == 200, response.text
        assert response.json()["version"] == policy["version"] + 1
        assert (
            admin.put(
                f"/api/user/llm-tasks/{task}", json={"version": policy["version"], "text": text}
            ).status_code
            == 409
        )
        history = admin.get(f"/api/user/llm-tasks/{task}/history").json()["items"]
        assert history[0]["before"]["text"] == policy["text"]
        assert history[0]["after"]["text"] == text
    restored = admin.get("/api/user/llm-tasks").json()
    assert all(item["policy"]["version"] == 2 for item in restored["items"])
    assert admin.get("/api/user/news-prompt").json()["text"] == next(
        item["policy"]["text"] for item in restored["items"] if item["id"] == "news"
    )
    after = admin.get("/api/settings").json()["value"]
    for key, value in original.items():
        assert after[key] == value


def test_task_policy_and_history_are_personal_and_viewer_cannot_change(admin, setup):
    own = admin.get("/api/user/llm-tasks").json()["items"][0]["policy"]
    custom = own["text"] + "\n소유자 A의 비공개 평가 지침입니다."
    assert admin.put("/api/user/llm-tasks/business", json={"version": 1, "text": custom}).status_code == 200
    for role, same_tenant in [("trader", True), ("admin", True), ("viewer", False)]:
        with setup[1].begin() as db:
            owner = db.scalar(select(User).where(User.email == "admin@example.com"))
            tid = owner.tenant_id
            if not same_tenant:
                tenant = Tenant(name="Other", timezone="Asia/Seoul")
                db.add(tenant)
                db.flush()
                tid = tenant.id
            user = User(
                tenant_id=tid, email="other-" + role + "@example.com", role=role, active=True, code_hash="unused"
            )
            db.add(user)
            db.flush()
            db.add(UserSettings(user_id=user.id, tenant_id=tid, value={}))
            token = create_session(db, user, 24)
        admin.cookies.set("trading_session", token)
        data = admin.get("/api/user/llm-tasks")
        assert data.status_code == 200 and "소유자 A" not in data.text
        assert admin.get("/api/user/llm-tasks/business/history").json()["items"] == []
        changed = admin.put("/api/user/llm-tasks/business", json={"version": 1, "text": custom})
        assert changed.status_code == (403 if role == "viewer" else 200)


def test_prompt_persists_through_logout_and_regular_settings_save(admin, setup):
    before = admin.get("/api/settings").json()
    text = "이것은 번역 작업 전용 사용자 지침입니다. 사실과 전망을 구분하고 제공 원문을 충실히 번역하세요."
    assert admin.put("/api/user/llm-tasks/translation", json={"version": 1, "text": text}).status_code == 200
    # An old settings form is rejected, rather than overwriting concurrent prompt edits.
    assert admin.put("/api/settings", json=before).status_code == 409
    admin.post("/api/auth/logout")
    with setup[1].begin() as db:
        owner = db.scalar(select(User))
        token = create_session(db, owner, 24)
    admin.cookies.set("trading_session", token)
    restored = admin.get("/api/user/llm-tasks").json()["items"]
    assert next(item for item in restored if item["id"] == "translation")["policy"]["text"] == text
