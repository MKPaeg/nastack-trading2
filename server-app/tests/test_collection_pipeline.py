import json
from copy import deepcopy
from datetime import timedelta

import httpx
import pytest
from conftest import CODE, EMAIL, ORIGIN
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from nastack_trading.app import create_app
from nastack_trading.catalog import ensure_personal_sources
from nastack_trading.db import now
from nastack_trading.models import (
    CollectionSearch,
    CollectionWorkflow,
    DataCollectionPolicy,
    Instrument,
    NewsArticle,
    PersonalSource,
    StockFactorProfile,
    Tenant,
    User,
    UserSettings,
    WatchlistItem,
)
from nastack_trading.security import create_session


@pytest.fixture
def workflow_client(setup):
    counters = {"plan": 0, "search": 0, "news": 0, "stat": 0, "fetch_stat": 0}
    control = {
        "bad_plan": False,
        "bad_news": False,
        "text": "반도체 장비 수요 증가",
        "hook": None,
        "published": (now() - timedelta(minutes=2)).isoformat(),
    }
    with setup[1].begin() as db:
        user = db.scalar(select(User))
        uid, tid = user.id, user.tenant_id
        ensure_personal_sources(db, user)
        source = PersonalSource(
            owner_id=uid,
            tenant_id=tid,
            domain="trendforce.com",
            value={"name": "Trendforce", "url": "https://trendforce.com"},
        )
        db.add(source)
        db.flush()
        source.value = {**source.value, "status": "active", "disposition": "use"}
        sid = source.id
        fred = PersonalSource(
            owner_id=uid,
            tenant_id=tid,
            domain="fred.stlouisfed.org",
            value={"name": "FRED", "url": "https://fred.stlouisfed.org"},
        )
        db.add(fred)
        fred.value = {**fred.value, "status": "active", "disposition": "use"}
        settings = db.get(UserSettings, uid)
        settings.value = {**settings.value, "analysis_model": {"provider": "vllm", "model_id": "test-model"}}
        inst = Instrument(
            tenant_id=tid,
            code="123456",
            value={"name": "시험기업", "market": "KOSPI", "asset_type": "stock", "status": "active"},
        )
        db.add(inst)
        db.flush()
        db.add(WatchlistItem(owner_id=uid, tenant_id=tid, instrument_id=inst.id))
        db.add(
            StockFactorProfile(
                owner_id=uid,
                tenant_id=tid,
                code="123456",
                value={
                    "origin": "llm_default",
                    "status": "ready",
                    "selected_dimensions": ["sector-01"],
                    "business_context": {
                        "description": "반도체 장비를 제조하는 시험기업",
                        "sources": ["https://issuer.test/about"],
                        "sha256": "business-1",
                    },
                },
            )
        )
    config = setup[0].model_copy(
        update={
            "news_enabled": True,
            "vllm_base_url": "http://model.test/v1",
            "vllm_model_id": "test-model",
            "tavily_api_key": SecretStr("mock-search"),
            "broker_encryption_key": SecretStr(Fernet.generate_key().decode()),
        }
    )
    control["config"] = config

    def handler(request):
        if request.url.host == "api.tavily.com":
            counters["search"] += 1
            body = json.loads(request.content)
            assert body["include_domains"] == ["trendforce.com"]
            assert body["include_raw_content"] is False
            if control["hook"]:
                control["hook"]()
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "url": "https://www.trendforce.com/news/test",
                            "title": "반도체 장비 산업",
                            "content": control["text"],
                            "published_date": control["published"],
                        },
                        {
                            "url": "http://127.0.0.1/private",
                            "title": "거부",
                            "content": "비허용",
                            "published_date": now().isoformat(),
                        },
                        {"url": "https://trendforce.com/unknown", "title": "날짜 미확인", "content": "내용"},
                    ]
                },
            )
        if request.url.host == "fred.stlouisfed.org":
            counters["fetch_stat"] += 1
            return httpx.Response(200, text="observation_date,INDPRO\n2026-08-01,100\n2026-09-01,101\n")
        assert request.url.host == "model.test"
        body = json.loads(request.content)
        content = json.loads(body["messages"][1]["content"])
        if "sources" in content:
            counters["plan"] += 1
            assert "반도체" in content["business"]
            result = {
                "queries": [
                    {
                        "source_id": "foreign-source" if control["bad_plan"] else sid,
                        "query": "시험기업 반도체 장비 수요",
                        "reason": "사업 수요 경로",
                    }
                ],
                "statistics": ["INDPRO"],
                "gaps": ["해당 산업 세부 통계 연결 부족"],
            }
        else:
            articles = content["articles"]
            kind = "stat" if articles[0]["id"] == "INDPRO" else "news"
            counters[kind] += 1
            result = {
                "assessments": [
                    {
                        "article_id": a["id"],
                        "dimension_id": "stock-" + content["selected_instrument"]["code"],
                        "score9": 2,
                        "reason": "제공된 사업 근거에 대한 참고 해석",
                        "evidence": a["excerpt"][:60],
                        "relevance": "industry",
                    }
                    for a in articles
                ]
            }
            if kind == "news" and control["bad_news"]:
                result["assessments"][0]["evidence"] = "원문에 없는 근거는 반드시 거부"
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(result, ensure_ascii=False)}}], **({"usage": control["usage"]} if "usage" in control else {})}
        )

    control["transport"] = httpx.MockTransport(handler)
    with TestClient(
        create_app(config, news_transport=control["transport"]), headers={"origin": ORIGIN}
    ) as client:
        assert client.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE}).status_code == 200
        yield client, counters, control, uid, tid, sid


