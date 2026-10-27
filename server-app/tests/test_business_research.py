import json
from datetime import date, timedelta

import httpx
import pytest
from conftest import CODE, EMAIL, ORIGIN
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from nastack_trading.app import create_app
from nastack_trading.factor_profiles import profile_view
from nastack_trading.models import (
    BusinessResearch,
    Instrument,
    StockFactorProfile,
    Tenant,
    User,
    UserSettings,
    WatchlistItem,
)
from nastack_trading.security import create_session


@pytest.mark.parametrize("kind,reason", [("empty", "EMPTY_OUTPUT"), ("json", "INVALID_JSON"),
    ("schema", "INVALID_SCHEMA"), ("id", "UNKNOWN_EVIDENCE_ID"), ("quote", "QUOTE_MISMATCH")])
def test_business_validation_diagnostics_do_not_accept_unsupported_claims(kind, reason):
    from nastack_trading.business_research import BusinessValidationError, validate_report

    report = {"claims": [{"text": "사업", "quote": "제조한다", "evidence_id": "E1"}], "topics": [], "uncertainties": []}
    if kind == "id":
        report["claims"][0]["evidence_id"] = "E9"
    if kind == "quote":
        report["claims"][0]["quote"] = "허구의 인용"
    text = {"empty": "", "json": "{", "schema": "{}"}.get(kind, json.dumps(report))
    with pytest.raises(BusinessValidationError) as error:
        validate_report(text, [{"id": "E1", "title": "회사", "snippet": "부품을 제조한다"}])
    assert error.value.reason == reason


def test_fenced_valid_json_is_unwrapped_but_quotes_still_checked():
    from nastack_trading.business_research import validate_report

    report = {"claims": [{"text": "사업", "quote": "제조한다", "evidence_id": "E1"}], "topics": [], "uncertainties": []}
    assert validate_report("```json\n" + json.dumps(report) + "\n```", [
        {"id": "E1", "title": "회사", "snippet": "부품을 제조한다"}]).claims[0].evidence_id == "E1"


def test_model_schema_enforces_the_same_string_bounds_as_local_validation():
    import re

    from nastack_trading.business_research import BusinessValidationError, report_schema, validate_report

    schema = report_schema()["$defs"]["Claim"]["properties"]
    for field, maximum in [("text", 800), ("quote", 200), ("evidence_id", 10)]:
        pattern = schema[field]["pattern"]
        assert re.fullmatch(pattern, "가\n" + "a" * (maximum - 2))
        assert not re.fullmatch(pattern, "a" * (maximum + 1))
        assert not re.fullmatch(pattern, "")
    report = {"claims": [{"text": "사업", "quote": "a" * 201, "evidence_id": "E1"}], "topics": [], "uncertainties": []}
    with pytest.raises(BusinessValidationError) as error:
        validate_report(json.dumps(report), [])
    assert error.value.issues == [{"field": "claims.0.quote", "type": "string_too_long", "max_length": 200, "actual_length": 201}]


def test_validation_diagnostics_do_not_save_model_input_or_arbitrary_field_names():
    from nastack_trading.business_research import BusinessValidationError, validate_report

    with pytest.raises(BusinessValidationError) as error:
        validate_report(json.dumps({"secret-key-in-field": "secret-value"}), [])
    assert "secret" not in json.dumps(error.value.issues)
    assert any(issue["field"] == "unknown_field" for issue in error.value.issues)


