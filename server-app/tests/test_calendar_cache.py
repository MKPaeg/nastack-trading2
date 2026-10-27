from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest
from fastapi import HTTPException

from nastack_trading import dimensions


def test_public_calendar_built_once_per_year_not_per_batch(monkeypatch):
    dimensions._krx_year.cache_clear()
    original, calls = dimensions.xcals.get_calendar, []

    def measured(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(dimensions.xcals, "get_calendar", measured)
    for day in range(1, 20):
        dates = dimensions.krx_dates(date(2026, 1, day), date(2026, 10, 23))
        assert "2026-05-01" not in dates
    assert len(calls) == 1
    assert calls[0] == {"start": "2026-01-01", "end": "2026-12-31"}
    # A caller cannot mutate the shared public calendar through a returned list.
    dates.clear()
    assert dimensions.krx_dates(date(2026, 1, 1), date(2026, 1, 6)) == [
        "2026-01-02",
        "2026-01-05",
        "2026-01-06",
    ]


def test_cold_calendar_initialization_is_single_flight(monkeypatch):
    dimensions._krx_year.cache_clear()
    original, calls = dimensions.xcals.get_calendar, []

    def measured(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(dimensions.xcals, "get_calendar", measured)
    with ThreadPoolExecutor(max_workers=8) as pool:
        values = list(pool.map(lambda _: dimensions.krx_dates(date(2026, 1, 1), date(2026, 2, 1)), range(16)))
    assert len(calls) == 1 and all(value == values[0] for value in values)
    with pytest.raises(HTTPException):
        dimensions.krx_dates(date(2026, 2, 1), date(2026, 1, 1))
