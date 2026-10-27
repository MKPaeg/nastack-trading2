"""IMF and palm break contracts. Synthetic public responses, no live services."""

from datetime import date

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select
from conftest import CODE as LOGIN_CODE, EMAIL, ORIGIN

from nastack_trading.dimension_catalog import BY_ID
from nastack_trading.dimension_imf import CODE, fetch_iron, parse_iron
from nastack_trading.dimension_pending import PENDING
from nastack_trading.dimension_worldbank import parse_monthly
from nastack_trading.models import DimensionCollection, User
from test_dimension_worldbank import workbook


@pytest.mark.parametrize("body", [
    "observation_date,WRONG\n2026-01-01,1\n",
    "observation_date,PIORECRUSDM\n2026-01-02,1\n",
    "observation_date,PIORECRUSDM\n2026-01-01,1\n2026-01-01,2\n",
    "observation_date,PIORECRUSDM\n2026-01-01,nan\n",
    "observation_date,PIORECRUSDM\n2026-01-01,inf\n",
    "observation_date,PIORECRUSDM\n2026-01-01,0\n",
    "observation_date,PIORECRUSDM\n2026-01-01,-2\n",
    "observation_date,PIORECRUSDM\n2099-01-01,2\n",
    "observation_date,PIORECRUSDM\n2026-01-01,2,extra\n",
])
def test_iron_rejects_wrong_identity_and_malformed_values(body):
    with pytest.raises(ValueError):
        parse_iron(body.encode(), date(2026, 1, 1), date(2026, 2, 28))


def test_iron_missing_is_not_zero_or_daily_fill():
    raw = f"observation_date,{CODE}\n2026-01-01,.\n2026-02-01,100.5\n".encode()
    points = parse_iron(raw, date(2026, 1, 1), date(2026, 2, 28))
    assert len(points) == 2 and points[0]["value"] is None
    assert points[1] == {"date": "2026-02-28", "source_date": "2026-02-01", "period": "2026-02", "value": 100.5}
    with pytest.raises(HTTPException) as err:
        parse_iron(raw, date(2025, 1, 1), date(2025, 12, 31))
    assert err.value.status_code == 409


def test_iron_metadata_is_checked_before_csv():
    calls = []
    def response(request):
        calls.append(str(request.url))
        assert request.url.host == "fred.stlouisfed.org"
        return httpx.Response(200, text="Wrong index; USD per pound; Daily")
    with pytest.raises(HTTPException) as err:
        fetch_iron(BY_ID["commodity-11"], date(2026, 1, 1), date(2026, 2, 28), httpx.MockTransport(response))
    assert err.value.status_code == 502 and len(calls) == 1


def test_palm_does_not_splice_old_regime_or_report_old_period_complete():
    row = BY_ID["commodity-29"]
    # Add a valid old-regime value by changing the one-row synthetic document.
    raw = workbook(label="Palm oil", unit="($/mt)")
    points, _ = parse_monthly(raw, row, date(2020, 1, 1), date(2026, 2, 28))
    assert points[0]["date"] == "2026-01-31" and row["source_version"] == "wb-palm-2026-monthly-v1"
    with pytest.raises(HTTPException) as err:
        parse_monthly(workbook(label="Palm oil", unit="($/mt)", period="2025M12"), row, date(2020, 1, 1), date(2026, 2, 28))
    assert err.value.status_code == 409


@pytest.mark.parametrize("key", ["commodity-11", "commodity-29"])
def test_monthly_view_rejects_foreign_source_and_old_version(setup, key):
    from nastack_trading.dimensions import series_view, missing_ranges
    config, factory = setup
    with factory.begin() as db:
        user = db.scalar(select(User))
        dimension = BY_ID[key]
        for provider, version in [("wrong", dimension["source_version"]), (dimension["provider"], "old-version")]:
            db.add(DimensionCollection(owner_id=user.id, tenant_id=user.tenant_id, dimension_id=key,
                start_date=date(2026, 1, 1), end_date=date(2026, 2, 28), status="completed",
                value={"provider": provider, "series_code": dimension["series_code"], "source_version": version,
                       "points": [{"date": "2026-01-31", "value": 999}]}))
        db.flush()
        assert series_view(db, user, dimension, date(2026, 1, 1), date(2026, 2, 28), "Asia/Seoul")["points"] == []
        assert missing_ranges(db, user, dimension, date(2026, 1, 1), date(2026, 2, 28), "Asia/Seoul") == [(date(2026, 1, 1), date(2026, 2, 28))]
    from nastack_trading.app import create_app
    with TestClient(create_app(config), headers={"origin": ORIGIN}) as client:
        client.post("/api/auth/login", json={"email": EMAIL, "access_code": LOGIN_CODE})
        row = next(r for r in client.get("/api/dimensions").json()["items"] if r["id"] == key)
        assert row["source_changed"] and row["last_success_at"] is None


def test_remaining_requirements_are_public_and_disabled():
    assert len(PENDING) == 12
    assert all(not BY_ID[key]["can_collect"] for key in PENDING)
    assert all(BY_ID[key]["blocker_code"] == PENDING[key][0] for key in PENDING)
    assert all("ETF" in BY_ID[key]["reason"] for key in ["sector-41", "sector-42", "sector-43"])
    assert BY_ID["market-10"]["blocker_code"] == "storage_permission_required"