@pytest.fixture
def business_client(setup):
    calls = []
    response = {"quote": "반도체 장비를 제조한다.", "hook": None}
    with setup[1].begin() as db:
        user = db.scalar(select(User))
        uid, tid = user.id, user.tenant_id
        settings = db.get(UserSettings, uid)
        settings.value = {**settings.value, "analysis_model": {"provider": "vllm", "model_id": "test-model"}}
        instrument = Instrument(
            tenant_id=tid,
            code="123456",
            value={"name": "시험기업", "status": "active", "market": "KOSPI", "asset_type": "stock"},
        )
        db.add(instrument)
        db.flush()
        db.add(WatchlistItem(owner_id=uid, tenant_id=tid, instrument_id=instrument.id))
        db.add(
            StockFactorProfile(
                owner_id=uid,
                tenant_id=tid,
                code="123456",
                value={
                    "origin": "manual",
                    "factors": [],
                    "selected_dimensions": ["sector-01"],
                    "data_cutoff": "2026-10-20",
                },
            )
        )
    config = setup[0].model_copy(
        update={
            "vllm_base_url": "http://model.test/v1",
            "vllm_model_id": "test-model",
            "tavily_api_key": SecretStr("search-key"),
        }
    )

    def handler(request):
        if request.url.host == "kind.krx.co.kr":
            return httpx.Response(503)
        if request.method == "GET" and request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": []})
        calls.append(request)
        if request.url.host == "api.tavily.com":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "title": "시험기업 사업보고서",
                            "url": "https://kind.krx.co.kr/report",
                            "content": "시험기업 123456은 반도체 장비를 제조한다.",
                        },
                        {
                            "title": "다른기업",
                            "url": "https://kind.krx.co.kr/other",
                            "content": "다른기업 사업",
                        },
                        {"title": "시험기업", "url": "https://evil.test/secret", "content": "시험기업 사업"},
                    ]
                },
            )
        assert request.url.host == "model.test"
        if response.get("http_status"):
            return httpx.Response(response["http_status"], json={"error": {
                "code": "server_error", "message": "never-print-secret-key-or-input"}})
        if response["hook"]:
            response["hook"]()
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "claims": [
                                        {
                                            "text": "반도체 장비 제조 사업",
                                            "evidence_id": "E1",
                                            "quote": response["quote"],
                                        }
                                    ],
                                    "topics": response.get("topics", ["반도체 장비·소재"]),
                                    "uncertainties": ["매출 비중 미확인"],
                                }
                            )
                        }
                    }
                ]
            },
        )

    with TestClient(
        create_app(config, news_transport=httpx.MockTransport(handler)), headers={"origin": ORIGIN}
    ) as client:
        assert client.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE}).status_code == 200
        yield client, calls, response, uid, tid


BODY = {"code": "123456", "request_key": "business-request-001", "confirmed": True}


@pytest.mark.parametrize("status,code", [(400, "invalid_json_schema"), (401, "invalid_api_key"),
                                       (429, "insufficient_quota"), (503, "server_error"), (400, "private-secret")])
def test_provider_http_diagnostics_are_bounded_and_safe(status, code):
    from nastack_trading.config import Config
    from nastack_trading.research import ResearchError, ResearchPipeline

    transport = httpx.MockTransport(lambda _: httpx.Response(status, json={
        "error": {"code": code, "message": "private-secret original-input"}}))
    with pytest.raises(ResearchError) as caught:
        ResearchPipeline(Config(_env_file=None), transport).post("https://api.openai.com/v1/responses", "private-key", {}, 1)
    error = caught.value
    assert error.code == "PROVIDER_HTTP_ERROR" and error.provider == "openai" and error.http_status == status
    assert error.provider_code == (None if code == "private-secret" else code)
    assert "private-" not in str(error)


@pytest.mark.parametrize("legacy", [False, True])
def test_explicit_http_failure_retry_reuses_search_without_leaking_provider_body(business_client, setup, legacy):
    from nastack_trading.db import now

    client, calls, response, _, _ = business_client
    response["http_status"] = 503
    result = client.post("/api/business-research", json=BODY).json()
    assert result["error"] == "PROVIDER_HTTP_ERROR" and result["retryable"]
    assert result["provider_error"] == {"provider": "local_model", "http_status": 503, "code": "server_error"}
    assert "HTTP 503" in result["failure_message"]
    assert "never-print" not in json.dumps(result)
    assert result["usage"] is None and len(calls) == 2
    with setup[1].begin() as db:
        row = db.get(BusinessResearch, result["id"])
        assert len(row.value["evidence"]) > 0 and "never-print" not in json.dumps(row.value)
        row.created_at = now() - timedelta(minutes=2)
        if legacy:
            row.value = {k: v for k, v in row.value.items() if k != "provider_error"}
    response["http_status"] = None
    retried = client.post("/api/business-research", json={**BODY, "request_key": "explicit-http-retry", "retry_of": result["id"]})
    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == "completed" and retried.json()["search_reused"]
    assert len(calls) == 3