def refresh(client, **body):
    response = client.post("/api/news/workflow/123456/refresh", json=body)
    assert response.status_code == 200, response.text
    return client.get("/api/news/workflow/123456").json()


def test_local_search_budget_is_not_reported_as_provider_rejection(workflow_client, setup):
    client, counters, _, uid, tid, _ = workflow_client
    with setup[1].begin() as db:
        db.add(DataCollectionPolicy(owner_id=uid, tenant_id=tid, value={"settings": {"search_daily_limit": 0}}))
    result = refresh(client)
    assert result["error"] == "DAILY_COLLECTION_LIMIT" and result["limit_kind"] == "search"
    assert result["retry_after_seconds"] > 0
    assert counters["search"] == 0


@pytest.mark.parametrize("usage,total", [({"prompt_tokens": 20, "completion_tokens": 5}, 25), ({"input_tokens": 30, "output_tokens": 10, "total_tokens": 40}, 40), ({}, None)])
def test_collection_job_tokens_are_actual_not_estimated(workflow_client, usage, total):
    client, calls, control, *_ = workflow_client
    control["usage"] = usage
    result = refresh(client, mode="collect")
    assert result["status"] == "collected"
    assert result["collection_usage"]["total_tokens"] == total
    assert result["collection_usage"]["known"] is (total is not None)
    assert result["execution_usage"]["llm_calls"] == 1
    assert result["execution_usage"]["known"] is (total is not None)
    assert result["execution_usage"]["total_tokens"] == (total or 0)
    assert calls["news"] == calls["stat"] == 0
    repeated = refresh(client, mode="collect")
    assert repeated["collection_usage"] == {"known": True, "total_tokens": 0, "llm_calls": 0}
    assert repeated["execution_usage"]["llm_calls"] == 0
    assert repeated["execution_usage"]["estimated_usd"] == 0
    assert calls["plan"] == 1


def test_failed_plan_keeps_reported_usage(workflow_client):
    client, _, control, *_ = workflow_client
    control.update(usage={"total_tokens": 42}, bad_plan=True)
    result = refresh(client, mode="collect")
    assert result["status"] == "failed"
    assert result["collection_usage"]["total_tokens"] == 42
    assert result["execution_usage"]["total_tokens"] == 42
    assert result["execution_usage"]["cost_known"] is False


def test_plan_collect_evaluate_and_reentry_reuses_all_calls(workflow_client, setup):
    client, calls, _, uid, _, _ = workflow_client
    result = refresh(client)
    assert result["status"] == "completed", result
    assert calls == {"plan": 1, "search": 1, "news": 1, "stat": 1, "fetch_stat": 1}
    assert result["counts"]["materials"] == 1
    assert result["statistics_assessments"]["INDPRO"]["observations"][-1]["value"] == 101
    assert refresh(client)["status"] == "completed"
    assert calls == {"plan": 1, "search": 1, "news": 1, "stat": 1, "fetch_stat": 1}
    with setup[1]() as db:
        assert len(db.scalars(select(NewsArticle).where(NewsArticle.owner_id == uid)).all()) == 1
    data = client.get("/api/news/today?code=123456").json()
    assert all(a["article_id"] != "INDPRO" for a in data["assessments"])


