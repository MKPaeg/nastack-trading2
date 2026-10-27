import json
import re
from datetime import date, timedelta

import httpx
import pytest
from conftest import CODE, EMAIL, ORIGIN
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from nastack_trading.app import create_app
from nastack_trading.config import ROOT
from nastack_trading.db import now
from nastack_trading.dimension_catalog import DIMENSIONS
from nastack_trading.dimensions import krx_dates, parse_csv
from nastack_trading.models import DimensionCollection


def test_valid_csv_without_new_observations_is_not_a_provider_502():
    with pytest.raises(HTTPException) as caught:
        parse_csv(b"observation_date,DEXKOUS\n2026-10-16,1400\n", "DEXKOUS", date(2026,10,17), date(2026,10,26))
    assert caught.value.status_code == 409
    assert caught.value.headers["X-Collection-State"] == "not_published"
    assert "2026-10-16" in caught.value.detail


@pytest.mark.parametrize("at,expected", [("2026-10-26T11:00:00+00:00", date(2026, 10, 23)),
                                       ("2026-10-26T21:00:00+00:00", date(2026, 10, 26)),
                                       ("2026-12-26T11:00:00+00:00", date(2026, 12, 24))])
def test_us_etf_missing_ranges_accept_weekend_and_holiday_cutoff(monkeypatch, at, expected):
    from datetime import datetime

    from nastack_trading import dimensions

    monkeypatch.setattr(dimensions, "now", lambda: datetime.fromisoformat(at))
    monkeypatch.setattr(dimensions, "scoped", lambda *args: [])
    result = dimensions.missing_ranges(None, None, {"id": "us-etf", "provider": "kiwoom_us_etf"},
                                       date(2026, 10, 1), date(2026, 12, 31), "Asia/Seoul")
    assert result == [(date(2026, 10, 1), expected)]


def transport(failing=False):
    def respond(request):
        assert request.url.host == "fred.stlouisfed.org"
        assert request.url.path == "/graph/fredgraph.csv"
        if failing:
            return httpx.Response(200, text="<html>Unavailable</html>")
        code = request.url.params["id"]
        return httpx.Response(
            200, text=f"observation_date,{code}\n2026-01-02,10.5\n2026-01-05,.\n2026-01-06,12\n"
        )

    return httpx.MockTransport(respond)


def test_catalog_matches_frontend_and_preserves_existing_dimension_ids():
    source = (ROOT / "app.js").read_text()
    block = source.split("const vectorFactorGroups = [", 1)[1].split("const vectorDraft", 1)[0]
    entries = re.findall(r'\["(sector|commodity|market|control)", "[^"]+", (\[[^\n]+\])\]', block)
    frontend = {(family, name) for family, names in entries for name in json.loads(names)}
    assert len(DIMENSIONS) == len(frontend) == 114
    assert {(item["family"], item["name"]) for item in DIMENSIONS} == frontend
    assert len({item["id"] for item in DIMENSIONS}) == 114
    assert sum(item["can_collect"] for item in DIMENSIONS) == 102
    assert sum(item["assessment"] == "license_required" for item in DIMENSIONS) == 0
    proxies = [item for item in DIMENSIONS if item.get("representation") == "etf_proxy"]
    assert len(proxies) == 72
    assert next(d for d in DIMENSIONS if d["id"] == "sector-33")["name"] == "리츠"
    new = [d for d in DIMENSIONS if d["id"] in {"sector-34", "sector-35", "sector-36"}]
    assert len(new) == 3
    assert {d["series_code"] for d in new} == {"455850", "475300", "475310"}
    assert all(
        d["representation"] == "etf_proxy" and "가격이나 물량이 아님" in d["proxy_definition"] for d in new
    )
    assert all(
        "ETF" in item["source_name"] and item["unit"] in {"KRW/ETF 1좌", "USD/ETF 1좌"} for item in proxies
    )
    assert all(
        "레버리지" not in item["instrument_name"] and "인버스" not in item["instrument_name"]
        for item in proxies
    )


def test_krx_calendar_excludes_exchange_holidays():
    days = krx_dates(date(2026, 1, 1), date(2026, 1, 6))
    assert days == ["2026-01-02", "2026-01-05", "2026-01-06"]
    assert "2026-05-01" not in krx_dates(date(2026, 4, 30), date(2026, 5, 4))