def test_missing_business_topics_is_insufficient_not_completed(business_client):
    client, _, response, _, _ = business_client
    response["topics"] = []
    result = client.post("/api/business-research", json=BODY).json()
    assert result["status"] == "insufficient" and result["error"] == "BUSINESS_CONTENT_REQUIRED"
    assert result["context"] is None
    assert client.get("/api/business-research?code=123456").json()["context"] is None


def test_explicit_validation_retry_reuses_owned_search_and_preserves_history(business_client, setup):
    from nastack_trading.db import now

    client, calls, response, uid, tid = business_client
    response["quote"] = "허구의 인용"
    first = client.post("/api/business-research", json=BODY).json()
    assert first["status"] == "failed" and first["failure_reason"] == "QUOTE_MISMATCH"
    assert len(calls) == 2
    blocked = client.post("/api/business-research", json={**BODY, "request_key": "blocked-immediate-retry", "retry_of": first["id"]})
    assert blocked.status_code == 409 and int(blocked.headers["Retry-After"]) > 0
    assert "중복 호출 방지 대기" in blocked.json()["detail"] and len(calls) == 2
    with setup[1].begin() as db:
        row = db.get(BusinessResearch, first["id"])
        assert row.value["evidence"] and "search-key" not in json.dumps(row.value)
        row.created_at = now() - timedelta(minutes=2)
    response["quote"] = "반도체 장비를 제조한다."
    retry = {**BODY, "request_key": "business-request-retry", "retry_of": first["id"]}
    result = client.post("/api/business-research", json=retry)
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "completed" and result.json()["search_reused"]
    assert len(calls) == 3  # Only the failed model step, not another search.
    assert client.post("/api/business-research", json=retry).json()["status"] == "completed"
    assert len(calls) == 3
    with setup[1]() as db:
        assert db.get(BusinessResearch, first["id"]).status == "failed"
        assert db.get(StockFactorProfile, (uid, "123456")).value["origin"] == "manual"


@pytest.mark.parametrize("same_tenant", [True, False])
def test_foreign_retry_id_is_not_accessible_even_for_owned_instrument(business_client, setup, same_tenant):
    client, calls, response, uid, tid = business_client
    response["quote"] = "허구의 인용"
    original = client.post("/api/business-research", json=BODY).json()
    with setup[1].begin() as db:
        if not same_tenant:
            tenant = Tenant(name="retry-isolation", timezone="Asia/Seoul")
            db.add(tenant)
            db.flush()
            tid = tenant.id
        other = User(tenant_id=tid, email="retry-other@example.com", role="trader", active=True, code_hash="unused")
        db.add(other)
        db.flush()
        db.add(UserSettings(user_id=other.id, tenant_id=tid, value={"analysis_model": {"provider": "vllm", "model_id": "test-model"}}))
        instrument = db.scalar(select(Instrument).where(Instrument.tenant_id == tid, Instrument.code == "123456"))
        if not instrument:
            instrument = Instrument(tenant_id=tid, code="123456", value={"name": "시험기업", "status": "active"})
            db.add(instrument)
            db.flush()
        db.add(WatchlistItem(owner_id=other.id, tenant_id=tid, instrument_id=instrument.id))
        token = create_session(db, other, 24)
    client.cookies.set("trading_session", token)
    assert client.post("/api/business-research", json={**BODY, "retry_of": original["id"]}).status_code == 404
    assert len(calls) == 2