@pytest.mark.parametrize("same_tenant", [True, False])
def test_saved_feed_evidence_reused_without_search_or_foreign_disclosures(
    workflow_client, setup, same_tenant
):
    client, calls, _, uid, tid, sid = workflow_client
    assert refresh(client)["counts"]["materials"] == 1
    with setup[1].begin() as db:
        other_tid = tid
        if not same_tenant:
            tenant = Tenant(name="Evidence isolation", timezone="Asia/Seoul")
            db.add(tenant)
            db.flush()
            other_tid = tenant.id
        other = User(
            tenant_id=other_tid,
            email="evidence@example.test",
            role="trader",
            active=True,
            code_hash="test-only",
        )
        db.add(other)
        db.flush()
        for suffix, owner, tenant_id, age, kind in [
            ("own", uid, tid, 1, "news"),
            ("foreign", other.id, other_tid, 1, "news"),
            ("expired", uid, tid, 60, "news"),
            ("other-disclosure", uid, tid, 1, "disclosure"),
        ]:
            stamp = now() - timedelta(days=age)
            db.add(
                NewsArticle(
                    owner_id=owner,
                    tenant_id=tenant_id,
                    source_id=sid,
                    fingerprint=suffix,
                    published_at=stamp,
                    news_date=stamp.date(),
                    value={
                        "title": "별개 회사" if kind == "disclosure" else "반도체 장비 수요",
                        "excerpt": "기존 저장 자료의 산업 수요 근거",
                        "url": "https://trendforce.com/" + suffix,
                        "published_at": stamp.isoformat(),
                        "content_type": kind,
                    },
                )
            )
    result = refresh(client)
    assert result["status"] == "completed", result
    assert result["counts"]["materials"] == 2
    assert calls["plan"] == 1 and calls["search"] == 1 and calls["news"] == 2
    assert refresh(client)["status"] == "completed"
    assert calls["news"] == 2


def test_same_owner_two_symbols_share_search_but_not_impact(workflow_client, setup):
    client, calls, _, uid, tid, _ = workflow_client
    assert refresh(client)["status"] == "completed"
    with setup[1].begin() as db:
        instrument = Instrument(
            tenant_id=tid,
            code="654321",
            value={"name": "다른 반도체 기업", "market": "KOSPI", "asset_type": "stock", "status": "active"},
        )
        db.add(instrument)
        db.flush()
        db.add(WatchlistItem(owner_id=uid, tenant_id=tid, instrument_id=instrument.id))
        db.add(
            StockFactorProfile(
                owner_id=uid,
                tenant_id=tid,
                code="654321",
                value=deepcopy(db.get(StockFactorProfile, (uid, "123456")).value),
            )
        )
    response = client.post("/api/news/workflow/654321/refresh", json={})
    assert response.status_code == 200
    result = client.get("/api/news/workflow/654321").json()
    assert result["status"] == "completed", result
    assert calls["plan"] == 2 and calls["search"] == 1
    assert calls["news"] == 2 and calls["fetch_stat"] == 1
    with setup[1]() as db:
        rows = db.scalars(select(CollectionSearch)).all()
        assert len(rows) == 1 and rows[0].owner_id == uid and rows[0].tenant_id == tid
        workflow = db.get(CollectionWorkflow, (uid, "654321"))
        assert all(q["shared_search_reused"] for q in workflow.value["query_cache"].values())


def test_incomplete_shared_search_is_not_automatically_resubmitted(workflow_client, setup):
    client, calls, _, uid, *_ = workflow_client
    assert refresh(client)["status"] == "completed"
    with setup[1].begin() as db:
        row = db.get(CollectionWorkflow, (uid, "123456"))
        value = deepcopy(row.value)
        for item in value["query_cache"].values():
            item["checked_at"] = (now() - timedelta(hours=13)).isoformat()
        row.value = value
        cached = db.scalar(select(CollectionSearch))
        cached.status = "running"
    result = refresh(client)
    assert result["error"] == "SHARED_SEARCH_INCOMPLETE"
    assert calls["search"] == 1