@pytest.mark.parametrize(
    "raw",
    [
        b"<html>error</html>",
        b"DATE,X\n2026-01-02,nan",
        b"DATE,X\n2026-01-02,.",
        b"DATE,X\n2026-01-02,1\n2026-01-02,2",
    ],
)
def test_parser_does_not_turn_bad_or_missing_data_into_zero(raw):
    with pytest.raises(HTTPException):
        parse_csv(raw, "X", date(2026, 1, 1), date(2026, 1, 6))


def test_collection_persistence_coverage_isolation_and_permissions(setup):
    config, factory = setup
    with TestClient(
        create_app(config, dimension_transport=transport()), headers={"origin": ORIGIN}
    ) as client:
        assert client.get("/api/dimensions").status_code == 401
        assert client.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE}).status_code == 200
        catalog = client.get("/api/dimensions").json()
        assert catalog["total"] == 114 and catalog["collectable"] == 101 and catalog["collected"] == 0
        payload = {"start": "2026-01-01", "end": "2026-01-06"}
        path = "/api/dimensions/control-07"
        empty = client.get(path + "/series", params=payload).json()
        assert empty["points"] == [] and empty["coverage"]["ratio"] == 0
        collected = client.post(path + "/collect", json=payload)
        assert collected.status_code == 200, collected.text
        value = collected.json()
        assert value["coverage"]["source_observations"] == 2
        assert value["coverage"]["krx_sessions"] == 3
        assert value["coverage"]["missing"] == 1
        assert value["missing"] == [{"date": "2026-01-05", "reason": "원천 결측"}]
        assert value["points"][1]["value"] is None
        assert value["calendar"]["point_in_time_verified"] is False
        assert client.get(path + "/series", params=payload).json()["points"] == value["points"]
        assert client.post(path + "/collect", json=payload).json()["sync"]["mode"] == "cached"
        assert client.post("/api/dimensions/market-10/collect", json=payload).status_code == 409
        assert client.post("/api/dimensions/commodity-12/collect", json=payload).status_code == 409
        assert client.get("/api/dimensions/arbitrary-url/series", params=payload).status_code == 404
        assert (
            client.get(path + "/series", params={"start": "2026-01-06", "end": "2026-01-01"}).status_code
            == 422
        )
        assert (
            client.get(path + "/series", params={"start": "2026-01-01", "end": "2099-01-01"}).status_code
            == 422
        )
        assert client.post(path + "/collect", json={**payload, "url": "http://127.0.0.1"}).status_code == 422
        subset = client.get(path + "/series", params={"start": "2026-01-06", "end": "2026-01-06"}).json()
        assert len(subset["points"]) == subset["coverage"]["matched"] == 1
        other = client.post(
            "/api/users", json={"email": "dimension-viewer@example.com", "role": "viewer"}
        ).json()
        client.post("/api/auth/logout")
        assert (
            client.post(
                "/api/auth/login", json={"email": other["email"], "access_code": other["access_code"]}
            ).status_code
            == 200
        )
        assert client.get(path + "/series", params=payload).json()["points"] == []
        assert client.get("/api/dimensions").json()["collected"] == 0
        assert client.post(path + "/collect", json=payload).status_code == 403
    with factory() as db:
        batch = db.scalar(select(DimensionCollection))
        assert batch.status == "completed" and len(batch.value["sha256"]) == 64


def test_failed_provider_preserves_previous_success(setup):
    config, factory = setup
    payload = {"start": "2026-01-01", "end": "2026-01-06"}
    with TestClient(
        create_app(config, dimension_transport=transport()), headers={"origin": ORIGIN}
    ) as client:
        client.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE})
        assert client.post("/api/dimensions/control-07/collect", json=payload).status_code == 200
    with factory.begin() as db:
        db.execute(update(DimensionCollection).values(created_at=now() - timedelta(minutes=1)))
    with TestClient(
        create_app(config, dimension_transport=transport(failing=True)), headers={"origin": ORIGIN}
    ) as client:
        client.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE})
        assert (
            client.post("/api/dimensions/control-07/collect", json={**payload, "force": True}).status_code
            == 502
        )
        result = client.get("/api/dimensions/control-07/series", params=payload).json()
        assert result["coverage"]["source_observations"] == 2
        assert result["last_attempt"]["status"] == "failed"
        assert result["last_success_at"]