@pytest.mark.parametrize("status,expected", [("completed", None), ("incomplete", "OUTPUT_TRUNCATED"), ("refusal", "MODEL_REFUSAL")])
def test_openai_structured_contract_and_usage_without_paid_retry(setup, status, expected):
    from nastack_trading.business_research import BusinessValidationError, search_and_summarize

    cfg = setup[0].model_copy(update={"openai_api_key": SecretStr("private-model-key"), "tavily_api_key": SecretStr("private-search-key")})
    progress, calls = [], []

    def handler(request):
        if request.url.host == "kind.krx.co.kr":
            return httpx.Response(503)
        calls.append(request)
        if request.url.host == "api.tavily.com":
            return httpx.Response(200, json={"results": [{"url": "https://kind.krx.co.kr/report", "title": "시험기업",
                "content": "시험기업은 부품을 제조한다."}]})
        payload = json.loads(request.content)
        assert payload["store"] is False and payload["text"]["format"]["strict"] is True
        assert payload["text"]["format"]["schema"]["additionalProperties"] is False
        report = {"claims": [{"text": "부품 제조", "quote": "부품을 제조한다.", "evidence_id": "E1"}], "topics": [], "uncertainties": []}
        content = [{"type": "refusal", "refusal": "refused"}] if status == "refusal" else [{"type": "output_text", "text": json.dumps(report)}]
        return httpx.Response(200, json={"status": "incomplete" if status == "incomplete" else "completed", "output": [{"content": content}],
            "usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150, "secret": "not copied"}})

    args = ({"code": "123456", "name": "시험기업"}, cfg, {"provider": "openai", "model": "user-selected-model"},
            {"text": "근거 요약", "version": 1, "sha256": "test"}, set(), httpx.MockTransport(handler))
    if expected:
        with pytest.raises(BusinessValidationError) as error:
            search_and_summarize(*args, progress=progress.append)
        assert error.value.reason == expected
    else:
        assert search_and_summarize(*args, progress=progress.append)["claims"]
    assert len(calls) == 2
    assert any(item.get("evidence") for item in progress)
    assert progress[-1]["usage"] == {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150}


@pytest.mark.parametrize("outcome", ["success", "failed", "revoked", "crash"])
def test_explicit_reset_preserves_backup_and_consent_and_does_not_retry(
    business_client, setup, monkeypatch, outcome
):
    from nastack_trading import profile_reset
    from nastack_trading.models import AuditEvent, TradePlan

    _, _, _, uid, tid = business_client
    with setup[1].begin() as db:
        original = dict(db.get(StockFactorProfile, (uid, "123456")).value)
        policy = dict(db.get(UserSettings, uid).value)
        # Trading-only codes are not part of this user's requested watch/holdings reset.
        db.add(
            TradePlan(owner_id=uid, tenant_id=tid, broker="namuh", code="999999", value={"name": "거래전용"})
        )
    stages = []

    def research(payload, user, db, config, transport, **kwargs):
        stages.append("business")
        assert payload.code == "123456" and payload.cloud_confirmed and kwargs["per_instrument_cooldown"]
        assert db.get(StockFactorProfile, (uid, "123456")).value == original
        if outcome == "revoked":
            user.active = False
            db.commit()
        return {
            "status": "failed" if outcome == "failed" else "completed",
            "error": "BUSINESS_EVIDENCE_REQUIRED",
        }

    def generate(factory, config, owner_id, codes, transport, **kwargs):
        stages.append("dimensions")
        assert codes == ["123456"] and kwargs["cloud_confirmed"]
        if outcome == "crash":
            raise RuntimeError("interrupted")
        with factory.begin() as db:
            row = db.get(StockFactorProfile, (owner_id, "123456"))
            assert row.value["status"] == "pending"
            row.value = {**row.value, "status": "ready", "selected_dimensions": ["sector-02"]}

    monkeypatch.setattr(profile_reset, "run_research", research)
    monkeypatch.setattr(profile_reset, "generate", generate)
    if outcome == "crash":
        with pytest.raises(RuntimeError):
            profile_reset.reset_owned_profiles(setup[1], setup[0], uid, tid, "batch-test", confirmed=True)
    else:
        result = profile_reset.reset_owned_profiles(
            setup[1], setup[0], uid, tid, "batch-test", confirmed=True
        )
        assert result["total"] == 1
    with setup[1]() as db:
        assert db.get(UserSettings, uid).value == policy  # Never enables future paid jobs.
        backups = db.scalars(select(AuditEvent).where(AuditEvent.action == "vector.reset_backup")).all()
        if outcome in {"success", "crash"}:
            assert len(backups) == 1 and backups[0].detail["before"] == original
        else:
            assert not backups and db.get(StockFactorProfile, (uid, "123456")).value == original
        assert db.get(StockFactorProfile, (uid, "999999")) is None
    if outcome != "revoked":
        stages.clear()
        result = profile_reset.reset_owned_profiles(
            setup[1], setup[0], uid, tid, "batch-test", confirmed=True
        )
        assert result["results"][0]["status"] == "already_attempted" and not stages


def test_reset_rejects_missing_consent_foreign_tenant_and_viewer(business_client, setup):
    from nastack_trading.profile_reset import reset_owned_profiles

    _, calls, _, uid, tid = business_client
    with pytest.raises(ValueError):
        reset_owned_profiles(setup[1], setup[0], uid, tid, "batch")
    with pytest.raises(PermissionError):
        reset_owned_profiles(setup[1], setup[0], uid, "foreign", "batch", confirmed=True)
    with pytest.raises(PermissionError):
        reset_owned_profiles(setup[1], setup[0], uid, tid, "batch", confirmed=True, codes=["999999"])
    with setup[1].begin() as db:
        db.get(User, uid).role = "viewer"
    with pytest.raises(PermissionError):
        reset_owned_profiles(setup[1], setup[0], uid, tid, "batch", confirmed=True)
    assert not calls


def test_etf_uses_official_manager_evidence_and_normalizes_product_name(setup):
    from nastack_trading.business_research import search_and_summarize

    config = setup[0].model_copy(
        update={"tavily_api_key": SecretStr("test"), "vllm_base_url": "http://model.test/v1"}
    )
    calls = []

    def handler(request):
        if request.url.host == "kind.krx.co.kr":
            return httpx.Response(503)
        calls.append(request)
        if request.url.host == "api.tavily.com":
            assert "samsungfund.com" in json.loads(request.content)["include_domains"]
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "title": "Kodex 반도체 레버리지",
                            "content": "494310은 KRX 반도체 지수의 일간 수익률 2배를 추종한다.",
                            "url": "https://www.samsungfund.com/factsheet",
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "claims": [
                                        {
                                            "text": "반도체 지수 레버리지 ETF",
                                            "evidence_id": "E1",
                                            "quote": "일간 수익률 2배",
                                        }
                                    ],
                                    "topics": ["반도체"],
                                    "uncertainties": [],
                                }
                            )
                        }
                    }
                ]
            },
        )

    context = search_and_summarize(
        {"code": "494310", "name": "KODEX 반도체레버리지"},
        config,
        {"provider": "vllm", "model": "test"},
        {"text": "근거 기반 요약", "version": 1, "sha256": "test"},
        set(),
        httpx.MockTransport(handler),
    )
    assert len(calls) == 2 and context["claims"][0]["quote"] == "일간 수익률 2배"