def test_changed_content_only_reassessed_and_business_change_replans(workflow_client, setup):
    client, calls, control, uid, _, _ = workflow_client
    assert refresh(client)["status"] == "completed"
    with setup[1].begin() as db:
        row = db.get(CollectionWorkflow, (uid, "123456"))
        value = deepcopy(row.value)
        for item in value["query_cache"].values():
            item["checked_at"] = (now() - timedelta(hours=13)).isoformat()
        row.value = value
        for cached in db.scalars(select(CollectionSearch)):
            cached.updated_at = now() - timedelta(hours=13)
    control["text"] = "반도체 장비 수요 감소 · 수정된 원문"
    assert refresh(client)["status"] == "completed"
    assert calls == {"plan": 1, "search": 2, "news": 2, "stat": 1, "fetch_stat": 1}
    with setup[1].begin() as db:
        row = db.get(StockFactorProfile, (uid, "123456"))
        row.value = {
            **row.value,
            "business_context": {
                "description": "반도체 검사장비를 개발·제조하는 시험기업",
                "sha256": "business-2",
            },
        }
    result = refresh(client)
    assert result["status"] == "completed", result
    assert calls["plan"] == 2 and calls["search"] == 2 and calls["stat"] == 2


def test_expired_search_with_unchanged_text_does_not_call_evaluator(workflow_client, setup):
    client, calls, control, uid, *_ = workflow_client
    assert refresh(client)["status"] == "completed"
    with setup[1].begin() as db:
        article = db.scalar(select(NewsArticle).where(NewsArticle.owner_id == uid))
        article.value = {**article.value, "summary_ko": "표시용 번역 요약"}
    assert refresh(client)["status"] == "completed"
    assert calls["news"] == 1 and calls["search"] == 1
    with setup[1].begin() as db:
        row = db.get(CollectionWorkflow, (uid, "123456"))
        value = deepcopy(row.value)
        for query in value["query_cache"].values():
            query["checked_at"] = (now() - timedelta(hours=13)).isoformat()
        row.value = value
        for cached in db.scalars(select(CollectionSearch)):
            cached.updated_at = now() - timedelta(hours=13)
    assert refresh(client)["status"] == "completed"
    assert calls == {"plan": 1, "search": 2, "news": 1, "stat": 1, "fetch_stat": 1}
    with setup[1]() as db:
        assert (
            db.scalar(select(NewsArticle).where(NewsArticle.owner_id == uid)).value["summary_ko"]
            == "표시용 번역 요약"
        )


def test_prompt_changes_invalidate_only_the_matching_stage(workflow_client):
    client, calls, *_ = workflow_client
    assert refresh(client)["status"] == "completed"

    def change(task):
        catalog = client.get("/api/user/llm-tasks").json()
        policy = next(t["policy"] for t in catalog["items"] if t["id"] == task)
        assert (
            client.put(
                f"/api/user/llm-tasks/{task}",
                json={
                    "version": policy["version"],
                    "text": policy["text"] + "\n검토한 자료의 한계를 명확히 쓰세요.",
                },
            ).status_code
            == 200
        )

    change("collection_plan")
    assert refresh(client)["status"] == "completed"
    assert calls == {"plan": 2, "search": 1, "news": 1, "stat": 1, "fetch_stat": 1}
    change("statistics_evaluation")
    assert refresh(client)["status"] == "completed"
    assert calls == {"plan": 2, "search": 1, "news": 1, "stat": 2, "fetch_stat": 1}
    change("news")
    assert refresh(client)["status"] == "completed"
    assert calls == {"plan": 2, "search": 1, "news": 2, "stat": 2, "fetch_stat": 1}


def test_durable_running_record_requires_explicit_recovery(workflow_client, setup):
    client, calls, _, uid, *_ = workflow_client
    assert refresh(client)["status"] == "completed"
    before = dict(calls)
    with setup[1].begin() as db:
        row = db.get(CollectionWorkflow, (uid, "123456"))
        row.status, row.updated_at = "running", now() - timedelta(minutes=6)
    assert refresh(client)["status"] == "running"
    assert calls == before
    assert client.post("/api/news/workflow/123456/abandon").json()["status"] == "failed"
    assert refresh(client)["status"] == "failed"
    assert calls == before
    assert refresh(client, retry_failed=True)["status"] == "completed"
    assert calls == before


