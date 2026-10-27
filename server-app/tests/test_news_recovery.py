from copy import deepcopy

import pytest
import test_collection_pipeline as base
from fastapi import HTTPException
from sqlalchemy import select

from nastack_trading.models import CollectionWorkflow, NewsArticle, NewsRun, Tenant, User, UserSettings
from nastack_trading.news_recovery import resume_news_run

workflow_client = base.workflow_client


def failed_run(workflow_client, setup):
    client, calls, control, uid, tid, _ = workflow_client
    assert base.refresh(client)["status"] == "completed"
    with setup[1].begin() as db:
        a = db.scalar(select(NewsArticle).where(NewsArticle.owner_id == uid))
        db.add(
            NewsArticle(
                owner_id=uid,
                tenant_id=tid,
                source_id=a.source_id,
                fingerprint="another-original",
                published_at=a.published_at,
                news_date=a.news_date,
                value={**a.value, "url": "https://trendforce.com/news/another"},
            )
        )
    control["bad_news"] = True
    assert base.refresh(client)["status"] == "partial"
    with setup[1]() as db:
        run_id = db.get(CollectionWorkflow, (uid, "123456")).value["news_run_id"]
        r = db.get(NewsRun, run_id)
        assert len(r.value["evaluated_article_ids"]) == 1 and r.value["pending_articles"] == 1
    control["bad_news"] = False
    return run_id


def test_resume_pins_original_model_reuses_completed_and_preserves_preferences(workflow_client, setup):
    _, calls, control, uid, _, _ = workflow_client
    run_id = failed_run(workflow_client, setup)
    before = dict(calls)
    with setup[1]() as db:
        settings = db.get(UserSettings, uid)
        settings.value = {**settings.value, "news_model": "vllm:different-current-choice"}
        settings.version += 1
        db.commit()
        preferences = deepcopy(settings.value)
        original = deepcopy(db.get(NewsRun, run_id).value)
        result = resume_news_run(
            db, db.get(User, uid), run_id, control["config"], transport=control["transport"]
        )
        assert result.status == "completed" and result.value["pending_articles"] == 0
        assert result.value["model"] == original["model"]
        assert original["assessments"][0] in result.value["assessments"]
        assert db.get(NewsRun, run_id).value == original
        assert db.get(UserSettings, uid).value == preferences
        assert calls == {**before, "news": before["news"] + 1}


@pytest.mark.parametrize("same_tenant", [True, False])
@pytest.mark.parametrize("role", ["admin", "trader", "viewer"])
def test_resume_never_accepts_foreign_run(workflow_client, setup, same_tenant, role):
    _, calls, control, _, tid, _ = workflow_client
    run_id = failed_run(workflow_client, setup)
    before = dict(calls)
    with setup[1]() as db:
        if not same_tenant:
            tenant = Tenant(name="Other", timezone="Asia/Seoul")
            db.add(tenant)
            db.flush()
            tid = tenant.id
        user = User(
            tenant_id=tid, email="other-recovery@example.com", role=role, active=True, code_hash="unused"
        )
        db.add(user)
        db.commit()
        with pytest.raises(HTTPException) as result:
            resume_news_run(db, user, run_id, control["config"])
        assert result.value.status_code == (403 if role == "viewer" else 404)
        assert calls == before
