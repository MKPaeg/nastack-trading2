from datetime import timedelta

import pytest
from conftest import ORIGIN
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from test_business_research import business_client  # noqa: F401

from nastack_trading.db import now
from nastack_trading.models import (
    BrokerSnapshot,
    StockFactorProfile,
    Tenant,
    User,
    UserSettings,
    WatchlistItem,
)
from nastack_trading.research import ResearchPipeline
from nastack_trading.security import create_session


def enrollment(setup, uid, tid, failure=False):
    with setup[1].begin() as db:
        db.execute(delete(WatchlistItem).where(WatchlistItem.owner_id == uid))
        db.execute(delete(StockFactorProfile).where(StockFactorProfile.owner_id == uid))
        settings = db.get(UserSettings, uid)
        settings.value = {**settings.value, "default_attributes_enabled": False,
                          "default_attributes_business_enabled": False, "default_attributes_cloud_enabled": False}
        original = dict(settings.value)
        db.add(BrokerSnapshot(owner_id=uid, tenant_id=tid, kind="analysis_entry", symbol="123456", environment="public",
            value={"code": "123456", "name": "시험기업", "market": "KRX", "asset_type": "stock", "status": "active"}))
        if failure:
            db.add(StockFactorProfile(owner_id=uid, tenant_id=tid, code="123456", value={"origin": "llm_default",
                "status": "needs_review", "error": "BUSINESS_INFO_REQUIRED", "llm_used": False,
                "selected_dimensions": []}))
        return original


@pytest.mark.parametrize("failure", [False, True])
def test_one_run_research_and_attributes_without_enabling_automatic_flags(business_client, setup, monkeypatch, failure):  # noqa: F811
    client, calls, _, uid, tid = business_client
    assert client.post("/api/recommendations/123456/initialize", json={"confirmed": True}).status_code == 404
    original = enrollment(setup, uid, tid, failure)
    post = ResearchPipeline.post
    attribute_calls = []

    def model(self, url, key, payload, timeout):
        if "instruments" in payload.get("messages", [{}, {}])[1].get("content", ""):
            attribute_calls.append(payload)
            import json

            assert "반도체 장비 제조 사업" in payload["messages"][1]["content"]
            return {"choices": [{"message": {"content": json.dumps({"items": [{"code": "123456",
                "dimension_ids": ["sector-01"], "reason": "인터넷 근거 기반 초기화"}]})}}]}
        return post(self, url, key, payload, timeout)

    monkeypatch.setattr(ResearchPipeline, "post", model)
    body = {"confirmed": True, "cloud_confirmed": False}
    response = client.post("/api/recommendations/123456/initialize", json=body)
    assert response.status_code == 200, response.text
    profile = client.get("/api/vectors/defaults/123456").json()["profile"]
    assert profile["status"] == "ready", profile
    assert profile["selected_dimensions"] == ["sector-01"]
    assert profile["business_context"]["source_kind"] == "llm_web_evidence"
    assert len(calls) == 2 and len(attribute_calls) == 1  # Search, business model, attribute model.
    assert client.post("/api/recommendations/123456/initialize", json=body).status_code == 200
    assert len(calls) == 2 and len(attribute_calls) == 1
    with setup[1]() as db:
        assert db.get(UserSettings, uid).value == original
        assert db.scalar(select(WatchlistItem).where(WatchlistItem.owner_id == uid)) is None


def test_one_run_failure_is_specific_and_not_automatically_repeated(business_client, setup):  # noqa: F811
    client, calls, response, uid, tid = business_client
    original = enrollment(setup, uid, tid)
    response["quote"] = "원문에 없는 문장은 거부"
    assert client.post("/api/recommendations/123456/initialize", json={"confirmed": True}).status_code == 200
    profile = client.get("/api/vectors/defaults/123456").json()["profile"]
    assert profile["status"] == "failed"
    assert profile["error"] == "BUSINESS_RESEARCH_FAILED"
    assert "사업설명 조사 중단" in profile["reason"]
    count = len(calls)
    with setup[1].begin() as db:
        db.get(StockFactorProfile, (uid, "123456")).updated_at = now() - timedelta(minutes=2)
    client.post("/api/recommendations/123456/initialize", json={"confirmed": True})
    assert len(calls) == count
    with setup[1]() as db:
        assert db.get(UserSettings, uid).value == original


def test_one_run_consent_and_owner_permissions(business_client, setup):  # noqa: F811
    client, calls, _, uid, tid = business_client
    enrollment(setup, uid, tid)
    assert client.post("/api/recommendations/123456/initialize", json={}).status_code == 422
    assert client.post("/api/recommendations/123456/initialize", json={"confirmed": False}).status_code == 422
    assert client.post("/api/recommendations/123456/initialize", json={"confirmed": True, "owner_id": uid}).status_code == 422
    with setup[1].begin() as db:
        tenant = Tenant(name="consent-other", timezone="Asia/Seoul")
        db.add(tenant)
        db.flush()
        tokens = []
        for index, (tenant_id, role) in enumerate([(tid, "trader"), (tenant.id, "trader"), (tid, "viewer")]):
            other = User(tenant_id=tenant_id, email=f"consent-{index}@example.com", active=True, role=role, code_hash="unused")
            db.add(other)
            db.flush()
            tokens.append(create_session(db, other, 24))
    for index, token in enumerate(tokens):
        with TestClient(client.app, headers={"origin": ORIGIN}) as other:
            other.cookies.set("trading_session", token)
            assert other.post("/api/recommendations/123456/initialize", json={"confirmed": True}).status_code == (403 if index == 2 else 404)
    assert calls == []