def test_disabled_personal_openai_is_not_replaced_by_global_key(workflow_client, setup):
    from fastapi import HTTPException

    from nastack_trading.collection_pipeline import inputs

    client, calls, control, uid, *_ = workflow_client
    assert (
        client.put(
            "/api/user/connections/openai", json={"version": 0, "enabled": False, "fields": {}}
        ).status_code
        == 200
    )
    with setup[1].begin() as db:
        settings = db.get(UserSettings, uid)
        settings.value = {
            **settings.value,
            "cloud_enabled": True,
            "cloud_model": "gpt-5.6-luna",
            "analysis_model": {"provider": "openai", "model_id": "gpt-5.6-luna"},
        }
    config = control["config"].model_copy(
        update={
            "news_enabled": True,
            "cloud_model_allowlist": ["gpt-5.6-luna"],
            "openai_api_key": SecretStr("global-must-not-be-used"),
        }
    )
    with setup[1]() as db, pytest.raises(HTTPException) as error:
        inputs(db, db.get(User, uid), "123456", config, True)
    assert error.value.status_code == 409 and "복호화" not in error.value.detail
    assert not any(calls.values())


def test_invalid_plan_is_not_executed_and_failed_jobs_not_automatically_retried(workflow_client):
    client, calls, control, *_ = workflow_client
    control["bad_plan"] = True
    assert refresh(client)["error"] == "INVALID_COLLECTION_PLAN"
    assert refresh(client)["status"] == "failed"
    assert calls["plan"] == 1 and calls["search"] == 0
    control["bad_plan"] = False
    assert refresh(client, retry_failed=True)["status"] == "completed"


def test_failed_assessment_preserves_actual_run_link_counts_and_pending(workflow_client, setup):
    from nastack_trading.models import NewsRun

    client, calls, control, uid, *_ = workflow_client
    control["bad_news"] = True
    result = refresh(client)
    assert result["status"] == "partial"
    assert result["error"] == "MODEL_INVALID_EVIDENCE_OR_SCORE"
    assert result["counts"]["materials"] == 1 and result["pending"] == 1
    with setup[1]() as db:
        run = db.get(NewsRun, result["news_run_id"])
        assert run.owner_id == uid and run.value["error"] == result["error"]
    refresh(client)
    assert calls["news"] == 1
    assert calls["stat"] > 0  # Independent statistical interpretation still runs.


def test_inflight_permission_change_discards_new_material(workflow_client, setup):
    client, calls, control, uid, *_ = workflow_client

    def revoke():
        with setup[1].begin() as db:
            db.get(User, uid).role = "viewer"

    control["hook"] = revoke
    assert client.post("/api/news/workflow/123456/refresh", json={}).status_code == 200
    assert calls["news"] == 0
    with setup[1]() as db:
        assert db.get(CollectionWorkflow, (uid, "123456")).status == "failed"
        assert not db.scalars(select(NewsArticle)).all()


@pytest.mark.parametrize("same_tenant", [True, False])
@pytest.mark.parametrize("role", ["admin", "trader", "viewer"])
def test_foreign_objects_and_workflows_are_never_visible(workflow_client, setup, same_tenant, role):
    client, calls, _, uid, tid, _ = workflow_client
    assert refresh(client)["status"] == "completed"
    before = dict(calls)
    with setup[1].begin() as db:
        if not same_tenant:
            tenant = Tenant(name="Other", timezone="Asia/Seoul")
            db.add(tenant)
            db.flush()
            tid = tenant.id
        user = User(
            tenant_id=tid, email="other-collection@example.com", role=role, active=True, code_hash="unused"
        )
        db.add(user)
        db.flush()
        db.add(UserSettings(user_id=user.id, tenant_id=tid, value={}))
        token = create_session(db, user, 24)
    client.cookies.set("trading_session", token)
    assert client.get("/api/news/workflow/123456").status_code == 404
    assert client.post("/api/news/workflow/123456/refresh", json={}).status_code in {403, 404}
    assert client.post("/api/news/workflow/123456/abandon").status_code == (403 if role == "viewer" else 404)
    assert calls == before
