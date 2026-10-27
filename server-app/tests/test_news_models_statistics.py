import json

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from test_storage_news import clock, feed
from test_vectors import payload, seed_vector_data

from nastack_trading.app import create_app
from nastack_trading.dimension_catalog import BY_ID
from nastack_trading.models import (
    DimensionCollection,
    NewsRun,
    PersonalSource,
    StatisticSeries,
    StockFactorProfile,
    User,
    UserSettings,
)
from nastack_trading.news import capability
from nastack_trading.news_models import resolve_model
from nastack_trading.statistics import worldbank_points


def test_worldbank_observation_year_and_identity():
    def respond(request):
        assert request.url.host == "api.worldbank.org"
        return httpx.Response(
            200,
            json=[
                {"pages": 1},
                [
                    {
                        "countryiso3code": "KOR",
                        "indicator": {"id": "NV.IND.MANF.CD"},
                        "date": "2025",
                        "value": 123,
                    },
                    {
                        "countryiso3code": "KOR",
                        "indicator": {"id": "NV.IND.MANF.CD"},
                        "date": "2024",
                        "value": None,
                    },
                ],
            ],
        )

    assert worldbank_points(httpx.MockTransport(respond)) == [{"date": "2025-01-01", "value": 123.0}]
    with pytest.raises(ValueError):
        worldbank_points(httpx.MockTransport(lambda r: httpx.Response(200, json=[{"pages": 2}, []])))


def test_model_default_override_discovery_and_stats(admin, setup):
    config, factory = setup
    config = config.model_copy(
        update={
            "vllm_base_url": "http://model.test/v1",
            "vllm_model_id": "base-model",
            "openai_api_key": SecretStr(""),
        }
    )
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "large-model"}]})
        assert request.url.host == "fred.stlouisfed.org"
        return httpx.Response(200, text="observation_date,INDPRO\n2026-01-01,101.2\n2026-02-01,102.3\n")

    with TestClient(
        create_app(config, news_transport=httpx.MockTransport(handler)),
        headers={"origin": "http://testserver"},
        cookies=admin.cookies,
    ) as client:
        initial = client.get("/api/news/models").json()
        assert initial["selected"] == "default"
        assert initial["default"]["model"] == "base-model"
        assert calls == []
        result = client.get("/api/news/models?refresh=true").json()
        assert any(item["model"] == "large-model" for item in result["items"])
        assert client.put("/api/news/model", json={"model": "vllm:large-model"}).status_code == 200
        assert client.get("/api/news/models").json()["selected"] == "vllm:large-model"
        assert client.put("/api/news/model", json={"model": "https://evil.example"}).status_code == 422
        assert client.get("/api/statistics").json()["series"][0]["points"] == []
        first = client.post("/api/statistics/INDPRO/refresh")
        assert first.status_code == 200, first.text
        assert first.json()["count"] == 2
        assert client.post("/api/statistics/INDPRO/refresh").json()["cached"] is True
        assert len(calls) == 2  # Discovery + statistics, no LLM inference.
        stored = client.get("/api/statistics").json()
        assert stored["series"][0]["frequency"] == "월간"
        assert stored["series"][0]["points"][1]["value"] == 102.3
        assert client.post("/api/statistics/NOT_ALLOWED/refresh").status_code == 404
    with factory() as db:
        assert db.scalar(select(StatisticSeries)).value["points"][0]["date"] == "2026-01-01"
        assert db.scalar(select(UserSettings)).value["news_model"] == "vllm:large-model"
        assert db.scalar(select(NewsRun)) is None


def test_statistics_sources_cannot_be_news_scored(admin, setup):
    admin.get("/api/news/today")
    with setup[1]() as db:
        source = db.scalar(select(PersonalSource).where(PersonalSource.domain == "bls.gov"))
        if not source:
            user = db.scalar(select(User))
            source = PersonalSource(
                owner_id=user.id,
                tenant_id=user.tenant_id,
                domain="bls.gov",
                value={
                    "name": "BLS",
                    "url": "https://www.bls.gov/",
                    "status": "active",
                    "disposition": "use",
                },
            )
            db.add(source)
        db.commit()
        assert capability(source)["kind"] == "statistics"
        assert capability(source)["eligible"] is False
        assert capability(source)["connected"] is False
    data = admin.get("/api/news/today").json()
    assert next(s for s in data["sources"] if s["domain"] == "bls.gov")["score_sum"] is None
    assert not any(s["domain"] == "bls.gov" for s in admin.get("/api/statistics").json()["sources"])


