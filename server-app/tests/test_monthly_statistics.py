import csv
import io

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select

from nastack_trading.db import now
from nastack_trading.models import PersonalSource, StatisticSeries, Tenant, User
from nastack_trading.monthly_statistics import eia_points, oecd_points
from nastack_trading.security import create_session
from nastack_trading.statistics import refresh_statistics

TITLE = "Europe Brent Spot Price FOB (Dollars per Barrel)"


def eia_html(title=TITLE, cells=None):
    values = (
        cells
        if cells is not None
        else ["63.65", "", "NA", "W", "--", "-", "43.24", "0", "40.91", "40.19", "42.69", "49.99"]
    )
    return (
        f"<h1>{title}</h1><table><tr><td class='B4'>2020</td>"
        + "".join(f"<td class='B3'>{v}</td>" for v in values)
        + "</tr></table>Release Date: 10/21/2026"
    )


def oecd_csv(**overrides):
    row = dict(
        DATAFLOW="OECD.SDD.STES:DSD_STES@DF_INDSERV(4.3)",
        REF_AREA="KOR",
        FREQ="M",
        MEASURE="PRVM",
        UNIT_MEASURE="IX",
        ACTIVITY="C",
        ADJUSTMENT="Y",
        TRANSFORMATION="_Z",
        TIME_HORIZ="_Z",
        METHODOLOGY="N",
        UNIT_MULT="0",
        BASE_PER="2015",
        TIME_PERIOD="2026-08",
        OBS_VALUE="120.5",
        OBS_STATUS="P",
    )
    row.update(overrides)
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=list(row))
    writer.writeheader()
    writer.writerow(row)
    return out.getvalue()


def test_eia_missing_is_not_zero_and_unit_and_table_are_checked():
    points, release = eia_points(eia_html(), TITLE)
    assert points[0] == {"date": "2020-01-01", "value": 63.65}
    assert len(points) == 7 and points[2]["value"] == 0
    assert release == "2026-10-21"
    for content in [
        eia_html(title="Wrong product"),
        eia_html(cells=["1"] * 11),
        eia_html(cells=["NaN"] * 12),
    ]:
        with pytest.raises(ValueError):
            eia_points(content, TITLE)


@pytest.mark.parametrize(
    "field,value",
    [
        ("REF_AREA", "USA"),
        ("FREQ", "Q"),
        ("ACTIVITY", "F"),
        ("BASE_PER", "2020"),
        ("UNIT_MULT", "6"),
        ("ADJUSTMENT", "N"),
        ("OBS_VALUE", "inf"),
        ("TIME_PERIOD", "2026"),
    ],
)
def test_oecd_dimensions_units_and_month_are_strict(field, value):
    with pytest.raises(ValueError):
        oecd_points(oecd_csv(**{field: value}))
    assert oecd_points(oecd_csv()) == [{"date": "2026-08-01", "value": 120.5, "status": "P"}]


@pytest.mark.parametrize("same_tenant", [True, False])
@pytest.mark.parametrize("role", ["admin", "trader", "viewer"])
def test_monthly_owner_approval_isolation_and_persistence(setup, same_tenant, role):
    config, factory = setup
    calls = []

    def handler(request):
        calls.append(request.url.host)
        assert request.url.host == "sdmx.oecd.org"
        assert "authorization" not in request.headers
        return httpx.Response(200, text=oecd_csv(), headers={"content-type": "application/vnd.sdmx.data+csv"})

    transport = httpx.MockTransport(handler)
    with factory.begin() as db:
        original = db.scalar(select(User))
        tid = original.tenant_id
        source = PersonalSource(
            owner_id=original.id,
            tenant_id=tid,
            domain="data-explorer.oecd.org",
            value={"name": "OECD", "status": "active", "disposition": "use"},
        )
        db.add(source)
        db.add(
            StatisticSeries(
                owner_id=original.id,
                tenant_id=tid,
                series_id="OECD_KOR_MANUFACTURING",
                value={"points": [{"date": "2020-01-01", "value": 999}]},
            )
        )
        if not same_tenant:
            tenant = Tenant(name="Other", timezone="Asia/Seoul")
            db.add(tenant)
            db.flush()
            tid = tenant.id
        user = User(
            tenant_id=tid, email="monthly-other@example.com", role=role, active=True, code_hash="unused"
        )
        db.add(user)
        db.flush()
        uid = user.id
        owner = original.id
    with factory() as db:
        user = db.get(User, uid)
        with pytest.raises(HTTPException):
            refresh_statistics("OECD_KOR_MANUFACTURING", user, db, config, transport)
        assert calls == []
        if role == "viewer":
            return
        db.add(
            PersonalSource(
                owner_id=uid,
                tenant_id=tid,
                domain="data-explorer.oecd.org",
                value={"name": "OECD", "status": "active", "disposition": "use"},
            )
        )
        db.commit()
        assert refresh_statistics("OECD_KOR_MANUFACTURING", user, db, config, transport)["count"] == 1
        assert refresh_statistics("OECD_KOR_MANUFACTURING", user, db, config, transport)["cached"]
        assert len(calls) == 1
    with factory() as db:
        assert db.get(StatisticSeries, (uid, "OECD_KOR_MANUFACTURING")).value["points"][0]["value"] == 120.5
        assert db.get(StatisticSeries, (owner, "OECD_KOR_MANUFACTURING")).value["points"][0]["value"] == 999


