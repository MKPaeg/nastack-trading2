"""Synthetic monthly workbook, isolated DB and mocked public network only."""

import io
import zipfile
from datetime import date
from xml.sax.saxutils import escape

import httpx
import pytest
from conftest import CODE, EMAIL, ORIGIN
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from nastack_trading.app import create_app
from nastack_trading.dimension_catalog import BY_ID
from nastack_trading.dimension_worldbank import SERIES, fetch_monthly, parse_monthly
from nastack_trading.models import DimensionCollection, Tenant, User

URL = "https://thedocs.worldbank.org/en/doc/public-test/related/CMO-Historical-Data-Monthly.xlsx"


def workbook(value="123.4", unit="($/mt)", duplicate=False, label="Zinc", period="2026M01"):
    def row(n, vals):
        return '<row r="%s">%s</row>' % (
            n,
            "".join(
                f'<c r="{col}{n}" t="inlineStr"><is><t>{escape(v)}</t></is></c>' for col, v in vals.items()
            ),
        )

    rows = [
        row(1, {"A": "Updated on October 02, 2026"}),
        row(2, {"B": label}),
        row(3, {"B": unit}),
        row(4, {"A": period, "B": value}),
    ]
    if duplicate:
        rows.append(row(5, {"A": "2026M01", "B": "125"}))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as book:
        book.writestr(
            "xl/workbook.xml",
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Mismatch Details" r:id="r1"/><sheet name="Monthly Prices" r:id="r2"/></sheets></workbook>',
        )
        book.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships><Relationship Id="r1" Target="worksheets/sheet1.xml"/><Relationship Id="r2" Target="worksheets/sheet2.xml"/></Relationships>',
        )
        book.writestr("xl/worksheets/sheet1.xml", "<wrong/>")
        book.writestr(
            "xl/worksheets/sheet2.xml",
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
            + "".join(rows)
            + "</sheetData></worksheet>",
        )
    return out.getvalue()


def transport(calls, raw=None, on_download=None):
    def reply(request):
        calls.append(str(request.url))
        assert "authorization" not in request.headers and "cookie" not in request.headers
        if request.url.host == "www.worldbank.org":
            return httpx.Response(200, text=f'<a href="{URL}">Monthly prices</a>')
        assert str(request.url) == URL
        if on_download:
            on_download()
        return httpx.Response(200, content=raw if raw is not None else workbook())

    return httpx.MockTransport(reply)


def test_monthly_no_daily_fill_and_safe_catalog():
    row = BY_ID["commodity-06"]
    points, release = parse_monthly(workbook(), row, date(2026, 1, 1), date(2026, 2, 28))
    assert points == [{"date": "2026-01-31", "period": "2026-01", "value": 123.4}]
    assert release == "2026-10-02"
    assert len(SERIES) == 11
    assert all(BY_ID[key]["manual_only"] and not BY_ID[key]["daily_analysis_eligible"] for key in SERIES)
    assert BY_ID["commodity-11"]["provider"] == "imf_monthly"


@pytest.mark.parametrize("key,other", [("commodity-31", "Coffee, Robusta"), ("commodity-33", "Rubber, TSR20 **")])
def test_new_variants_have_explicit_identity_and_reject_substitution(key, other):
    label, unit, _ = SERIES[key]
    row = BY_ID[key]
    assert ("Arabica" if key == "commodity-31" else "RSS3") in row["name"]
    points, _ = parse_monthly(workbook(label=label, unit=unit), row, date(2026, 1, 1), date(2026, 2, 28))
    assert len(points) == 1 and row["unit"] == "USD/kg"
    for raw in [workbook(label=other, unit=unit), workbook(label=label, unit="($/mt)")]:
        with pytest.raises(ValueError):
            parse_monthly(raw, row, date(2026, 1, 1), date(2026, 2, 28))


@pytest.mark.parametrize(
    "options",
    [
        {"unit": "USD/ton"},
        {"duplicate": True},
        {"label": "Lead"},
        {"value": "nan"},
        {"value": "inf"},
        {"value": "-1"},
    ],
)
def test_invalid_workbook_fails_closed(options):
    with pytest.raises(ValueError):
        parse_monthly(workbook(**options), BY_ID["commodity-06"], date(2026, 1, 1), date(2026, 2, 28))


def test_no_months_is_publication_wait_not_zero():
    with pytest.raises(HTTPException) as caught:
        parse_monthly(workbook(), BY_ID["commodity-06"], date(2026, 2, 1), date(2026, 2, 28))
    assert caught.value.status_code == 409


def test_discovery_does_not_follow_arbitrary_url():
    calls = []

    def reply(request):
        calls.append(str(request.url))
        return httpx.Response(200, text='<a href="http://127.0.0.1/private.xlsx">Monthly</a>')

    with pytest.raises(HTTPException) as caught:
        fetch_monthly(BY_ID["commodity-06"], date(2026, 1, 1), date(2026, 2, 28), httpx.MockTransport(reply))
    assert caught.value.status_code == 502 and len(calls) == 1