@pytest.mark.parametrize(
    "outcome", ["success", "no_evidence", "policy_changed", "viewer", "no_consent", "manual"]
)
def test_business_precedes_initial_dimensions_and_never_retries_paid_work(business_client, setup, outcome):
    from nastack_trading.default_attributes import generate

    _, _, _, uid, tid = business_client
    config = setup[0].model_copy(
        update={
            "vllm_base_url": "http://model.test/v1",
            "vllm_model_id": "test-model",
            "tavily_api_key": SecretStr("search-key"),
        }
    )
    with setup[1].begin() as db:
        user = db.get(User, uid)
        if outcome == "viewer":
            user.role = "viewer"
        settings = db.get(UserSettings, uid)
        settings.value = {**settings.value, "default_attributes_business_enabled": outcome != "no_consent"}
        profile = db.get(StockFactorProfile, (uid, "123456"))
        if outcome != "manual":
            profile.value = {
                "origin": "llm_default",
                "status": "pending",
                "instrument": {
                    "code": "123456",
                    "name": "시험기업",
                    "market": "KOSPI",
                    "asset_type": "stock",
                },
            }
    stages = []
    first_pass = True

    def handler(request):
        if request.url.host == "kind.krx.co.kr":
            return httpx.Response(503)
        body = json.loads(request.content)
        if request.url.host == "api.tavily.com":
            stages.append("search")
            if outcome == "policy_changed":
                with setup[1].begin() as db:
                    settings = db.get(UserSettings, uid)
                    settings.value = {**settings.value, "default_attributes_business_enabled": False}
                    settings.version += 1
            return httpx.Response(
                200,
                json={
                    "results": []
                    if outcome == "no_evidence"
                    else [
                        {
                            "title": "시험기업 공시",
                            "url": "https://kind.krx.co.kr/report",
                            "content": "시험기업은 반도체 장비를 제조한다.",
                        }
                    ]
                },
            )
        content = json.loads(body["messages"][1]["content"])
        if "instruments" in content:
            stages.append("dimensions")
            if first_pass:
                assert stages[:2] == ["search", "business"]
            context = content["instruments"][0]["business_context"]
            assert context["source_kind"] == "llm_web_evidence"
            assert "반도체 장비" in context["description"]
            result = {
                "items": [{"code": "123456", "dimension_ids": ["sector-01"], "reason": "사업 근거 제안"}]
            }
        else:
            stages.append("business")
            result = {
                "claims": [
                    {"text": "반도체 장비 제조", "evidence_id": "E1", "quote": "반도체 장비를 제조한다."}
                ],
                "topics": ["반도체"],
                "uncertainties": [],
            }
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(result)}}]})

    transport = httpx.MockTransport(handler)
    generate(setup[1], config, uid, ["123456"], transport)
    if outcome == "success":
        assert stages == ["search", "business", "dimensions"]
    elif outcome in {"no_evidence", "policy_changed"}:
        assert stages == ["search"]
    else:
        assert not stages
    with setup[1].begin() as db:
        profile = db.get(StockFactorProfile, (uid, "123456"))
        if outcome == "manual":
            assert profile.value["origin"] == "manual"
            return
        if outcome == "success":
            assert profile.value["status"] == "ready"
        if outcome == "no_evidence":
            assert profile.value["status"] == "needs_review"
        profile.value = {**profile.value, "status": "pending"}
    stages.clear()
    first_pass = False
    generate(setup[1], config, uid, ["123456"], transport)
    assert "search" not in stages  # Successful evidence reused; unknown/failed calls never auto-repeated.