def test_collect_once_then_stock_specific_analysis_without_refetch(admin, setup, monkeypatch):
    from nastack_trading.models import Instrument, WatchlistItem

    monkeypatch.setattr("nastack_trading.news.now", clock)
    seed_vector_data(setup[1])
    with setup[1]() as db:
        user = db.scalar(select(User))
        second = Instrument(
            tenant_id=user.tenant_id,
            code="654321",
            value={"name": "합성 소비기업", "market": "KOSPI", "asset_type": "stock", "status": "active"},
        )
        db.add(second)
        db.flush()
        db.add(WatchlistItem(owner_id=user.id, tenant_id=user.tenant_id, instrument_id=second.id))
        source = PersonalSource(
            owner_id=user.id,
            tenant_id=user.tenant_id,
            domain="eia.gov",
            value={"name": "EIA", "url": "https://www.eia.gov/", "status": "active", "disposition": "use"},
        )
        db.add(source)
        db.commit()
        source_id = source.id
    calls = []

    def handler(request):
        calls.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, content=feed())
        content = json.loads(json.loads(request.content)["messages"][1]["content"])
        instrument = content["selected_instrument"]
        assert instrument["business_context"]
        report = {
            "assessments": [
                {
                    "article_id": content["articles"][0]["id"],
                    "dimension_id": "stock-" + instrument["code"],
                    "score9": 3 if instrument["code"] == "123456" else -3,
                    "reason": "합성 종목별 원유 노출 차이 검증",
                    "evidence": "Oil supply disruption",
                }
            ]
        }
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(report)}}]})

    config = setup[0].model_copy(
        update={"news_enabled": True, "vllm_base_url": "http://model.test/v1", "vllm_model_id": "base-model"}
    )
    with TestClient(
        create_app(config, news_transport=httpx.MockTransport(handler)),
        headers={"origin": "http://testserver"},
        cookies=admin.cookies,
    ) as client:
        collected = client.post("/api/news/collect", json={"source_ids": [source_id], "collect_only": True})
        assert collected.status_code == 200, collected.text
        assert calls == ["GET"]
        for code in ["123456", "654321"]:
            result = client.post(
                "/api/news/analyze",
                json={"source_ids": [source_id], "code": code, "stock_context": "합성 사업 정보"},
            )
            assert result.status_code == 200, result.text
            assert result.json()["status"] == "completed", result.text
        assert calls == ["GET", "POST", "POST"]
        assert client.get("/api/news/today?code=123456").json()["total_sum"] == 2
        assert client.get("/api/news/today?code=654321").json()["total_sum"] == -2
        assert client.get("/api/news/today").json()["total_sum"] is None
        assert client.get("/api/news/today?code=999999").status_code == 404


def test_cloud_requires_settings_allowance_and_per_execution_consent(setup):
    config = setup[0].model_copy(
        update={
            "openai_api_key": SecretStr("synthetic-not-a-key"),
            "cloud_model_allowlist": ["synthetic-premium"],
        }
    )
    with setup[1]() as db:
        user = db.scalar(select(User))
        with pytest.raises(HTTPException, match="클라우드 사용"):
            resolve_model(db, user, config, "openai:synthetic-premium", True)
        settings = db.get(UserSettings, user.id)
        settings.value = {**settings.value, "cloud_enabled": True}
        db.commit()
        with pytest.raises(HTTPException, match="외부 전송"):
            resolve_model(db, user, config, "openai:synthetic-premium", False)
        assert resolve_model(db, user, config, "openai:synthetic-premium", True)["provider"] == "openai"


def test_factor_attributes_only_manual_save_and_replace(admin, setup):
    seed_vector_data(setup[1])
    with setup[1]() as db:
        original = db.scalar(select(DimensionCollection))
        db.add(
            DimensionCollection(
                owner_id=original.owner_id,
                tenant_id=original.tenant_id,
                dimension_id="sector-02",
                start_date=original.start_date,
                end_date=original.end_date,
                status="completed",
                value={**original.value, "series_code": BY_ID["sector-02"]["series_code"]},
            )
        )
        db.commit()
    first = admin.post("/api/vectors/analyze", json=payload()).json()
    with setup[1]() as db:
        assert db.scalar(select(StockFactorProfile)) is None
    saved = admin.post("/api/vectors/attributes", json={"result_id": first["persistence"]["id"]})
    assert saved.status_code == 200, saved.text
    assert saved.json()["selected_dimensions"] == ["sector-01"]
    second = admin.post("/api/vectors/analyze", json=payload(dimensions=["sector-02"])).json()
    with setup[1]() as db:
        assert db.scalar(select(StockFactorProfile)).value["selected_dimensions"] == ["sector-01"]
    updated = admin.post("/api/vectors/attributes", json={"result_id": second["persistence"]["id"]})
    assert updated.status_code == 200, updated.text
    assert updated.json()["selected_dimensions"] == ["sector-02"]
    context = admin.get("/api/news/today?code=123456").json()["instrument"]["trading_factor_profile"]
    assert context["selected_dimensions"] == ["sector-02"]
    assert context["usable_for_news"] is True
    assert (
        admin.post("/api/vectors/attributes", json={"result_id": "not-owned-or-missing"}).status_code == 404
    )
