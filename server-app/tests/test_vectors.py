import math
from datetime import date, datetime, timezone

import pytest
from conftest import EMAIL
from sqlalchemy import func, select

from nastack_trading.dimension_catalog import BY_ID
from nastack_trading.dimensions import exclude_uncollected, krx_dates
from nastack_trading.models import BrokerSnapshot, DimensionCollection, Instrument, User, WatchlistItem
from nastack_trading.vectors import (
    align_observations,
    construct,
    correlation,
    estimate_return,
    ranks,
    strength,
)


@pytest.mark.parametrize("method", ["pearson", "spearman", "kendall"])
def test_coefficients_perfect_constant_and_insufficient(method):
    x = list(range(20))
    assert correlation(x, x, method) == pytest.approx(1)
    assert correlation(x, [-value for value in x], method) == pytest.approx(-1)
    assert correlation(x, [1] * 20, method) is None
    assert correlation(x[:19], x[:19], method) is None


def test_rank_ties_and_nonlinear_algorithms():
    assert ranks([5, 5, 9, 1]) == [2.5, 2.5, 4, 1]
    x = list(range(20))
    assert correlation(x, [i * i for i in x], "spearman") == pytest.approx(1)
    assert correlation(x, [i * i for i in x], "pearson") < 0.98
    assert correlation(x, [i // 2 for i in x], "kendall") == pytest.approx(math.sqrt(180 / 190))
    assert strength(None, 5)["score"] is None
    assert strength(-0.8, 5)["label"] == "−−"
    assert strength(0.8, 9)["label"] == "+4"


def test_bar_predictions_are_lagged_and_exclude_partial():
    from nastack_trading.vectors import bar_forecasts

    sessions = krx_dates(date(2026, 7, 1), date(2026, 10, 20))
    rows = []
    price = 10000
    for i, day in enumerate(sessions):
        price *= 1 + math.sin(i * 0.4) * 0.01
        rows.append(
            {
                "date": day,
                "partial": False,
                "stock_close": price,
                "vector_score": 100 + math.sin(i * 0.4) * 10,
            }
        )
    forecasts = bar_forecasts(rows, 1, date(2026, 10, 21), date(2026, 10, 20))
    for f in forecasts:
        assert f["status"] == "estimated"
        assert f["target_date"] == "2026-10-21"
        assert f["base_date"] == "2026-10-20"
        assert f["predicted_close"] == pytest.approx(price * (1 + f["predicted_return"]))
        assert f["validation"]["samples"] == 30
    polluted = rows + [{"date": "2026-10-21", "partial": True, "stock_close": 1e50, "vector_score": 1e50}]
    assert bar_forecasts(polluted, 1, date(2026, 10, 21), date(2026, 10, 20)) == forecasts
    rows[-1]["vector_score"] = None
    assert all(
        f["status"] == "unavailable" for f in bar_forecasts(rows, 1, date(2026, 10, 21), date(2026, 10, 20))
    )


def test_bar_forecast_period_targets_and_sample_guard():
    from nastack_trading.vectors import bar_forecasts

    for horizon, base, target in [(5, "2026-10-16", "2026-10-23"), (21, "2026-09-30", "2026-10-30")]:
        rows = [{"date": base, "partial": False, "stock_close": 100, "vector_score": 100}]
        for f in bar_forecasts(rows, horizon, date(2026, 10, 21), date(2026, 10, 20)):
            assert f["base_date"] == base
            assert f["target_date"] == target
            assert f["predicted_return"] is None
            assert "표본 부족" in f["reason"]


def test_today_forecast_uses_completed_week_context_not_terminal_price():
    from nastack_trading.vectors import bar_today_comparison

    sessions = krx_dates(date(2026, 1, 1), date(2026, 10, 20))
    daily = {
        day: {"stock_close": 1000 * math.exp(i * 0.001 + math.sin(i * 0.3) * 0.02)}
        for i, day in enumerate(sessions)
    }
    rows = [
        {"date": day, "partial": False, "vector_score": 100 + math.sin(i * 0.4) * 5}
        for i, day in enumerate(sessions)
        if date.fromisoformat(day).weekday() == 4
    ]
    rows.append({"date": "2026-10-20", "partial": True, "vector_score": 1e50})
    values = bar_today_comparison(
        rows, daily, sessions, 5, date(2026, 10, 20), date(2026, 10, 21), date(2026, 10, 20), 5
    )
    for value in values:
        f = value["forecast"]
        assert f["status"] == "estimated"
        assert f["target_date"] == "2026-10-21"
        assert f["input_date"] == "2026-10-16"
        assert f["base_date"] == "2026-10-20"
        assert f["input_bars"] >= 20
        assert f["predicted_close"] == pytest.approx(
            daily["2026-10-20"]["stock_close"] * (1 + f["predicted_return"])
        )
    rows[-1]["vector_score"] = -1e50
    assert values == bar_today_comparison(
        rows, daily, sessions, 5, date(2026, 10, 20), date(2026, 10, 21), date(2026, 10, 20), 5
    )
    monthly = [row for i, row in enumerate(rows[:-1]) if i % 4 == 0]
    values = bar_today_comparison(
        monthly, daily, sessions, 21, date(2026, 10, 20), date(2026, 10, 21), date(2026, 10, 20), 5
    )
    assert all(v["forecast"]["predicted_close"] is None for v in values)
    assert all("독립 입력 봉 부족" in v["forecast"]["reason"] for v in values)


def test_foreign_alignment_next_session_without_fill():
    sessions = ["2026-01-02", "2026-01-05", "2026-01-06"]
    points = [{"date": "2026-01-02", "value": 10}, {"date": "2026-01-05", "value": None}]
    assert align_observations(points, sessions, True) == {"2026-01-05": 10}
    assert align_observations(points, sessions, False) == {"2026-01-02": 10}


def test_past_only_scaling_missing_fixed_weights_and_rate_difference():
    sessions = krx_dates(date(2026, 1, 1), date(2026, 5, 1))
    price = {day: 100 + i + math.sin(i) for i, day in enumerate(sessions)}
    factor = BY_ID["sector-01"]
    rows, _, _ = construct(sessions, price, [(factor, price)], [1], 60)
    changed = dict(price)
    changed[sessions[-1]] = 1e6
    other, _, _ = construct(sessions, price, [(factor, changed)], [1], 60)
    assert rows[:-1] == other[:-1]
    assert all(row["vector_score"] is None for row in rows[:11])
    missing = dict(price)
    del missing[sessions[30]]
    rows, _, _ = construct(sessions, price, [(factor, price), (BY_ID["sector-02"], missing)], [1], 60)
    assert rows[30]["vector_score"] is None and rows[31]["vector_score"] is None
    rates = {day: -1 + i / 100 + math.sin(i) / 10 for i, day in enumerate(sessions)}
    rows, _, definitions = construct(sessions, price, [(BY_ID["control-07"], rates)], [1], 60)
    assert any(row["vector_score"] is not None for row in rows)
    assert definitions["control-07:1"]["transform"] == "difference"


def test_calendar_close_bars_full_range_and_no_future():
    from nastack_trading.vectors import VectorRequest, horizon_analysis

    sessions = krx_dates(date(2026, 1, 1), date(2026, 10, 19))
    stock = {day: 100 + i + 5 * math.sin(i / 3) for i, day in enumerate(sessions)}
    factors = [(BY_ID["sector-01"], {day: 50 + i + 3 * math.cos(i / 4) for i, day in enumerate(sessions)})]
    request = VectorRequest(
        code="123456", start="2026-01-01", end="2026-10-19", dimensions=["sector-01"], horizons=[1, 5, 21]
    )
    groups = horizon_analysis(sessions, stock, factors, request, date(2026, 10, 19))
    assert [g["horizon"] for g in groups] == [1, 5, 21]
    for group in groups:
        horizon = group["horizon"]
        assert len(group["comparison"]) == 3
        assert group["sampling"] == "calendar_close_bars"
        assert all(p["stock_close"] == stock[p["date"]] for p in group["points"])
        assert group["comparison"][0]["samples"] == sum(not p["partial"] for p in group["points"])
        if horizon == 1:
            assert len(group["points"]) == len(sessions)
            assert group["comparison"][0]["samples"] > 60
        elif horizon == 5:
            assert len(group["points"]) < len(sessions) / 3
            assert group["points"][0]["partial"]
        else:
            assert len(group["points"]) == 10
            assert group["points"][-1]["partial"]
            assert group["comparison"][0]["samples"] == 9
            assert group["comparison"][0]["low_sample"] is True
    changed = {**stock, sessions[-1]: 1e10}
    after = horizon_analysis(sessions, changed, factors, request, date(2026, 10, 19))
    assert all(a["points"][:-1] == b["points"][:-1] for a, b in zip(groups, after))
    with pytest.raises(ValueError):
        VectorRequest(
            code="123456", start="2026-01-01", end="2026-10-19", dimensions=["sector-01"], horizons=[63]
        )


def seed_vector_data(factory):
    sessions = krx_dates(date(2026, 1, 1), date(2026, 10, 19))
    chart, points, stock, factor = [], [], 10000.0, 100.0
    for i, day in enumerate(sessions):
        change = math.sin(i * 1.7) * 0.025 + math.cos(i * 0.2) * 0.007
        stock *= 1 + change
        factor *= 1 + change * 0.7 + math.cos(i) * 0.004
        chart.append({"date": day, "close": stock})
        points.append({"date": day, "value": factor})
    chart[-1]["provisional"] = True
    with factory.begin() as db:
        user = db.scalar(select(User).where(User.email == EMAIL))
        item = Instrument(
            tenant_id=user.tenant_id,
            code="123456",
            value={"name": "합성 종목", "status": "active", "market": "KOSPI", "asset_type": "stock"},
        )
        db.add(item)
        db.flush()
        db.add(WatchlistItem(owner_id=user.id, tenant_id=user.tenant_id, instrument_id=item.id))
        db.add(
            BrokerSnapshot(
                owner_id=user.id,
                tenant_id=user.tenant_id,
                kind="market",
                symbol=item.code,
                environment="live",
                value={"chart": chart},
            )
        )
        db.add(
            DimensionCollection(
                owner_id=user.id,
                tenant_id=user.tenant_id,
                dimension_id="sector-01",
                start_date=date(2026, 1, 1),
                end_date=date(2026, 10, 19),
                status="completed",
                value={
                    "series_code": BY_ID["sector-01"]["series_code"],
                    "provider": "namuh_etf",
                    "points": points,
                },
            )
        )
    return sessions


def payload(**updates):
    return {
        "code": "123456",
        "start": "2026-01-01",
        "end": "2026-10-19",
        "dimensions": ["sector-01"],
        "horizons": [1],
        "window": 60,
        "method": "pearson",
        "levels": 5,
        **updates,
    }


def test_real_analysis_api_algorithms_provenance_and_no_mutation(admin, setup):
    sessions = seed_vector_data(setup[1])
    result = admin.post("/api/vectors/analyze", json=payload())
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["status"] == "completed"
    assert data["summary"]["samples"] == 59
    assert data["summary"]["stock_last"] == sessions[-2]
    assert data["points"][-1]["stock_return"] is None
    assert data["summary"]["correlation"] > 0.8
    assert data["dimensions"][0]["lag"] == "same_date"
    assert "NH PLUG" in data["dimensions"][0]["source"]
    other = admin.post("/api/vectors/analyze", json=payload(method="spearman")).json()
    assert other["method_label"].startswith("Spearman")
    assert other["summary"]["correlation"] != data["summary"]["correlation"]
    kendall = admin.post("/api/vectors/analyze", json=payload(method="kendall")).json()
    assert kendall["status"] == "completed"
    assert kendall["summary"]["correlation"] < other["summary"]["correlation"]
    with setup[1]() as db:
        assert db.scalar(select(func.count()).select_from(BrokerSnapshot)) == 1
        assert db.scalar(select(func.count()).select_from(DimensionCollection)) == 1


def test_missing_factors_not_silently_dropped_and_dates_validated(admin, setup):
    seed_vector_data(setup[1])
    response = admin.post("/api/vectors/analyze", json=payload(dimensions=["sector-01", "sector-02"]))
    assert response.status_code == 200
    assert response.json()["status"] == "insufficient_data"
    assert response.json()["summary"]["correlation"] is None
    for updates in [
        {"method": "invented"},
        {"dimensions": ["sector-01", "sector-01"]},
        {"dimensions": ["arbitrary"]},
        {"dimensions": []},
        {"horizons": []},
        {"end": "2099-01-01"},
        {"start": "2026-10-19", "end": "2026-01-01"},
    ]:
        assert admin.post("/api/vectors/analyze", json=payload(**updates)).status_code == 422
    with setup[1].begin() as db:
        user = db.scalar(select(User).where(User.email == EMAIL))
        exclude_uncollected(db, user, BY_ID["commodity-06"], "test")
    assert admin.post("/api/vectors/analyze", json=payload(dimensions=["commodity-06"])).status_code == 422


def test_owner_watchlist_and_snapshot_isolation(admin, setup):
    seed_vector_data(setup[1])
    other = admin.post("/api/users", json={"email": "other@example.com", "role": "trader"}).json()
    admin.post("/api/auth/logout")
    admin.post("/api/auth/login", json={"email": other["email"], "access_code": other["access_code"]})
    assert admin.post("/api/vectors/analyze", json=payload()).status_code == 404
    item = admin.get("/api/instruments?q=123456").json()["items"][0]
    admin.post("/api/watchlist", json={"instrument_id": item["id"]})
    response = admin.post("/api/vectors/analyze", json=payload())
    assert response.status_code == 200
    assert response.json()["summary"]["stock_last"] is None
    assert response.json()["status"] == "insufficient_data"


@pytest.mark.parametrize("method", ["pearson", "spearman", "kendall"])
def test_forecast_models_are_explicit_and_degenerate_inputs_unavailable(method):
    x = list(range(30))
    y = [0.001 * value - 0.01 for value in x]
    assert estimate_return(x, y, 15, method) == pytest.approx(0.005)
    assert estimate_return([1] * 30, y, 1, method) is None
    assert estimate_return(x[:10], y[:10], 5, method) is None


def test_all_methods_forecast_today_excludes_every_today_value(admin, setup, monkeypatch):
    def clock():
        return datetime(2026, 10, 20, 16, tzinfo=timezone.utc)  # Oct 21 KST

    monkeypatch.setattr("nastack_trading.vectors.now", clock)
    monkeypatch.setattr("nastack_trading.dimensions.now", clock)
    seed_vector_data(setup[1])
    with setup[1].begin() as db:
        snapshot = db.scalar(select(BrokerSnapshot))
        chart = [dict(row, provisional=False) for row in snapshot.value["chart"]]
        chart.append({"date": "2026-10-20", "close": chart[-1]["close"] * 1.01})
        snapshot.value = {"chart": chart}
        batch = db.scalar(select(DimensionCollection))
        points = [
            *batch.value["points"],
            {"date": "2026-10-20", "value": batch.value["points"][-1]["value"] * 1.01},
        ]
        batch.end_date = date(2026, 10, 21)
        batch.value = {**batch.value, "points": points}
    request = payload(end="2026-10-21")
    first = admin.post("/api/vectors/analyze", json=request)
    assert first.status_code == 200, first.text
    data = first.json()
    assert data["data_cutoff"] == "2026-10-20"
    assert data["today"] == "2026-10-21"
    assert len(data["comparison"]) == 3
    for row in data["comparison"]:
        forecast = row["forecast"]
        assert forecast["status"] == "estimated", forecast
        assert forecast["base_date"] == forecast["input_date"] == "2026-10-20"
        assert forecast["training_last"] <= "2026-10-20"
        assert forecast["samples"] >= 20
        assert forecast["predicted_close"] > 0
    assert all(row["date"] < "2026-10-21" for row in data["points"])
    with setup[1].begin() as db:
        snapshot = db.scalar(select(BrokerSnapshot))
        snapshot.value = {"chart": [*snapshot.value["chart"], {"date": "2026-10-21", "close": 1e50}]}
        batch = db.scalar(select(DimensionCollection))
        batch.value = {
            **batch.value,
            "points": [*batch.value["points"], {"date": "2026-10-21", "value": 1e50}],
        }
    after = admin.post("/api/vectors/analyze", json=request).json()
    assert after["comparison"] == data["comparison"]
    assert after["points"] == data["points"]
    historical = admin.post("/api/vectors/analyze", json=payload()).json()
    assert all(row["forecast"]["status"] == "unavailable" for row in historical["comparison"])
    assert (
        admin.post("/api/vectors/analyze", json=payload(start="2026-10-21", end="2026-10-21")).status_code
        == 422
    )


def test_forecast_does_not_target_holidays_or_use_stale_input():
    from nastack_trading.vectors import forecast_comparison

    sessions = ["2026-10-19", "2026-10-20"]
    rows = [{"date": day, "stock_close": 100, "stock_return": 0.01, "vector_score": 1} for day in sessions]
    result = forecast_comparison(
        rows, sessions, date(2026, 10, 24), date(2026, 10, 25), date(2026, 10, 24), 60, 5
    )
    assert all("거래일이 아니" in row["forecast"]["reason"] for row in result)
    rows[-1]["stock_close"] = None
    result = forecast_comparison(
        rows, sessions, date(2026, 10, 20), date(2026, 10, 21), date(2026, 10, 20), 60, 5
    )
    assert all("직전 거래일 종가" in row["forecast"]["reason"] for row in result)
