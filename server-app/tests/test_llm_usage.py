from concurrent.futures import ThreadPoolExecutor
from datetime import date
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from nastack_trading.app import create_app
from nastack_trading.config import Config
from nastack_trading.llm_usage import begin_call, estimate, finish_call, usage_scope, usage_snapshot
from nastack_trading.research import ResearchError, ResearchPipeline


def entry(usage, model="gpt-5.6-sol", provider="openai"):
    item = {"provider": provider, "model": model}
    finish_call(item, {"usage": usage})
    return item


def test_actual_tokens_cached_discount_and_output_reasoning_not_double_counted():
    item = entry({"input_tokens": 10000, "input_tokens_details": {"cached_tokens": 6000},
                  "output_tokens": 2000, "output_tokens_details": {"reasoning_tokens": 1500}, "total_tokens": 12000})
    assert estimate(item, date(2026, 10, 27)) == pytest.approx(.0584)
    with usage_scope() as ledger:
        ledger.append(item)
        result = usage_snapshot()
    assert result["total_tokens"] == 12000 and result["output_tokens"] == 2000
    assert result["known"] and result["cost_known"] and result["estimated_usd"] == pytest.approx(.0584)


@pytest.mark.parametrize("changes", [
    {"model": "gpt-5.6-sol-unverified"}, {"provider": "local_model"},
    {"cache_write_tokens": 50}, {"service_tier": False}, {"input_tokens": 300000},
    {"cached_input_tokens": 200}, {"input_tokens": None}, {"total_tokens": 999},
])
def test_unknown_rates_or_billing_details_are_not_free(changes):
    item = entry({"input_tokens": 100, "output_tokens": 10, "total_tokens": 110})
    item.update(changes)
    assert estimate(item, date(2026, 10, 27)) is None
    assert estimate(item, date(2026, 11, 22)) is None


def test_missing_malformed_failed_usage_and_scoped_isolation():
    def work(i):
        with usage_scope():
            row = begin_call("openai", {"model": "gpt-5.6-sol"})
            finish_call(row, {"usage": {"input_tokens": i, "output_tokens": 10, "total_tokens": i+10}})
            begin_call("openai", {"model": "gpt-5.6-sol"})  # Failed/unreported, not zero.
            assert begin_call("tavily", {}) is None
            return usage_snapshot()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(work, [100, 200]))
    assert [r["total_tokens"] for r in results] == [110, 210]
    assert all(r["llm_calls"] == 2 and r["unreported_calls"] == 1 and not r["cost_known"] for r in results)
    assert usage_snapshot() is None
    bad = entry({"input_tokens": True, "output_tokens": -1, "total_tokens": "500", "secret": "private"})
    assert bad["total_tokens"] is None
    assert "private" not in json.dumps(bad)


@pytest.mark.parametrize("status", [200, 400])
def test_post_records_reported_usage_even_when_response_is_rejected(status):
    transport = httpx.MockTransport(lambda request: httpx.Response(status, json={
        "usage": {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110},
        "error": {"message": "private-key", "code": "invalid_request_error"}}))
    with usage_scope():
        pipeline = ResearchPipeline(Config(_env_file=None), transport)
        if status == 200:
            pipeline.post("https://api.openai.com/v1/responses", "private-key", {"model": "gpt-5.6-sol"}, 1)
        else:
            with pytest.raises(ResearchError):
                pipeline.post("https://api.openai.com/v1/responses", "private-key", {"model": "gpt-5.6-sol"}, 1)
        result = usage_snapshot()
        assert result["llm_calls"] == 1 and result["total_tokens"] == 110
        assert "private-key" not in json.dumps(result)


def test_http_request_usage_header_is_local_to_each_request(setup):
    app = create_app(setup[0])
    @app.post("/api/synthetic-usage/{amount}")
    def synthetic(amount: int):
        row = begin_call("openai", {"model": "gpt-5.6-sol"})
        finish_call(row, {"usage": {"input_tokens": amount, "output_tokens": 10, "total_tokens": amount+10}})
        return {"status": "synthetic"}
    from conftest import ORIGIN
    with TestClient(app, headers={"origin": ORIGIN}) as client:
        for amount in (100, 200):
            response = client.post(f"/api/synthetic-usage/{amount}")
            assert json.loads(response.headers["X-LLM-Usage"])["total_tokens"] == amount+10
        empty = client.post("/api/auth/logout")
        assert json.loads(empty.headers["X-LLM-Usage"])["llm_calls"] == 0
