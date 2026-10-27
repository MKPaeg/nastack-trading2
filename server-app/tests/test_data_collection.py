from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from conftest import CODE, EMAIL, ORIGIN
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_collection_pipeline import workflow_client  # noqa: F401

from nastack_trading.app import create_app
from nastack_trading.data_collection import DEFAULTS, DataCollector, charge, freshness, resources
from nastack_trading.db import now
from nastack_trading.dimension_catalog import BY_ID
from nastack_trading.models import (
    DataCollectionPolicy,
    DataCollectionTask,
    DimensionCollection,
    Tenant,
    User,
    UserSettings,
)
from nastack_trading.security import create_session


def test_collection_and_evaluation_are_independent(workflow_client):  # noqa: F811
    client, counters, control, *rest = workflow_client
    collected = client.post("/api/news/workflow/123456/refresh", json={"mode": "collect"})
    assert collected.status_code == 200, collected.text
    assert counters["plan"] == 1 and counters["search"] == 1
    assert counters["news"] == counters["stat"] == 0
    before = dict(counters)
    evaluated = client.post("/api/news/workflow/123456/refresh", json={"mode": "evaluate"})
    assert evaluated.status_code == 200, evaluated.text
    assert counters["search"] == before["search"] and counters["fetch_stat"] == before["fetch_stat"]
    assert counters["news"] > 0 and counters["stat"] > 0
    before = dict(counters)
    client.post("/api/news/workflow/123456/refresh", json={"mode": "evaluate"})
    assert counters == before


def seed(db, user, stamp=None):
    definition = BY_ID["commodity-17"]
    db.add(
        DimensionCollection(
            owner_id=user.id,
            tenant_id=user.tenant_id,
            dimension_id=definition["id"],
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 6),
            status="completed",
            created_at=stamp or now() - timedelta(days=10),
            value={
                "provider": definition.get("provider"),
                "series_code": definition["series_code"],
                "points": [{"date": "2026-01-02", "value": 10}],
            },
        )
    )


def test_news_worker_reuses_plan_without_llm(workflow_client, setup):  # noqa: F811
    from copy import deepcopy

    from nastack_trading.models import CollectionSearch, CollectionWorkflow

    client, counters, control, owner, tenant, source = workflow_client
    assert client.post("/api/news/workflow/123456/refresh", json={"mode": "collect"}).status_code == 200
    before = dict(counters)
    with setup[1].begin() as db:
        row = db.get(CollectionWorkflow, (owner, "123456"))
        value = deepcopy(row.value)
        value["collected_at"] = (now() - timedelta(hours=13)).isoformat()
        for cached in value["query_cache"].values():
            cached["checked_at"] = value["collected_at"]
        row.value = value
        policy_row = db.get(DataCollectionPolicy, owner)
        policy_row.value = {**policy_row.value, "settings": {**DEFAULTS, "news_enabled": True}}
        for search in db.scalars(select(CollectionSearch)):
            search.updated_at = now() - timedelta(hours=13)
    # The fixture injects MockTransport into this actual worker. No network/LLM.
    client.app.state.data_worker.tick()
    assert counters["search"] == before["search"] + 1
    for key in ("plan", "news", "stat", "fetch_stat"):
        assert counters[key] == before[key]
    with setup[1]() as db:
        assert db.get(CollectionWorkflow, (owner, "123456")).status == "collected"


def test_announced_monthly_release_and_missing_calendar_fallback():
    from datetime import datetime

    from nastack_trading.data_collection import KST, statistic_due

    stamp = datetime(2026, 10, 24, 9, tzinfo=KST)
    monthly = SimpleNamespace(
        series_id="EIA_WTI_MONTHLY", updated_at=stamp, value={"next_release_date": "2026-11-01"}
    )
    assert statistic_due(monthly) == datetime(2026, 11, 2, 9, tzinfo=KST)
    monthly.value = {}
    assert statistic_due(monthly) == stamp + timedelta(days=7)
    monthly.series_id = "WB_KOR_MANUFACTURING"
    assert statistic_due(monthly) == stamp + timedelta(days=30)


def test_stale_exploratory_forecast_is_not_a_live_buy_input(setup):
    from datetime import datetime

    from nastack_trading.data_collection import KST
    from nastack_trading.models import VectorResult
    from nastack_trading.trading import automatic_signal

    with setup[1]() as db:
        user = db.scalar(select(User))
        value = {
            "request": {"code": "123456"},
            "today": "2026-10-22",
            "data_cutoff": "2026-10-21",
            "freshness": {"needs_refresh": True},
            "comparison": [{"method": "pearson", "forecast": {"predicted_return": 0.1}}],
        }
        row = VectorResult(owner_id=user.id, tenant_id=user.tenant_id, fingerprint="test-stale", value=value)
        db.add(row)
        db.commit()
        plan = {"active": True, "buy_rule": "forecast", "take_profit": 5}
        at = datetime(2026, 10, 22, 10, tzinfo=KST)
        assert automatic_signal(plan, at, True, forecast=0.1, entry_allowed=True) is None
        row.value = {**value, "freshness": {"needs_refresh": False}}
        db.commit()
        assert automatic_signal(plan, at, True, forecast=0.1, entry_allowed=True) is None


