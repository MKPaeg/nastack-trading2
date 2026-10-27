import json
import time
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from nastack_trading.app import create_app
from nastack_trading.config import Config
from nastack_trading.db import now
from nastack_trading.jobs import claim_job
from nastack_trading.models import AnalysisRun, LLMUsageRecord
from nastack_trading.research import ResearchError, ResearchPipeline, public_url
from nastack_trading.schemas import Preferences
from nastack_trading.worker import work_once


def fixtures(**overrides):
    config = Config(
        research_enabled=True,
        vllm_base_url="http://win11:8000/v1",
        tavily_api_key="secret-search",
        openai_api_key="secret-cloud",
        _env_file=None,
    )
    settings = Preferences(
        analysis_model={"provider": "vllm", "base_url": config.vllm_base_url, "model_id": "local-model"},
        sources=[
            {"domain": "example.com", "disposition": "use"},
            {"domain": "news.example.org", "disposition": "reference"},
            {"domain": "blocked.example.com", "disposition": "exclude"},
        ],
    )
    value = settings.model_dump()
    value.update(overrides)
    return config, SimpleNamespace(symbol="005930", settings_version=2, settings_snapshot=value)


def provider(report=None, *, cloud=False, fail=False, search_only=False):
    calls = []

    def handle(request):
        payload = json.loads(request.content)
        calls.append((str(request.url), payload))
        if request.url.host == "api.tavily.com":
            assert "secret-search" in request.headers["authorization"]
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"url": "https://example.com/a", "title": "공시", "content": "공시 근거"},
                        {"url": "https://example.com/a", "title": "중복", "content": "중복 근거"},
                        {"url": "https://news.example.org/a", "title": "참고", "content": "참고 기사"},
                        {"url": "https://blocked.example.com/a", "content": "제외 기사"},
                        {"url": "http://127.0.0.1/private", "content": "사설"},
                        {"url": "https://unknown.example.org/a", "content": "미등록 기사"},
                    ]
                },
            )
        assert not search_only
        if fail:
            raise httpx.ReadTimeout("secret-cloud should not reach DB")
        text = json.dumps(
            report
            or {
                "summary": "공시 기준 연구 요약",
                "outlook": "uncertain",
                "evidence_ids": ["E1"],
                "risks": ["실시간 시세 미수집"],
            }
        )
        if cloud:
            assert payload["store"] is False
            assert "secret-cloud" in request.headers["authorization"]
            model_input = payload["input"]
            response = {"output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]}
        else:
            model_input = payload["messages"][1]["content"]
            response = {"choices": [{"message": {"content": text}}]}
        assert "참고 기사" not in model_input and "미등록 기사" not in model_input
        assert "제외 기사" not in model_input
        assert "secret" not in model_input
        return httpx.Response(200, json=response)

    return httpx.MockTransport(handle), calls


def test_vllm_report_evidence_and_no_order():
    config, run = fixtures()
    transport, calls = provider()
    stages = []
    result = ResearchPipeline(config, transport)(run, lambda p, m: stages.append(p) or True)
    assert result["simulation"] is False and result["action"] == "abstain"
    assert result["trading_enabled"] is False
    assert len(result["evidence"]) == 3
    assert result["evidence"][1]["disposition"] == "reference"
    assert result["evidence"][0]["retrieved_at"].endswith("+09:00")
    assert len(result["evidence"][0]["content_hash"]) == 64
    assert len(calls) == 2 and stages == [35, 65]


def test_openai_explicit_opt_in():
    config, run = fixtures(analysis_model={"provider": "openai"}, cloud_enabled=True)
    transport, calls = provider(cloud=True)
    result = ResearchPipeline(config, transport)(run)
    assert result["model"]["model_id"] == "gpt-5.6-luna"
    assert calls[-1][0] == "https://api.openai.com/v1/responses"


@pytest.mark.parametrize(
    "overrides,code",
    [
        ({"analysis_model": {"provider": "openai"}}, "CLOUD_NOT_ALLOWED"),
        (
            {"analysis_model": {"provider": "vllm", "base_url": "http://evil.example/v1", "model_id": "x"}},
            "VLLM_ENDPOINT_NOT_ALLOWED",
        ),
        (
            {"analysis_model": {"provider": "openai"}, "cloud_enabled": True, "cloud_model": "unapproved"},
            "MODEL_NOT_ALLOWED",
        ),
        ({"sources": []}, "SOURCE_POLICY_REQUIRED"),
    ],
)
def test_configuration_gate_before_network(overrides, code):
    config, run = fixtures(**overrides)
    transport, calls = provider()
    with pytest.raises(ResearchError, match=code):
        ResearchPipeline(config, transport)(run)
    assert not calls


@pytest.mark.parametrize("ids", [["E999"], ["E2"]])
def test_invented_or_reference_evidence_rejected(ids):
    config, run = fixtures()
    transport, _ = provider({"summary": "invalid", "outlook": "positive", "evidence_ids": ids, "risks": []})
    with pytest.raises(ResearchError, match="MODEL_INVALID_EVIDENCE"):
        ResearchPipeline(config, transport)(run)


def test_reference_only_abstains_without_model_call():
    config, run = fixtures(sources=[{"domain": "example.com", "disposition": "reference"}])
    transport, calls = provider(search_only=True)
    result = ResearchPipeline(config, transport)(run)
    assert result["outlook"] == "uncertain" and result["evidence_ids"] == []
    assert len(calls) == 1


def test_local_timeout_never_falls_back_to_cloud():
    config, run = fixtures(cloud_enabled=True)
    transport, calls = provider(fail=True)
    with pytest.raises(ResearchError, match="PROVIDER_TIMEOUT"):
        ResearchPipeline(config, transport)(run)
    assert len(calls) == 2 and not any("openai.com" in url for url, _ in calls)


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "http://localhost/a",
        "http://192.168.1.1/a",
        "http://169.254.169.254/a",
        "https://u:p@example.com/a",
        "http://[::1]/a",
    ],
)
def test_unsafe_source_links_not_stored(url):
    assert public_url(url) is None


