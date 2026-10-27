import json
from datetime import date

import httpx
from conftest import ORIGIN
from fastapi.testclient import TestClient
from test_kiwoom import login
from test_market import configured
from test_vectors import seed_vector_data

from nastack_trading.app import create_app


def test_owned_history_backfill_persists_and_reuses_cache(setup):
    seed_vector_data(setup[1])
    config = configured(setup[0])
    calls = []
    failing = [False]

    def respond(request):
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": "synthetic-token", "expires_in": 86400})
        assert request.url.path == "/krstock/quote/v1/currentDaily"
        body = json.loads(request.content)
        assert body["Input_0"]["view_main_yn"] == "Y"
        calls.append(body)
        if failing[0]:
            return httpx.Response(503, json={})
        return httpx.Response(
            200,
            json={
                "Output_0": [
                    {"bsop_date": "25/01/03", "stck_clpr": "101"},
                    {"bsop_date": "25/01/02", "stck_clpr": "100"},
                ],
            },
        )

    with TestClient(
        create_app(config, broker_transport=httpx.MockTransport(respond)), headers={"origin": ORIGIN}
    ) as client:
        login(client)
        saved = client.put(
            "/api/user/brokers/namuh", json={"version": 0, "enabled": True, "environment": "live"}
        )
        assert saved.status_code == 200
        checked = client.post("/api/user/brokers/namuh/verify", json={"version": saved.json()["version"]})
        assert checked.status_code == 200, checked.text
        request = {"code": "123456", "start": "2025-01-01", "end": "2025-01-03"}
        result = client.post("/api/vectors/stock-history", json=request)
        assert result.status_code == 200, result.text
        assert result.json()["remaining"] == []
        assert result.json()["chart"][0]["date"] == "2025-01-02"
        assert client.post("/api/vectors/stock-history", json=request).json()["mode"] == "cached"
        assert len(calls) == 1

        failing[0] = True
        partial = client.post("/api/vectors/stock-history", json={**request, "start": "2024-01-01"})
        assert partial.status_code == 200, partial.text
        assert partial.json()["mode"] == "cached_partial"
        assert "저장 자료로 분석" in partial.json()["warning"]
        assert partial.json()["chart"]
        assert (
            client.post("/api/vectors/stock-history", json={**request, "code": "999999"}).status_code == 422
        )
        assert len(calls) == 2


def test_truncated_dimension_history_does_not_mark_older_dates_covered(admin, setup):
    from sqlalchemy import select

    from nastack_trading.dimension_catalog import BY_ID
    from nastack_trading.dimensions import missing_ranges
    from nastack_trading.models import DimensionCollection, User

    with setup[1]() as db:
        user = db.scalar(select(User))
        definition = BY_ID["sector-01"]
        db.add(
            DimensionCollection(
                owner_id=user.id,
                tenant_id=user.tenant_id,
                dimension_id="sector-01",
                start_date=date(2025, 1, 1),
                end_date=date(2026, 1, 6),
                status="completed",
                value={
                    "series_code": definition["series_code"],
                    "provider": "namuh_etf",
                    "points": [{"date": "2026-01-02", "value": 100}, {"date": "2026-01-06", "value": 101}],
                },
            )
        )
        db.commit()
        ranges = missing_ranges(db, user, definition, date(2025, 1, 1), date(2026, 1, 6), "Asia/Seoul")
        assert ranges[0][0] == date(2025, 1, 1)
        assert ranges[0][1] == date(2026, 1, 1)
