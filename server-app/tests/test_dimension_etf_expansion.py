import json
from datetime import date, datetime, timezone

import httpx
import pytest
from conftest import EMAIL, ORIGIN
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_kiwoom import configuration, enable, login, provider

from nastack_trading.app import create_app
from nastack_trading.dimension_catalog import BY_ID
from nastack_trading.dimensions import exclude_uncollected, excluded_ids
from nastack_trading.models import DimensionCollection, DimensionExclusion, User


@pytest.mark.parametrize("problem", [None, "not_etf", "ticker", "nan", "duplicate"])
def test_us_etf_decimal_cutoff_and_identity(setup, monkeypatch, problem):
    monkeypatch.setattr(
        "nastack_trading.dimension_us_etf.now", lambda: datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
    )
    auth = provider([])

    def respond(request):
        if request.url.path == "/oauth2/token":
            return auth.handle_request(request)
        body = json.loads(request.content)
        assert body["stex_tp"] == "NY" and body["stk_cd"] == "FAN"
        if request.headers["api-id"] == "usa10100":
            return httpx.Response(
                200,
                json={
                    "return_code": 0,
                    "stk_cd": "OTHER" if problem == "ticker" else "FAN",
                    "stex_tp": "NY",
                    "isEtf": "N" if problem == "not_etf" else "Y",
                },
            )
        assert request.headers["api-id"] == "usa06012"
        assert body["strt_dt"] == "20261019"
        rows = [
            {"dt": "20261020", "cur_prc": "99"},
            {"dt": "20261019", "cur_prc": "NaN" if problem == "nan" else "12.3456"},
            {"dt": "20261016", "cur_prc": "11.1"},
        ]
        if problem == "duplicate":
            rows.append(rows[1])
        return httpx.Response(200, json={"return_code": 0, "result_list": rows})

    with TestClient(
        create_app(configuration(setup[0]), kiwoom_transport=httpx.MockTransport(respond)),
        headers={"origin": ORIGIN},
    ) as client:
        login(client)
        enable(client)
        response = client.post(
            "/api/dimensions/sector-07/collect", json={"start": "2026-10-16", "end": "2026-10-20"}
        )
        if problem:
            assert response.status_code in {409, 502}, response.text
        else:
            assert response.status_code == 200, response.text
            assert response.json()["points"][-1]["value"] == 12.3456
            assert response.json()["coverage"]["last"] == "2026-10-19"
            with setup[1]() as db:
                batch = db.scalar(select(DimensionCollection))
                assert batch.value["adjustment_verified"] is False
                assert batch.value["currency"] == "USD"


def test_exclusion_is_owner_scoped_recoverable_and_preserves_history(setup):
    with setup[1]() as db:
        user = db.scalar(select(User).where(User.email == EMAIL))
        item = BY_ID["commodity-06"]
        record = exclude_uncollected(db, user, item, "적합한 ETF 미확인")
        assert exclude_uncollected(db, user, item, "retry") is record
        db.commit()
        assert excluded_ids(db, user) == {item["id"]}
        user.role = "viewer"
        with pytest.raises(HTTPException) as error:
            exclude_uncollected(db, user, BY_ID["commodity-07"], "test")
        assert error.value.status_code == 403
        db.rollback()
    with TestClient(create_app(setup[0]), headers={"origin": ORIGIN}) as client:
        login(client)
        assert client.get("/api/dimensions").json()["total"] == len(BY_ID) - 1
        assert client.get("/api/dimensions/commodity-06/series").status_code == 410
    with setup[1]() as db:
        user = db.scalar(select(User).where(User.email == EMAIL))
        record = db.get(DimensionExclusion, (user.id, item["id"]))
        assert record.definition["name"] == "아연"
        db.delete(record)  # Restore candidate, never touching collection history.
        db.commit()
        assert excluded_ids(db, user) == set()


def test_exclusion_rejects_collected_data_and_does_not_affect_other_owner(setup):
    with setup[1]() as db:
        user = db.scalar(select(User).where(User.email == EMAIL))
        other = User(tenant_id=user.tenant_id, email="other@example.com", role="admin", code_hash="unused")
        db.add(other)
        db.flush()
        item = BY_ID["commodity-06"]
        exclude_uncollected(db, user, item, "synthetic")
        db.commit()
        assert excluded_ids(db, other) == set()
        batch = DimensionCollection(
            owner_id=other.id,
            tenant_id=other.tenant_id,
            dimension_id=item["id"],
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 2),
            status="completed",
            value={"points": [{"date": "2026-01-02", "value": 1.0}]},
        )
        db.add(batch)
        db.commit()
        with pytest.raises(HTTPException) as error:
            exclude_uncollected(db, other, item, "should reject")
        assert error.value.status_code == 409
        assert db.get(DimensionCollection, batch.id).value["points"][0]["value"] == 1.0