def test_worker_persists_live_report(admin, setup):
    config, run = fixtures()
    settings = admin.get("/api/settings").json()
    assert (
        admin.put(
            "/api/settings", json={"version": settings["version"], "value": run.settings_snapshot}
        ).status_code
        == 200
    )
    response = admin.post("/api/runs", json={"symbol": "005930", "request_key": "research-request"}).json()
    with setup[1].begin() as db:
        db.get(AnalysisRun, response["id"]).kind = "research"
    transport, _ = provider()
    work_once(setup[1], pause=lambda _: None, evaluator=ResearchPipeline(config, transport))
    saved = admin.get(f"/api/runs/{response['id']}").json()
    assert saved["status"] == "completed" and saved["result"]["evidence_ids"] == ["E1"]
    assert saved["result"]["settings_version"] == 2
    with setup[1]() as db:
        records = db.query(LLMUsageRecord).all()
        assert len(records) == 1
        assert records[0].owner_id == db.get(AnalysisRun, response["id"]).owner_id
        assert records[0].category == "evaluation" and records[0].model == "local-model"
        assert records[0].estimated_usd is None  # Provider omitted usage and local pricing.


def test_external_crash_not_replayed(admin, setup):
    response = admin.post("/api/runs", json={"symbol": "005930", "request_key": "crash-request"}).json()
    with setup[1].begin() as db:
        run = db.get(AnalysisRun, response["id"])
        run.kind, run.status, run.lease_until = "research", "running", now() - timedelta(seconds=10)
    assert claim_job(setup[1]) is None
    assert admin.get(f"/api/runs/{response['id']}").json()["error_code"] == "EXTERNAL_CALL_UNKNOWN"


def test_integration_status_no_secrets(admin):
    response = admin.get("/api/integrations")
    assert response.status_code == 200
    assert response.json()["research_enabled"] is False
    assert "api_key" not in response.text


@pytest.mark.parametrize(
    "response,code",
    [
        (httpx.Response(302, headers={"location": "http://127.0.0.1/secret"}), "PROVIDER_HTTP_ERROR"),
        (httpx.Response(200, content=b"not json"), "PROVIDER_INVALID_RESPONSE"),
        (httpx.Response(200, content=b"x" * 1_000_001), "RESPONSE_TOO_LARGE"),
    ],
)
def test_provider_redirect_bad_json_and_size_limit(response, code):
    config, _ = fixtures()
    pipeline = ResearchPipeline(config, httpx.MockTransport(lambda _: response))
    with pytest.raises(ResearchError, match=code):
        pipeline.post("https://api.tavily.com/search", "key", {}, 5)


def test_bad_report_extra_order_field_rejected():
    config, run = fixtures()
    transport, _ = provider(
        {"summary": "ignored", "outlook": "positive", "evidence_ids": ["E1"], "risks": [], "action": "buy"}
    )
    with pytest.raises(ResearchError, match="MODEL_INVALID_REPORT"):
        ResearchPipeline(config, transport)(run)


def test_policy_invalidation_before_model_call():
    config, run = fixtures()
    transport, calls = provider(search_only=True)
    with pytest.raises(ResearchError, match="JOB_INVALIDATED"):
        ResearchPipeline(config, transport)(run, lambda progress, message: progress < 65)
    assert len(calls) == 1


def test_long_call_heartbeat_prevents_duplicate_claim(admin, setup):
    config, run = fixtures()
    response = admin.post("/api/runs", json={"symbol": "005930", "request_key": "heartbeat-request"}).json()
    with setup[1].begin() as db:
        row = db.get(AnalysisRun, response["id"])
        row.kind, row.settings_snapshot = "research", run.settings_snapshot
        row.settings_version = 1
    transport, _ = provider()
    original = transport.handle_request

    def slow(request):
        time.sleep(0.6)  # Exceeds the deliberately shortened test lease.
        assert claim_job(setup[1], lease_seconds=0.3) is None
        return original(request)

    work_once(
        setup[1],
        lease_seconds=0.3,
        pause=lambda _: None,
        evaluator=ResearchPipeline(config, httpx.MockTransport(slow)),
    )
    saved = admin.get(f"/api/runs/{response['id']}").json()
    assert saved["status"] == "completed" and saved["attempts"] == 1


def test_api_enabled_research_kind_and_status(admin, setup):
    config = setup[0].model_copy(update={"research_enabled": True})
    with TestClient(create_app(config), headers={"origin": "http://testserver"}) as client:
        # Reuse an authenticated session, never create a real provider call.
        client.cookies.update(admin.cookies)
        assert (
            client.post(
                "/api/admin/instruments",
                json={
                    "code": "005930",
                    "name": "시험 종목",
                    "market": "KOSPI",
                    "source_note": "테스트용 합성 메타데이터",
                },
            ).status_code
            == 201
        )
        result = client.post("/api/runs", json={"symbol": "005930", "request_key": "enabled-request"}).json()
        assert result["kind"] == "research"
        assert client.get("/api/integrations").json()["research_enabled"] is True
        assert client.get("/api/health").json()["analysis"] == "research"