def test_web_research_is_cited_persisted_idempotent_and_used_by_news(business_client, setup):
    client, calls, _, uid, tid = business_client
    assert client.post("/api/business-research", json={**BODY, "confirmed": False}).status_code == 422
    assert not calls
    prompt = "사업 설명 사용자 전용 지침: 제품과 최종 수요처에 집중하고 근거가 없으면 미확인으로 남겨주세요."
    assert client.put("/api/user/llm-tasks/business", json={"version": 1, "text": prompt}).status_code == 200
    result = client.post("/api/business-research", json=BODY)
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "completed", result.text
    context = result.json()["context"]
    assert context["sources"] == ["https://kind.krx.co.kr/report"]
    assert context["prompt"]["version"] == 2 and "E1" in context["description"]
    assert prompt in json.loads(calls[1].content)["messages"][0]["content"]
    assert "search-key" not in result.text and "evil.test" not in result.text
    assert len(calls) == 2
    assert client.post("/api/business-research", json=BODY).json()["id"] == result.json()["id"]
    assert len(calls) == 2
    assert (
        client.post("/api/business-research", json={**BODY, "request_key": "different-request"}).status_code
        == 409
    )
    with setup[1]() as db:
        user = db.get(User, uid)
        profile = profile_view(db, user, "123456", date(2026, 10, 24))
        assert profile["business_context"]["description"] == context["description"]
        assert profile["selected_dimensions"] == ["sector-01"]
        from nastack_trading.news import stock_context

        news = stock_context(db, user, "123456")
        assert news["business_context"] == context["description"]
        assert "LLM" in news["context_source"]
    assert client.get("/api/business-research?code=123456").json()["context"]["sha256"] == context["sha256"]


