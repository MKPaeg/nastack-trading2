import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import httpx
import pytest
from fastapi.testclient import TestClient

from nastack_trading import model_pricing
from nastack_trading.app import create_app
from nastack_trading.config import Config
from nastack_trading.llm_usage import begin_call, estimate, finish_call, usage_scope, usage_snapshot
from nastack_trading.model_pricing import ModelPrice, parse_model_price
from nastack_trading.research import ResearchError, ResearchPipeline


@pytest.fixture(autouse=True)
def known_test_prices(monkeypatch):
    rates = {"gpt-5.6-sol": (4, .4, 20), "gpt-6.1-sol": (2, .1, 10),
             "gpt-6.1-terra": (1, .05, 5)}

    def fetch(model, day):
        if model not in rates:
            raise ValueError("model price unavailable")
        incoming, cached, outgoing = rates[model]
        return ModelPrice(model, incoming, cached, incoming * 1.25, outgoing, day,
                          model_pricing.MODEL_SOURCE + model)

    monkeypatch.setattr(model_pricing, "fetch_model_price", fetch)


def entry(usage, model="gpt-5.6-sol", provider="openai"):
    item = {"provider": provider, "model": model}
    finish_call(item, {"usage": usage})
    return item


def test_actual_tokens_cached_discount_and_output_reasoning_not_double_counted():
    item = entry({"input_tokens": 10000, "input_tokens_details": {"cached_tokens": 6000,
                  "cache_write_tokens": 0},
                  "output_tokens": 2000, "output_tokens_details": {"reasoning_tokens": 1500}, "total_tokens": 12000})
    assert estimate(item, date(2026, 10, 27)) == pytest.approx(.0584)
    with usage_scope() as ledger:
        ledger.append(item)
        result = usage_snapshot()
    assert result["total_tokens"] == 12000 and result["output_tokens"] == 2000
    assert result["cache_write_known"] and result["cache_write_tokens"] == 0
    assert result["known"] and result["cost_known"] and result["estimated_usd"] == pytest.approx(.0584)


def test_reported_cache_writes_use_their_own_rate_not_an_extra_surcharge():
    item = entry({"input_tokens": 10000, "input_tokens_details": {"cached_tokens": 2000,
                  "cache_write_tokens": 3000}, "output_tokens": 1000, "total_tokens": 11000},
                 "gpt-6.1-sol")
    assert estimate(item, date(2026, 10, 27)) == pytest.approx(.0277)
    assert item["cache_write_tokens"] == 3000


def test_missing_cache_write_breakdown_is_not_silently_free():
    item = entry({"input_tokens": 10000, "input_tokens_details": {"cached_tokens": 0},
                  "output_tokens": 1000, "total_tokens": 11000}, "gpt-6.1-sol")
    assert estimate(item, date(2026, 10, 27)) is None
    with usage_scope() as ledger:
        ledger.append(item)
        assert not usage_snapshot()["cache_write_known"]


@pytest.mark.parametrize("changes", [
    {"model": "gpt-5.6-sol-unverified"}, {"provider": "local_model"},
    {"cache_write_ttl": "12h"}, {"service_tier": False}, {"input_tokens": 300000},
    {"cached_input_tokens": 200}, {"input_tokens": None}, {"total_tokens": 999},
])
def test_unknown_rates_or_billing_details_are_not_free(changes):
    item = entry({"input_tokens": 100, "input_tokens_details": {"cached_tokens": 0,
                  "cache_write_tokens": 0}, "output_tokens": 10, "total_tokens": 110})
    item.update(changes)
    assert estimate(item, date(2026, 10, 27)) is None


def test_daily_refresh_and_mixed_model_prices(monkeypatch):
    calls = []

    def fetch(model, day):
        calls.append((model, day))
        incoming = 2 if day == date(2026, 10, 27) else 3
        return ModelPrice(model, incoming, 0.1, incoming * 1.25, 10, day,
                          model_pricing.MODEL_SOURCE + model)

    monkeypatch.setattr(model_pricing, "fetch_model_price", fetch)
    item = entry({"input_tokens": 1000, "input_tokens_details": {"cached_tokens": 0,
                  "cache_write_tokens": 0},
                  "output_tokens": 100, "total_tokens": 1100}, "gpt-6.1-sol")
    assert estimate(item, date(2026, 10, 27)) == pytest.approx(.003)
    assert estimate(item, date(2026, 10, 27)) == pytest.approx(.003)
    assert estimate(item, date(2026, 10, 28)) == pytest.approx(.004)
    assert calls == [("gpt-6.1-sol", date(2026, 10, 27)), ("gpt-6.1-sol", date(2026, 10, 28))]