def test_policy_isolation_cas_permissions_and_setting_preservation(admin, setup):
    first = admin.get("/api/data-collection").json()
    assert first["settings"] == DEFAULTS
    assert not first["settings"]["news_enabled"]
    with setup[1].begin() as db:
        a = db.scalar(select(User))
        aid, tid = a.id, a.tenant_id
        old_settings = db.get(UserSettings, aid).value.copy()
        seed(db, a)
        other_tenant = Tenant(name="other", timezone="Asia/Seoul")
        db.add(other_tenant)
        db.flush()
        identities = []
        for index, tenant in enumerate([tid, tid, other_tenant.id]):
            b = User(
                email=f"data{index}@example.com",
                tenant_id=tenant,
                role="viewer" if index == 1 else "trader",
                active=True,
                code_hash="unused",
            )
            db.add(b)
            db.flush()
            token = create_session(db, b, 24)
            identities.append((token, b.id))
    body = {**DEFAULTS, "version": first["version"], "search_daily_limit": 3, "news_hours": 1}
    assert admin.put("/api/data-collection", json=body).status_code == 200
    assert admin.put("/api/data-collection", json=body).status_code == 409
    assert len(admin.get("/api/data-collection").json()["resources"]) == 1
    with setup[1]() as db:
        assert db.get(UserSettings, aid).value == old_settings
    for index, (token, bid) in enumerate(identities):
        with TestClient(create_app(setup[0]), headers={"origin": ORIGIN}) as other:
            other.cookies.set("trading_session", token)
            result = other.get("/api/data-collection").json()
            assert result["resources"] == [] and result["settings"] == DEFAULTS
            mutation = other.put("/api/data-collection", json={**DEFAULTS, "version": 0})
            assert mutation.status_code == (403 if index == 1 else 200)
            assert (
                other.put(
                    "/api/data-collection", json={**DEFAULTS, "version": 0, "owner_id": aid}
                ).status_code
                == 422
            )
    # Logout/login restores own policy and cannot copy a foreign object's ID.
    assert admin.get("/api/data-collection").json()["settings"]["search_daily_limit"] == 3
    assert admin.get("/api/data-collection").json()["settings"]["news_hours"] == 1
    assert admin.post("/api/auth/logout").status_code == 204
    assert admin.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE}).status_code == 200
    assert admin.get("/api/data-collection").json()["settings"]["search_daily_limit"] == 3
    assert admin.get("/api/data-collection").json()["settings"]["news_hours"] == 1


@pytest.mark.parametrize("hours", [1, 3, 168])
def test_news_hourly_interval_save_and_restore(admin, hours):
    before = admin.get("/api/data-collection").json()
    response = admin.put(
        "/api/data-collection", json={**before["settings"], "version": before["version"], "news_hours": hours}
    )
    assert response.status_code == 200
    restored = admin.get("/api/data-collection").json()["settings"]
    assert restored == {**before["settings"], "news_hours": hours}


@pytest.mark.parametrize("hours", [0, -1, 169, 1.5])
def test_news_hourly_interval_rejects_out_of_range_without_mutation(admin, hours):
    before = admin.get("/api/data-collection").json()
    response = admin.put(
        "/api/data-collection", json={**before["settings"], "version": before["version"], "news_hours": hours}
    )
    assert response.status_code == 422
    assert admin.get("/api/data-collection").json()["settings"] == before["settings"]


def test_budget_persists_failure_and_denies_inactive_or_viewer(setup):
    with setup[1]() as db:
        user = db.scalar(select(User))
        db.add(
            DataCollectionPolicy(
                owner_id=user.id,
                tenant_id=user.tenant_id,
                value={"settings": {**DEFAULTS, "search_daily_limit": 1}},
            )
        )
        db.commit()
        charge(db, user, "search")
        with pytest.raises(HTTPException) as error:
            charge(db, user, "search")
        assert error.value.status_code == 429
        assert error.value.headers["X-Collection-Limit"] == "search"
        assert int(error.value.headers["Retry-After"]) > 0
        db.expire_all()
        assert db.get(DataCollectionPolicy, user.id).value["usage"]["search"] == 1
        user.role = "viewer"
        db.commit()
        with pytest.raises(HTTPException) as error:
            charge(db, user, "dimension")
        assert error.value.status_code == 403


def test_worker_independent_bounded_failure_backoff_and_restart(setup, monkeypatch):
    import nastack_trading.data_collection as module

    config, factory = setup
    calls = []
    with factory.begin() as db:
        user = db.scalar(select(User))
        owner, tenant = user.id, user.tenant_id
        seed(db, user)
        item = resources(db, user)[0]

    def failed(db, user, definition, *args):
        calls.append((user.id, user.tenant_id, definition["id"]))
        charge(db, user, "dimension")
        raise HTTPException(502, "SECRET_MUST_NOT_BE_PERSISTED")

    monkeypatch.setattr(module, "collect", failed)
    engine = factory.kw["bind"]
    worker = DataCollector(config, engine, factory)
    worker.tick()
    assert calls == [(owner, tenant, item["id"])]
    DataCollector(config, engine, factory).tick()
    assert len(calls) == 1
    with factory() as db:
        task = db.get(DataCollectionTask, (owner, item["resource"]))
        assert task.status == "failed" and task.value["error"] == "REQUEST_REJECTED_502"
        assert "SECRET" not in str(task.value)
        assert task.next_at.replace(tzinfo=None) > now().replace(tzinfo=None)
        result = freshness(
            db, db.get(User, owner), [item["id"]], date(2026, 1, 1), date(2026, 1, 6), config.timezone
        )
        assert result["needs_refresh"] and not result["network_called"]