@pytest.mark.parametrize("key", ["commodity-06", "commodity-31", "commodity-33", "commodity-29", "commodity-11"])
def test_owner_persistence_cache_roles_tenants_and_direct_ids(setup, key):
    config, factory = setup
    calls = []
    payload = {"start": "2026-01-01", "end": "2026-02-28"}
    path = "/api/dimensions/" + key
    label, unit, _ = SERIES.get(key, ("Zinc", "($/mt)", "USD/mt"))
    raw = workbook(label=label, unit=unit)
    network = transport(calls, raw=raw)
    if key == "commodity-11":
        def reply(request):
            calls.append(str(request.url))
            assert "authorization" not in request.headers and "cookie" not in request.headers
            if request.url.path == "/series/PIORECRUSDM":
                return httpx.Response(200, text="PIORECRUSDM Global price of Iron Ore U.S. Dollars per Metric Ton Monthly International Monetary Fund")
            assert request.url.path == "/graph/fredgraph.csv"
            return httpx.Response(200, text="observation_date,PIORECRUSDM\n2026-01-01,123.4\n")
        network = httpx.MockTransport(reply)
    with TestClient(
        create_app(config, dimension_transport=network), headers={"origin": ORIGIN}
    ) as client:
        assert client.post(path + "/collect", json=payload).status_code == 401
        client.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE})
        a = client.post(path + "/collect", json=payload)
        assert a.status_code == 200, a.text
        assert a.json()["coverage"]["source_observations"] == 1
        assert a.json()["krx_points"] == a.json()["missing"] == []
        assert a.json()["calendar"]["alignment"] == "monthly_observation_only"
        assert client.post(path + "/collect", json=payload).json()["sync"]["mode"] == "cached"
        assert len(calls) == 2
        client.post("/api/auth/logout")
        client.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE})
        assert client.get(path + "/series", params=payload).json()["points"] == a.json()["points"]
        users = [
            client.post("/api/users", json={"email": f"wb-{role}@example.com", "role": role}).json()
            for role in ("viewer", "trader")
        ]
        for i, other in enumerate(users):
            client.post("/api/auth/logout")
            client.post(
                "/api/auth/login", json={"email": other["email"], "access_code": other["access_code"]}
            )
            assert client.get(path + "/series", params={**payload, "owner_id": "a"}).json()["points"] == []
            assert client.post(path + "/collect", json={**payload, "owner_id": "a"}).status_code == 422
            response = client.post(path + "/collect", json=payload)
            assert response.status_code == (403 if i == 0 else 200), response.text
    with factory() as db:
        rows = db.scalars(select(DimensionCollection)).all()
        assert len(rows) == len({r.owner_id for r in rows}) == 2
        from nastack_trading.dimensions import collect, series_view

        db.add(Tenant(id="separate-monthly-tenant", name="Monthly test", timezone="Asia/Seoul"))
        db.flush()
        cross = User(
            tenant_id="separate-monthly-tenant",
            email="cross@example.com",
            role="trader",
            active=True,
            code_hash=db.scalar(select(User)).code_hash,
        )
        db.add(cross)
        db.commit()
        assert (
            series_view(db, cross, BY_ID[key], date(2026, 1, 1), date(2026, 2, 28), "Asia/Seoul")[
                "points"
            ]
            == []
        )
        collect(db, cross, BY_ID[key], date(2026, 1, 1), date(2026, 2, 28), network, config)
        assert (
            len(
                series_view(
                    db, cross, BY_ID[key], date(2026, 1, 1), date(2026, 2, 28), "Asia/Seoul"
                )["points"]
            )
            == 1
        )
        assert len(db.scalars(select(DimensionCollection)).all()) == 3


def test_revoked_role_during_public_fetch_is_not_applied(setup):
    config, factory = setup

    def revoke():
        with factory.begin() as db:
            user = db.scalar(select(User).where(User.email == EMAIL))
            user.role = "viewer"

    with TestClient(
        create_app(config, dimension_transport=transport([], on_download=revoke)), headers={"origin": ORIGIN}
    ) as client:
        client.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE})
        assert (
            client.post(
                "/api/dimensions/commodity-06/collect", json={"start": "2026-01-01", "end": "2026-02-28"}
            ).status_code
            == 403
        )
    with factory() as db:
        assert db.scalar(select(DimensionCollection)).status == "failed"


def test_monthly_saved_sources_not_auto_scheduled(setup):
    from nastack_trading.data_collection import resources

    _, factory = setup
    with factory.begin() as db:
        user = db.scalar(select(User))
        db.add(
            DimensionCollection(
                owner_id=user.id,
                tenant_id=user.tenant_id,
                dimension_id="commodity-06",
                start_date=date(2026, 1, 1),
                end_date=date(2026, 2, 28),
                status="completed",
                value={"points": []},
            )
        )
        db.flush()
        assert not any(r.get("id") == "commodity-06" for r in resources(db, user))


@pytest.mark.parametrize("key", ["commodity-06", "commodity-31", "commodity-33", "commodity-29", "commodity-11"])
def test_monthly_is_rejected_by_daily_analysis_server(setup, monkeypatch, key):
    from nastack_trading import vectors

    config, factory = setup
    monkeypatch.setattr(vectors, "owned_instruments", lambda *args: [{"code": "005930"}])
    payload = vectors.VectorRequest(
        code="005930", start=date(2026, 1, 1), end=date(2026, 2, 28), dimensions=[key]
    )
    with factory() as db:
        user = db.scalar(select(User))
        with pytest.raises(HTTPException) as caught:
            vectors.analyze(db, user, payload, config)
        assert caught.value.status_code == 409
        assert "월간" in caught.value.detail
