from datetime import datetime
from zoneinfo import ZoneInfo

from nastack_trading.vectors import forecast_clock


def test_session_cutoff_and_next_trading_day():
    zone = ZoneInfo("Asia/Seoul")
    cutoff, target = forecast_clock(datetime(2026, 10, 21, 14, tzinfo=zone))
    assert str(cutoff) == "2026-10-20" and str(target) == "2026-10-21"
    cutoff, target = forecast_clock(datetime(2026, 10, 21, 18, tzinfo=zone))
    assert str(cutoff) == "2026-10-21" and str(target) == "2026-10-22"
    cutoff, target = forecast_clock(datetime(2026, 10, 23, 18, tzinfo=zone))
    assert str(cutoff) == "2026-10-23" and str(target) == "2026-10-26"
    for day in (24, 25):
        cutoff, target = forecast_clock(datetime(2026, 10, day, 18, tzinfo=zone))
        assert str(cutoff) == "2026-10-23" and str(target) == "2026-10-26"
    cutoff, target = forecast_clock(datetime(2026, 10, 9, 18, tzinfo=zone))
    assert str(cutoff) == "2026-10-08" and str(target) == "2026-10-12"


def test_latest_completed_mode_overrides_old_browser_end(admin, setup, monkeypatch):
    from test_vectors import payload, seed_vector_data

    seed_vector_data(setup[1])
    at = datetime(2026, 10, 21, 18, tzinfo=ZoneInfo("Asia/Seoul"))
    monkeypatch.setattr("nastack_trading.vectors.now", lambda: at)
    response = admin.post("/api/vectors/analyze", json=payload(end="2026-10-20", end_mode="latest_completed"))
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["request"]["end"] == "2026-10-21"
    assert data["comparison"][0]["forecast"]["target_date"] == "2026-10-22"
    fixed = admin.post("/api/vectors/analyze", json=payload(end="2026-10-20", end_mode="fixed")).json()
    assert fixed["request"]["end"] == "2026-10-20"
