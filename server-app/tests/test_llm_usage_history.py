"""Persist numeric LLM use without crossing user or tenant boundaries."""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from nastack_trading import model_pricing
from nastack_trading.app import create_app
from nastack_trading.llm_usage import begin_call, finish_call, tracked_usage, usage_category, usage_scope
from nastack_trading.llm_usage_history import period_bounds, save_usage, usage_report
from nastack_trading.model_pricing import ModelPrice
from nastack_trading.models import LLMUsageRecord, Tenant, User
from nastack_trading.security import create_session


def _reported(model="gpt-6.1-sol"):
    row = begin_call("openai", {"model": model})
    finish_call(row, {"usage": {"input_tokens": 1000, "output_tokens": 100, "total_tokens": 1100,
                                "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0}}})
    return row


def test_authenticated_http_call_is_saved_and_periods_are_seoul_calendar(setup, monkeypatch):
    config, factory = setup

    def price(model, day):
        return ModelPrice(model, 2, .1, 2.5, 10, day, model_pricing.MODEL_SOURCE + model)

    monkeypatch.setattr(model_pricing, "fetch_model_price", price)
    app = create_app(config)

    @app.post("/api/synthetic-history")
    def synthetic():
        _reported()
        return {"ok": True}

    with TestClient(app, headers={"origin": "http://testserver"}) as client:
        assert client.post("/api/auth/login", json={"email": "admin@example.com",
                                                    "access_code": "test-admin-code-only-123456"}).status_code == 200
        response = client.post("/api/synthetic-history")
        assert response.status_code == 200
        assert json.loads(response.headers["X-LLM-Usage"])["total_tokens"] == 1100
        today = datetime.now(ZoneInfo("Asia/Seoul")).date()
        for period in ("day", "week", "month", "year"):
            report = client.get(f"/api/llm-usage?period={period}&anchor={today}")
            assert report.status_code == 200
            data = report.json()
            assert data["summary"]["calls"] == 1
            assert data["summary"]["total_tokens"] == 1100
            assert data["summary"]["estimated_usd"] == "0.0030000000"
            assert data["by_model"][0]["model"] == "gpt-6.1-sol"
            assert data["recent"][0]["category"] == "evaluation"
            assert data["recent"][0]["pricing_checked_on"] == str(today)
        assert client.get(f"/api/llm-usage?period=day&anchor={today}&category=collection").json()["summary"]["calls"] == 0
        assert client.get("/api/llm-usage?period=decade").status_code == 422
        assert client.get("/api/llm-usage?category=trading").status_code == 422
        assert client.post("/api/auth/logout").status_code == 204
        assert client.get("/api/llm-usage").status_code == 401
        assert client.post("/api/auth/login", json={"email": "admin@example.com",
                                                    "access_code": "test-admin-code-only-123456"}).status_code == 200
        assert client.get(f"/api/llm-usage?period=day&anchor={today}").json()["summary"]["calls"] == 1
    with factory() as db:
        records = db.query(LLMUsageRecord).all()
        assert len(records) == 1
        assert records[0].owner_id and records[0].tenant_id
        assert records[0].model == "gpt-6.1-sol"


def test_collection_evaluation_and_unknown_cost_are_kept_separate(setup, monkeypatch):
    _, factory = setup
    monkeypatch.setattr(model_pricing, "fetch_model_price", lambda model, day: ModelPrice(
        model, 2, .1, 2.5, 10, day, model_pricing.MODEL_SOURCE + model))
    with factory() as db:
        owner = db.query(User).filter_by(email="admin@example.com").one()
        owner_id, tenant_id = owner.id, owner.tenant_id
    with usage_scope() as ledger:
        with usage_category("collection"):
            _reported()
        with usage_category("evaluation"):
            local = begin_call("local_model", {"model": "owned-vllm"})
            finish_call(local, {"usage": {"input_tokens": 20, "output_tokens": 5, "total_tokens": 25}})
    assert save_usage(factory, owner_id, tenant_id, ledger) == 2
    today = datetime.now(ZoneInfo("Asia/Seoul")).date()
    with factory() as db:
        owner = db.get(User, owner_id)
        report = usage_report(db, owner, "day", today, None, None)
        assert report["summary"]["calls"] == 2
        assert report["summary"]["unknown_cost_calls"] == 1
        assert report["summary"]["estimated_usd"] == "0.0030000000"
        assert {item["category"] for item in report["by_category"]} == {"collection", "evaluation"}
        assert usage_report(db, owner, "day", today, "owned-vllm", None)["summary"]["calls"] == 1
        assert usage_report(db, owner, "day", today, None, "collection")["summary"]["calls"] == 1


def test_same_tenant_and_other_tenant_cannot_see_another_users_history(setup):
    config, factory = setup
    with factory() as db:
        owner = db.query(User).filter_by(email="admin@example.com").one()
        tenant = Tenant(name="Other", timezone="Asia/Seoul")
        db.add(tenant)
        db.flush()
        same = User(tenant_id=owner.tenant_id, email="same@example.com", role="viewer", code_hash="x")
        other = User(tenant_id=tenant.id, email="other@example.com", role="trader", code_hash="x")
        db.add_all([same, other])
        db.commit()
        owner_id, tenant_id = owner.id, owner.tenant_id
        same_id, other_id = same.id, other.id
    with usage_scope() as ledger:
        begin_call("local_model", {"model": "private-model"})
    assert save_usage(factory, owner_id, tenant_id, ledger) == 1
    assert save_usage(factory, owner_id, "wrong-tenant", ledger) == 0
    today = datetime.now(ZoneInfo("Asia/Seoul")).date()
    with factory() as db:
        assert usage_report(db, db.get(User, owner_id), "day", today, None, None)["summary"]["calls"] == 1
        assert usage_report(db, db.get(User, same_id), "day", today, None, None)["summary"]["calls"] == 0
        assert usage_report(db, db.get(User, other_id), "day", today, None, None)["summary"]["calls"] == 0
        same_token = create_session(db, db.get(User, same_id), 24)
        other_token = create_session(db, db.get(User, other_id), 24)
        db.commit()
    for token in (same_token, other_token):
        with TestClient(create_app(config), cookies={"trading_session": token}) as client:
            response = client.get("/api/llm-usage?period=day")
            assert response.status_code == 200
            assert response.json()["summary"]["calls"] == 0


def test_calendar_bounds_do_not_mix_months_or_years():
    day = datetime(2026, 12, 31).date()
    assert period_bounds("week", day) == (datetime(2026, 12, 28).date(), datetime(2027, 1, 3).date())
    assert period_bounds("month", day) == (datetime(2026, 12, 1).date(), day)
    assert period_bounds("year", day) == (datetime(2026, 1, 1).date(), day)


def test_background_task_scope_persists_selected_model_and_type(setup):
    _, factory = setup
    with factory() as db:
        owner = db.query(User).filter_by(email="admin@example.com").one()
        owner_id = owner.id

    @tracked_usage
    def synthetic_background(factory, config, owner_id):
        with usage_category("collection"):
            row = begin_call("local_model", {"model": "background-model"})
            finish_call(row, {"usage": {"input_tokens": 8, "output_tokens": 2, "total_tokens": 10}})

    synthetic_background(factory, None, owner_id)
    with factory() as db:
        records = db.query(LLMUsageRecord).filter_by(owner_id=owner_id).all()
        assert len(records) == 1
        assert records[0].model == "background-model"
        assert records[0].category == "collection"
        assert records[0].total_tokens == 10
        assert records[0].estimated_usd is None