def test_each_selected_model_gets_its_own_price():
    sol = entry({"input_tokens": 1000, "input_tokens_details": {"cached_tokens": 0,
                 "cache_write_tokens": 0},
                 "output_tokens": 100, "total_tokens": 1100}, "gpt-6.1-sol")
    terra = entry({"input_tokens": 1000, "input_tokens_details": {"cached_tokens": 0,
                   "cache_write_tokens": 0},
                   "output_tokens": 100, "total_tokens": 1100}, "gpt-6.1-terra")
    with usage_scope() as ledger:
        ledger.extend([sol, terra])
        result = usage_snapshot()
    assert result["cost_known"] and result["estimated_usd"] == pytest.approx(.0045)
    assert set(result["pricing_models"]) == {"gpt-6.1-sol", "gpt-6.1-terra"}
    assert [(item["model"], item["estimated_usd"]) for item in result["model_usage"]] == [
        ("gpt-6.1-sol", .003), ("gpt-6.1-terra", .0015)]


def test_failed_new_day_lookup_never_uses_yesterdays_price(monkeypatch):
    def fetch(model, day):
        if day == date(2026, 10, 28):
            raise ValueError("published page changed")
        return ModelPrice(model, 2, .1, 2.5, 10, day, model_pricing.MODEL_SOURCE + model)

    monkeypatch.setattr(model_pricing, "fetch_model_price", fetch)
    item = entry({"input_tokens": 1000, "input_tokens_details": {"cached_tokens": 0,
                  "cache_write_tokens": 0},
                  "output_tokens": 100, "total_tokens": 1100}, "gpt-6.1-sol")
    assert estimate(item, date(2026, 10, 27)) == pytest.approx(.003)
    assert estimate(item, date(2026, 10, 28)) is None


def test_official_model_page_parser_is_strict():
    page = ("<h1>GPT-6.1 Sol</h1><code>gpt-6.1-sol</code><h2>Pricing</h2>"
            "<h3>Text tokens</h3><p>Per 1M tokens</p><div>Input</div><div>$2.00</div>"
            "<div>Cached input</div><div>$0.10</div><div>Cache writes</div><div>$2.50</div>"
            "<div>Output</div><div>$10.00</div>")
    rate = parse_model_price(page, "gpt-6.1-sol", date(2026, 10, 27))
    assert (rate.input_usd, rate.cached_input_usd, rate.cache_write_30m_usd, rate.output_usd) == (
        2, .1, 2.5, 10)
    with pytest.raises(ValueError):
        parse_model_price(page, "gpt-6.1-terra", date(2026, 10, 27))


def test_official_three_rate_page_prices_connection_test_without_cache_writes(monkeypatch):
    page = ("<h1>GPT-5.6 Luna</h1><code>gpt-5.6-luna</code><h2>Pricing</h2>"
            "<h3>Text tokens</h3><p>Per 1M tokens</p><div>Input</div><div>$0.20</div>"
            "<div>Cached input</div><div>$0.02</div><div>Output</div><div>$1.20</div>"
            "<h3>Quick comparison</h3>")
    rate = parse_model_price(page, "gpt-5.6-luna", date(2026, 10, 27))
    assert (rate.input_usd, rate.cached_input_usd, rate.cache_write_30m_usd, rate.output_usd) == (
        .2, .02, None, 1.2)
    item = entry({"input_tokens": 62, "input_tokens_details": {"cached_tokens": 0,
                  "cache_write_tokens": 0}, "output_tokens": 5, "total_tokens": 67}, "gpt-5.6-luna")
    monkeypatch.setattr(model_pricing, "fetch_model_price", lambda model, day: rate)
    assert estimate(item, date(2026, 10, 27)) == pytest.approx(.0000184)


def test_official_luna_page_prices_default_cache_writes(monkeypatch):
    page = ("<h1>GPT-5.6 Luna</h1><code>gpt-5.6-luna</code><h2>Pricing</h2>"
            "<h3>Text tokens</h3><p>Per 1M tokens</p><div>Input</div><div>$0.20</div>"
            "<div>Cached input</div><div>$0.02</div><div>Output</div><div>$1.20</div>"
            "<h3>Quick comparison</h3><p>Cache writes are billed at 1.25x the uncached input token rate.</p>")
    rate = parse_model_price(page, "gpt-5.6-luna", date(2026, 10, 28))
    assert rate.cache_write_30m_usd == pytest.approx(.25)
    monkeypatch.setattr(model_pricing, "fetch_model_price", lambda model, day: rate)
    item = entry({"input_tokens": 2470, "input_tokens_details": {"cached_tokens": 0,
                  "cache_write_tokens": 2467}, "output_tokens": 699, "total_tokens": 3169}, "gpt-5.6-luna")
    assert estimate(item, date(2026, 10, 28)) == pytest.approx(.00145615)
    item["cache_write_ttl"] = "12h"
    assert estimate(item, date(2026, 10, 28)) is None


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