@pytest.mark.parametrize("problem", ["bad_quote", "permission", "prompt"])
def test_invalid_evidence_or_changed_authority_never_becomes_business_attribute(
    business_client, setup, problem
):
    client, calls, response, uid, _ = business_client
    if problem == "bad_quote":
        response["quote"] = "근거에 없는 매출과 계약"
    else:

        def change():
            with setup[1].begin() as db:
                if problem == "permission":
                    db.get(User, uid).role = "viewer"
                else:
                    settings = db.get(UserSettings, uid)
                    settings.version += 1

        response["hook"] = change
    result = client.post("/api/business-research", json=BODY)
    assert result.status_code == 200
    assert result.json()["status"] == "failed" and result.json()["context"] is None
    with setup[1]() as db:
        user = db.get(User, uid)
        assert profile_view(db, user, "123456", date(2026, 10, 24))["business_context"] is None


@pytest.mark.parametrize("same_tenant", [True, False])
@pytest.mark.parametrize("role", ["admin", "trader", "viewer"])
def test_business_research_history_is_personal_and_foreign_codes_cannot_run(
    business_client, setup, same_tenant, role
):
    client, calls, _, uid, tid = business_client
    assert client.post("/api/business-research", json=BODY).json()["status"] == "completed"
    with setup[1].begin() as db:
        if not same_tenant:
            tenant = Tenant(name="Other", timezone="Asia/Seoul")
            db.add(tenant)
            db.flush()
            tid = tenant.id
        user = User(tenant_id=tid, email="other@example.com", role=role, active=True, code_hash="unused")
        db.add(user)
        db.flush()
        db.add(UserSettings(user_id=user.id, tenant_id=tid, value={}))
        token = create_session(db, user, 24)
    client.cookies.set("trading_session", token)
    assert client.get("/api/business-research?code=123456").json() == {"items": [], "context": None}
    denied = client.post("/api/business-research", json=BODY)
    assert denied.status_code == (403 if role == "viewer" else 404)
    assert len(calls) == 2
    with setup[1]() as db:
        assert len(db.scalars(select(BusinessResearch)).all()) == 1


@pytest.mark.parametrize("same_tenant", [True, False])
@pytest.mark.parametrize("role", ["admin", "trader", "viewer"])
def test_automatic_business_policy_is_personal_versioned_and_persistent(
    business_client, setup, same_tenant, role
):
    client, calls, _, uid, tid = business_client
    initial = client.get("/api/user/vector-defaults").json()
    body = {
        "version": initial["version"],
        "enabled": True,
        "allow_cloud": False,
        "model": initial["model"]["id"],
        "research_business": True,
    }
    assert client.put("/api/user/vector-defaults", json=body).status_code == 200
    assert client.put("/api/user/vector-defaults", json=body).status_code == 409
    assert client.get("/api/user/vector-defaults").json()["research_business"] is True
    with setup[1].begin() as db:
        if not same_tenant:
            tenant = Tenant(name="Policy other", timezone="Asia/Seoul")
            db.add(tenant)
            db.flush()
            tid = tenant.id
        user = User(
            tenant_id=tid, email="policy-other@example.com", role=role, active=True, code_hash="unused"
        )
        db.add(user)
        db.flush()
        db.add(
            UserSettings(
                user_id=user.id,
                tenant_id=tid,
                value={"analysis_model": {"provider": "vllm", "model_id": "test-model"}},
            )
        )
        token = create_session(db, user, 24)
    client.cookies.set("trading_session", token)
    other = client.get("/api/user/vector-defaults")
    if role == "viewer":
        assert other.status_code == 403
        assert client.put("/api/user/vector-defaults", json=body).status_code == 403
    else:
        assert other.json()["research_business"] is False
        body.update(
            version=other.json()["version"], model=other.json()["model"]["id"], research_business=False
        )
        assert client.put("/api/user/vector-defaults", json=body).status_code == 200
    with setup[1]() as db:
        assert db.get(UserSettings, uid).value["default_attributes_business_enabled"] is True
    assert not calls  # Saving consent is not a search or inference.


