from nastack_trading.news_prompt import DEFAULT_PROMPT


def test_business_research_contract_is_evidence_bounded():
    for text in [
        "사업지도",
        "수요처",
        "직접 영향",
        "간접 영향",
        "외부 검색 도구",
        "조사자료 한계",
        "스마트폰",
        "점수를 강제하지 않는다",
    ]:
        assert text in DEFAULT_PROMPT


def test_news_window_is_separate_and_configurable(admin):
    assert admin.get("/api/news/today").json()["window_days"] == 14
    assert admin.put("/api/news/window", json={"days": 21}).status_code == 200
    assert admin.get("/api/news/today").json()["window_days"] == 21
    assert admin.put("/api/news/window", json={"days": 180}).status_code == 422


def test_prompt_requires_admin(admin, setup):
    from sqlalchemy import select

    from nastack_trading.models import User

    with setup[1]() as db:
        user = db.scalar(select(User))
        user.role = "viewer"
        db.commit()
    # Role changes revoke the prior administrator's unlimited session.
    from conftest import CODE, EMAIL

    assert admin.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE}).status_code == 200
    assert admin.get("/api/admin/news-prompt").status_code == 403
    assert admin.put("/api/admin/news-prompt", json={"version": 1, "text": DEFAULT_PROMPT}).status_code == 403


def test_admin_prompt_versioning_and_validation(admin):
    response = admin.get("/api/admin/news-prompt")
    assert response.status_code == 200
    before = response.json()
    assert "간접 영향" in before["text"]
    custom = DEFAULT_PROMPT + "\n관리자 검증용 지침: 국내외 공급망 전달 경로를 비교한다."
    result = admin.put("/api/admin/news-prompt", json={"version": before["version"], "text": custom})
    assert result.status_code == 200, result.text
    after = result.json()
    assert after["text"] == custom and after["version"] == before["version"] + 1
    assert after["sha256"] != before["sha256"]
    assert admin.get("/api/admin/news-prompt").json()["text"] == custom
    assert (
        admin.put("/api/admin/news-prompt", json={"version": before["version"], "text": custom}).status_code
        == 409
    )
    assert (
        admin.put("/api/admin/news-prompt", json={"version": after["version"], "text": "짧음"}).status_code
        == 422
    )