def test_monthly_failed_fetch_preserves_values_and_inflight_revocation(setup):
    config, factory = setup
    with factory.begin() as db:
        user = db.scalar(select(User))
        uid = user.id
        tid = user.tenant_id
        s = PersonalSource(
            owner_id=uid,
            tenant_id=tid,
            domain="eia.gov",
            value={"name": "EIA", "status": "active", "disposition": "use"},
        )
        db.add(s)
        db.flush()
        sid = s.id
        old = {"points": [{"date": "2020-01-01", "value": 77}]}
        db.add(
            StatisticSeries(
                owner_id=uid,
                tenant_id=tid,
                series_id="EIA_BRENT_MONTHLY",
                value=old,
                updated_at=now().replace(year=2020),
            )
        )
    with factory() as db:
        user = db.get(User, uid)
        for transport in [
            httpx.MockTransport(lambda r: httpx.Response(429)),
            httpx.MockTransport(
                lambda r: httpx.Response(
                    200, text=eia_html("Wrong unit"), headers={"content-type": "text/html"}
                )
            ),
        ]:
            with pytest.raises(HTTPException):
                refresh_statistics("EIA_BRENT_MONTHLY", user, db, config, transport)
            db.rollback()
            assert db.get(StatisticSeries, (uid, "EIA_BRENT_MONTHLY")).value == old

        def revoke():
            s = db.get(PersonalSource, sid)
            s.value = {**s.value, "disposition": "exclude"}
            s.version += 1
            db.commit()

        with pytest.raises(HTTPException):
            refresh_statistics(
                "EIA_BRENT_MONTHLY",
                user,
                db,
                config,
                httpx.MockTransport(
                    lambda r: httpx.Response(200, text=eia_html(), headers={"content-type": "text/html"})
                ),
                checkpoint=revoke,
            )
        db.rollback()
        assert db.get(StatisticSeries, (uid, "EIA_BRENT_MONTHLY")).value == old


@pytest.mark.parametrize("same_tenant", [True, False])
@pytest.mark.parametrize("role", ["admin", "trader", "viewer"])
def test_monthly_api_account_switch_never_leaks_saved_values(admin, setup, same_tenant, role):
    with setup[1].begin() as db:
        owner = db.scalar(select(User))
        tid = owner.tenant_id
        db.add(
            StatisticSeries(
                owner_id=owner.id,
                tenant_id=tid,
                series_id="OECD_KOR_MANUFACTURING",
                value={"points": [{"date": "2026-08-01", "value": 777}]},
            )
        )
        if not same_tenant:
            tenant = Tenant(name="Other", timezone="Asia/Seoul")
            db.add(tenant)
            db.flush()
            tid = tenant.id
        other = User(
            tenant_id=tid, email="api-monthly@example.com", role=role, active=True, code_hash="unused"
        )
        db.add(other)
        db.flush()
        token = create_session(db, other, 24)
    own = admin.get("/api/statistics").json()
    assert next(s for s in own["series"] if s["id"] == "OECD_KOR_MANUFACTURING")["points"][0]["value"] == 777
    admin.cookies.set("trading_session", token)
    foreign = admin.get("/api/statistics").json()
    assert next(s for s in foreign["series"] if s["id"] == "OECD_KOR_MANUFACTURING")["points"] == []
    assert admin.post("/api/statistics/OECD_KOR_MANUFACTURING/refresh").status_code == (
        403 if role == "viewer" else 409
    )