def test_failed_refresh_preserves_previous_business_description(business_client, setup):
    client, _, response, _, _ = business_client
    first = client.post("/api/business-research", json=BODY).json()
    with setup[1].begin() as db:
        db.get(BusinessResearch, first["id"]).created_at -= timedelta(minutes=2)
    response["quote"] = "원문에 없는 계약"
    second = client.post("/api/business-research", json={**BODY, "request_key": "new-refresh-request"}).json()
    assert second["status"] == "failed"
    history = client.get("/api/business-research?code=123456").json()
    assert len(history["items"]) == 2
    assert history["context"]["sha256"] == first["context"]["sha256"]


def test_existing_description_is_combined_with_web_evidence_without_losing_provenance(business_client, setup):
    client, calls, _, uid, _ = business_client
    with setup[1].begin() as db:
        profile = db.get(StockFactorProfile, (uid, "123456"))
        profile.value = {
            **profile.value,
            "business_context": {
                "description": "기존에 등록한 시험기업의 검사장비 사업 설명",
                "sources": ["https://issuer.test/about"],
                "source_kind": "administrator_supplied",
                "retrieved_at": "2026-10-01T00:00:00+00:00",
            },
        }
    result = client.post("/api/business-research", json=BODY).json()
    assert result["status"] == "completed"
    payload = json.loads(json.loads(calls[1].content)["messages"][1]["content"])
    assert "검사장비" in payload["existing_business_context"]["description"]
    old = next(e for e in payload["evidence"] if e.get("source_kind") == "existing_context")
    assert old["url"] == "https://issuer.test/about"
    assert old["retrieved_at"].startswith("2026-10-01")
    assert result["context"]["baseline"]["source_kind"] == "administrator_supplied"
    viewed = client.get("/api/vectors/defaults/123456").json()["profile"]
    assert viewed["origin"] == "manual" and viewed["selected_dimensions"] == ["sector-01"]
    assert viewed["business_context"]["sha256"] == result["context"]["sha256"]


def test_interrupted_record_requires_owner_confirmation_and_never_retries(business_client, setup):
    client, calls, _, uid, tid = business_client
    from nastack_trading.db import now

    with setup[1].begin() as db:
        record = BusinessResearch(
            owner_id=uid,
            tenant_id=tid,
            code="123456",
            request_key="interrupted",
            status="running",
            created_at=now() - timedelta(minutes=6),
            value={},
        )
        db.add(record)
        db.flush()
        identity = record.id
    assert client.post("/api/business-research", json=BODY).status_code == 409
    assert not calls
    assert client.post(f"/api/business-research/{identity}/abandon", json={}).status_code == 422
    result = client.post(
        f"/api/business-research/{identity}/abandon", json={"confirmation": "미완료 조사 종료 확인"}
    )
    assert result.json()["status"] == "abandoned" and not calls


def test_disabled_personal_cloud_key_never_uses_public_key(business_client, setup):
    from cryptography.fernet import Fernet

    from nastack_trading.user_brokers import cipher

    _, _, _, uid, tid = business_client
    config = setup[0].model_copy(
        update={
            "broker_encryption_key": SecretStr(Fernet.generate_key().decode()),
            "openai_api_key": SecretStr("public-secret"),
            "tavily_api_key": SecretStr("search-secret"),
        }
    )
    with setup[1].begin() as db:
        saved = db.get(UserSettings, uid)
        saved.value = {
            **saved.value,
            "analysis_model": {"provider": "openai"},
            "cloud_model": "gpt-5.6-luna",
            "cloud_enabled": True,
            "personal_connections": {
                "openai": {
                    "version": 1,
                    "ciphertext": cipher(config)
                    .encrypt(json.dumps({"enabled": False, "api_key": "private-secret"}).encode())
                    .decode(),
                }
            },
        }
        token = create_session(db, db.get(User, uid), 24)

    def forbidden(request):
        pytest.fail("disabled personal key must block search and cloud inference")

    with TestClient(
        create_app(config, news_transport=httpx.MockTransport(forbidden)), headers={"origin": ORIGIN}
    ) as client:
        client.cookies.set("trading_session", token)
        result = client.post("/api/business-research", json={**BODY, "cloud_confirmed": True})
        assert result.status_code == 200 and result.json()["status"] == "failed"
        assert result.json()["error"] == "OPENAI_NOT_CONFIGURED"
        assert "public-secret" not in result.text and "private-secret" not in result.text
