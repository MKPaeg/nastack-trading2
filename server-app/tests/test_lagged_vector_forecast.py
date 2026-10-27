from datetime import date

import pytest

from nastack_trading.dimensions import krx_dates
from nastack_trading.vectors import forecast_comparison


def test_delayed_input_retrains_same_lag_and_keeps_current_base_close():
    days = krx_dates(date(2026, 5, 1), date(2026, 10, 21))
    rows = [
        {
            "date": day,
            "stock_close": 100.0,
            "stock_return": (i - 2) / 100000 if i >= 2 else None,
            "vector_score": i / 100 if i < len(days) - 1 else None,
        }
        for i, day in enumerate(days)
    ]
    args = (rows, days, date(2026, 10, 21), date(2026, 10, 22), date(2026, 10, 21), 60, 5)
    assert forecast_comparison(*args)[0]["forecast"]["predicted_return"] is None
    forecast = forecast_comparison(*args, max_input_lag=3)[0]["forecast"]
    assert forecast["input_date"] == "2026-10-20"
    assert forecast["input_lag_sessions"] == 2
    assert forecast["base_date"] == "2026-10-21" and forecast["base_close"] == 100
    assert forecast["target_date"] == "2026-10-22"
    assert forecast["predicted_return"] == pytest.approx((len(days) - 2) / 100000)
    assert rows[-1]["vector_score"] is None  # Never fill a missing current observation.
