"""Read-only, owner-scoped exploratory vector correlations, never trading signals."""

import hashlib
import json
import math
import threading
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import date, time, timedelta
from statistics import mean, median, pstdev
from types import SimpleNamespace
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from .db import local_time, now
from .default_attributes import owned_instruments
from .dimension_catalog import BY_ID
from .dimensions import dates, excluded_ids, krx_dates, series_view
from .factor_profiles import save_profile
from .models import BrokerSnapshot, Instrument, User, VectorResult, VectorSelection, WatchlistItem
from .security import audit

VECTOR_CACHE_LOCKS = defaultdict(threading.Lock)

METHODS = {
    "pearson": "Pearson · 선형 동행",
    "spearman": "Spearman · 순위 동행",
    "kendall": "Kendall τ-b · 순위 일치",
}


def forecast_clock(at):
    local = at.astimezone(ZoneInfo("Asia/Seoul"))
    day = local.date()
    sessions = krx_dates(day - timedelta(days=14), day + timedelta(days=14))
    closed = str(day) in sessions and local.time() >= time(15, 30)
    calendar_cutoff = day if closed else day - timedelta(days=1)
    cutoff = next(date.fromisoformat(d) for d in reversed(sessions) if d <= str(calendar_cutoff))
    target = next((date.fromisoformat(d) for d in sessions if d > str(cutoff)), day)
    return cutoff, target


MIN_SAMPLES = 20
WARMUP = 10
FORECAST_MODELS = {
    "pearson": "전일 벡터 → 다음 날 수익률 OLS",
    "spearman": "전일 벡터 순위 회귀·수익률 분위수 역변환",
    "kendall": "전일 벡터 → 다음 날 수익률 Theil–Sen",
}


def ols(x, y, target):
    mx, my = mean(x), mean(y)
    denominator = sum((value - mx) ** 2 for value in x)
    if denominator <= 1e-15:
        return None
    slope = sum((a - mx) * (b - my) for a, b in zip(x, y)) / denominator
    return my + slope * (target - mx)


def estimate_return(x, y, target, method):
    """Explicit prediction models, not an invalid direct conversion of correlation."""
    if len(x) < MIN_SAMPLES or len(x) != len(y) or not finite(target):
        return None
    if correlation(x, y, method) is None:
        return None
    if method == "pearson":
        result = ols(x, y, target)
    elif method == "spearman":
        ordered = sorted(x)
        # Midrank empirical CDF; outside observed support is flagged by the caller.
        rank = (bisect_left(ordered, target) + bisect_right(ordered, target) + 1) / 2
        predicted_rank = ols(ranks(x), ranks(y), rank)
        if predicted_rank is None:
            return None
        index = max(0.0, min(len(y) - 1.0, predicted_rank - 1))
        ordered_y = sorted(y)
        low = int(index)
        result = ordered_y[low] + (ordered_y[min(low + 1, len(y) - 1)] - ordered_y[low]) * (index - low)
    else:
        slopes = [
            (y[i] - y[j]) / (x[i] - x[j]) for i in range(len(x)) for j in range(i) if abs(x[i] - x[j]) > 1e-12
        ]
        if not slopes:
            return None
        slope = median(slopes)
        result = median([b - slope * a for a, b in zip(x, y)]) + slope * target
    return result if finite(result) and result > -1 else None


def forecast_comparison(rows, sessions, cutoff, today, end, window, levels, max_input_lag=0):
    by_day = {row["date"]: row for row in rows}
    current_sessions = krx_dates(today - timedelta(days=20), today)
    trading_today = str(today) in current_sessions
    previous_day = next((day for day in reversed(current_sessions) if day < str(today)), None)
    previous = by_day.get(previous_day, {})
    input_day, input_row, model_lag = previous_day, previous, 1
    if max_input_lag and not finite(previous.get("vector_score")):
        preceding = [day for day in sessions if day <= previous_day]
        for lag, day in enumerate(reversed(preceding[-(max_input_lag + 1) :])):
            candidate = by_day.get(day, {})
            if finite(candidate.get("vector_score")):
                input_day, input_row, model_lag = day, candidate, lag + 1
                break
    training = []
    for index, day in enumerate(sessions):
        row = by_day.get(day)
        prior = by_day.get(sessions[index - model_lag]) if index >= model_lag else None
        if row and prior and row["stock_return"] is not None and prior["vector_score"] is not None:
            training.append({"date": day, "x": prior["vector_score"], "y": row["stock_return"]})
    window_days = set(sessions[-window:])
    training = [row for row in training if row["date"] in window_days]
    x, y = [row["x"] for row in training], [row["y"] for row in training]
    same_day = [
        row for row in rows[-window:] if row["stock_return"] is not None and row["vector_score"] is not None
    ]
    comparison = []
    for method, label in METHODS.items():
        value = correlation(
            [r["stock_return"] for r in same_day], [r["vector_score"] for r in same_day], method
        )
        predictive = correlation(x, y, method)
        reason = None
        if not trading_today:
            reason = "오늘은 한국 거래일이 아니므로 오늘 종가를 추정하지 않습니다."
        elif end < date.fromisoformat(previous_day):
            reason = "선택 기간이 직전 거래일까지 도달하지 않아 오늘을 추정할 수 없습니다."
        elif not finite(previous.get("stock_close"), True):
            reason = "직전 거래일 종가가 없습니다. 오래된 종가를 전일 종가로 대체하지 않습니다."
        elif not finite(input_row.get("vector_score")):
            reason = "직전 거래일의 전체 요인 벡터가 없습니다. 미수집 요인을 확인하세요."
        elif predictive is None:
            reason = "예측 학습 표본이 20쌍 미만이거나 상수 계열입니다."
        target = input_row.get("vector_score")
        predicted = estimate_return(x, y, target, method) if reason is None else None
        if reason is None and predicted is None:
            reason = "유효한 수익률 추정치를 계산하지 못했습니다."
        price = previous.get("stock_close", 0) * (1 + predicted) if predicted is not None else None
        if price is not None and not finite(price, True):
            predicted = price = None
            reason = "추정치가 허용 가능한 수치 범위를 벗어났습니다."
        comparison.append(
            {
                "method": method,
                "label": label,
                "correlation": value,
                "strength": strength(value, levels),
                "samples": len(same_day),
                "forecast": {
                    "target_date": str(today),
                    "data_cutoff": str(cutoff),
                    "base_date": previous_day,
                    "base_close": previous.get("stock_close"),
                    "input_date": input_day if target is not None else None,
                    "input_lag_sessions": model_lag,
                    "input_note": "최신 제공 벡터에 맞춰 동일 지연으로 재학습" if model_lag > 1 else None,
                    "input_score": target,
                    "model": FORECAST_MODELS[method],
                    "lagged_correlation": predictive,
                    "samples": len(training),
                    "training_first": training[0]["date"] if training else None,
                    "training_last": training[-1]["date"] if training else None,
                    "predicted_return": predicted,
                    "predicted_close": price,
                    "extrapolation": bool(x and finite(target) and (target < min(x) or target > max(x))),
                    "status": "estimated" if price is not None else "unavailable",
                    "reason": reason,
                },
            }
        )
    return comparison


class VectorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(pattern=r"^[0-9]{6}$")
    start: date
    end: date
    end_mode: Literal["fixed", "latest_completed"] = "fixed"
    input_policy: Literal["strict", "lagged_available"] = "strict"
    cache_only: bool = Field(default=False, exclude=True)
    dimensions: list[str] = Field(min_length=1, max_length=20)
    horizons: list[Literal[1, 5, 21]] = Field(default=[1], min_length=1, max_length=3)
    window: Literal[60, 120, 252] = 60
    method: Literal["pearson", "spearman", "kendall"] = "pearson"
    levels: Literal[5, 9] = 5

    @model_validator(mode="after")
    def unique(self):
        if len(set(self.dimensions)) != len(self.dimensions) or len(set(self.horizons)) != len(self.horizons):
            raise ValueError("중복 차원·변화 기간은 허용하지 않습니다.")
        return self


class SelectionValue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str | None = Field(default=None, pattern=r"^[0-9]{6}$")
    start: date
    end: date
    end_mode: Literal["fixed", "rolling_yesterday"] = "rolling_yesterday"
    dimensions: list[str] = Field(default_factory=list, max_length=20)
    horizons: list[Literal[1, 5, 21]] = Field(default=[1, 5, 21], max_length=3)
    window: Literal[60, 120, 252] = 60
    levels: Literal[5, 9] = 5


class SelectionSave(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=0)
    value: SelectionValue


def ranks(values):
    ordered = sorted(range(len(values)), key=lambda index: values[index])
    result = [0.0] * len(values)
    start = 0
    while start < len(ordered):
        end = start + 1
        while end < len(ordered) and values[ordered[end]] == values[ordered[start]]:
            end += 1
        for index in ordered[start:end]:
            result[index] = (start + end - 1) / 2 + 1
        start = end
    return result


def correlation(x, y, method, min_samples=MIN_SAMPLES):
    if len(x) < min_samples or len(x) != len(y):
        return None
    if method == "spearman":
        x, y = ranks(x), ranks(y)
    if method == "kendall":
        concordance = ties_x = ties_y = 0
        for i in range(len(x)):
            for j in range(i):
                dx, dy = (x[i] > x[j]) - (x[i] < x[j]), (y[i] > y[j]) - (y[i] < y[j])
                ties_x += dx == 0
                ties_y += dy == 0
                concordance += dx * dy
        pairs = len(x) * (len(x) - 1) // 2
        denominator = math.sqrt((pairs - ties_x) * (pairs - ties_y))
        return concordance / denominator if denominator else None
    mx, my = mean(x), mean(y)
    numerator = sum((a - mx) * (b - my) for a, b in zip(x, y))
    denominator = math.sqrt(sum((a - mx) ** 2 for a in x) * sum((b - my) ** 2 for b in y))
    return max(-1.0, min(1.0, numerator / denominator)) if denominator > 1e-15 else None


def strength(value, levels):
    if value is None:
        return {"score": None, "label": "표본 부족 또는 상수 계열"}
    magnitude = abs(value)
    number = sum(
        magnitude >= threshold for threshold in ([0.2, 0.6] if levels == 5 else [0.1, 0.3, 0.5, 0.7])
    )
    score = number * (1 if value >= 0 else -1)
    label = (
        {2: "++", 1: "+", 0: "0", -1: "−", -2: "−−"}.get(score)
        if levels == 5
        else f"{score:+d}"
        if score
        else "0"
    )
    return {"score": score, "label": label}


def finite(value, positive=False):
    return (
        isinstance(value, (float, int))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and (not positive or value > 0)
    )


def align_observations(points, sessions, lag):
    """No forward fill. Foreign observations move to the next KRX session only."""
    available = set(sessions)
    result = {}
    for point in sorted(points, key=lambda row: row["date"]):
        day, value = point["date"], point["value"]
        if not finite(value):
            continue
        index = bisect_right(sessions, day) if lag else -1
        effective = sessions[index] if lag and index < len(sessions) else day if not lag else None
        if effective in available:
            result[effective] = value
    return result


def construct(sessions, stock, factors, horizons, window, stock_horizon=1):
    """Fixed feature set; past-only standardization. Missing features never reweight."""
    features, raw = {}, {}
    for dimension, values in factors:
        difference = dimension["unit"] in {"%", "%p"}
        for horizon in horizons:
            key = f"{dimension['id']}:{horizon}"
            changes, scores = [], {}
            for i, day in enumerate(sessions):
                value = None
                # A multi-session return needs its two endpoint prices, not every
                # intermediate price. Do not interpolate missing endpoints.
                if i >= horizon and sessions[i - horizon] in values and day in values:
                    previous, current = values[sessions[i - horizon]], values[day]
                    if difference:
                        value = current - previous
                    elif previous > 0 and current > 0:
                        value = current / previous - 1
                if value is not None and not finite(value):
                    value = None
                history = [v for v in changes[-window:] if v is not None]
                if value is not None and len(history) >= WARMUP:
                    deviation = pstdev(history)
                    if deviation > 1e-12:
                        scores[day] = (value - mean(history)) / deviation
                    elif abs(value - mean(history)) < 1e-12:
                        scores[day] = 0.0
                changes.append(value)
            features[key] = scores
            raw[key] = {
                "dimension_id": dimension["id"],
                "name": dimension["name"],
                "horizon": horizon,
                "transform": "difference" if difference else "return",
            }
    rows = []
    for i, day in enumerate(sessions):
        stock_return = None
        if (
            i >= stock_horizon
            and finite(stock.get(day), True)
            and finite(stock.get(sessions[i - stock_horizon]), True)
        ):
            stock_return = stock[day] / stock[sessions[i - stock_horizon]] - 1
        complete = features and all(day in values for values in features.values())
        score = mean(values[day] for values in features.values()) if complete else None
        rows.append(
            {"date": day, "stock_close": stock.get(day), "stock_return": stock_return, "vector_score": score}
        )
    return rows, features, raw


def bar_forecasts(rows, horizon, today, end):
    """Strictly lagged completed-bar vector -> next-bar return, with walk-forward checks."""
    calendar = krx_dates(today - timedelta(days=70), today + timedelta(days=70))

    def key(day):
        dt = date.fromisoformat(day)
        if horizon == 1:
            return day
        if horizon == 5:
            return str(dt - timedelta(days=dt.weekday()))
        return str(dt.replace(day=1))

    groups = {}
    for day in calendar:
        groups.setdefault(key(day), []).append(day)
    # A period is completed only after its final exchange session has passed.
    completed = [days[-1] for days in groups.values() if days[-1] < str(today)]
    base_date = completed[-1] if completed else None
    future = [days[-1] for days in groups.values() if days[-1] >= str(today)]
    target_date = future[0] if future else None
    by_date = {r["date"]: r for r in rows if not r["partial"]}
    base = by_date.get(base_date, {})
    training = []
    for previous, current in zip(rows, rows[1:]):
        if (
            not previous["partial"]
            and not current["partial"]
            and current["date"] < str(today)
            and finite(previous["vector_score"])
            and finite(previous["stock_close"], True)
            and finite(current["stock_close"], True)
        ):
            training.append(
                (
                    previous["vector_score"],
                    current["stock_close"] / previous["stock_close"] - 1,
                    current["date"],
                )
            )
    x, y = [r[0] for r in training], [r[1] for r in training]
    results = []
    for method, label in METHODS.items():
        reason = None
        if not base_date or not target_date or str(end) < base_date or not base:
            reason = "최신 완료 봉이 조회 구간에 없습니다."
        elif not finite(base.get("stock_close"), True) or not finite(base.get("vector_score")):
            reason = "최신 완료 봉의 종가 또는 다차원 벡터가 결측입니다."
        elif len(training) < MIN_SAMPLES:
            reason = f"학습 표본 부족: {len(training)}쌍 / 최소 {MIN_SAMPLES}쌍"
        predicted = None if reason else estimate_return(x, y, base["vector_score"], method)
        if predicted is None and reason is None:
            reason = "상수 계열 또는 유효한 추정값 없음"
        # Expanding-window validation: each realized return is predicted using earlier pairs only.
        errors, hits = [], []
        for i in range(max(MIN_SAMPLES, len(training) - 30), len(training)):
            estimate = estimate_return(x[:i], y[:i], x[i], method)
            if estimate is not None:
                errors.append(abs(estimate - y[i]))
                hits.append((estimate > 0) == (y[i] > 0))
        results.append(
            {
                "method": method,
                "label": label,
                "base_date": base_date,
                "base_close": base.get("stock_close"),
                "target_date": target_date,
                "samples": len(training),
                "lagged_correlation": correlation(x, y, method),
                "training_first": training[0][2] if training else None,
                "training_last": training[-1][2] if training else None,
                "predicted_return": predicted,
                "predicted_close": base["stock_close"] * (1 + predicted) if predicted is not None else None,
                "model": {"pearson": "OLS", "spearman": "순위 회귀·분위수 역변환", "kendall": "Theil–Sen"}[
                    method
                ],
                "status": "unavailable" if reason else "estimated",
                "reason": reason,
                "extrapolation": bool(
                    x and finite(base.get("vector_score")) and not min(x) <= base["vector_score"] <= max(x)
                ),
                "validation": {
                    "samples": len(errors),
                    "mae": mean(errors) if errors else None,
                    "direction_accuracy": mean(hits) if hits else None,
                },
            }
        )
    return results


def bar_today_comparison(rows, daily, sessions, horizon, cutoff, today, end, levels, max_input_lag=0):
    """Forecast today's daily return from the latest *completed* bar context.

    Weekly/monthly terminal forecasts are not relabelled as today's price.
    Repeated context scores require 20 distinct completed input bars as well.
    """
    complete = [row for row in rows if not row["partial"]]
    contexts, cursor, context = [], 0, None
    for day in sessions:
        while cursor < len(complete) and complete[cursor]["date"] <= day:
            context = complete[cursor]
            cursor += 1
        contexts.append(
            {
                "date": day,
                "stock_close": daily[day].get("stock_close"),
                "stock_return": None,
                "vector_score": context.get("vector_score") if context else None,
                "context_date": context["date"] if context else None,
            }
        )
        if len(contexts) > 1:
            prior = contexts[-2]
            if finite(prior["stock_close"], True) and finite(contexts[-1]["stock_close"], True):
                contexts[-1]["stock_return"] = contexts[-1]["stock_close"] / prior["stock_close"] - 1
    effective = len(
        {
            previous["context_date"]
            for previous, current in zip(contexts, contexts[1:])
            if finite(previous["vector_score"]) and current["stock_return"] is not None
        }
    )
    comparisons = forecast_comparison(
        contexts, sessions, cutoff, today, end, len(sessions), levels, max_input_lag
    )
    for value in comparisons:
        forecast = value["forecast"]
        forecast["input_bars"] = effective
        effective_context = next((r for r in contexts if r["date"] == forecast["input_date"]), None)
        forecast["input_date"] = effective_context["context_date"] if effective_context else None
        period = {1: "일봉", 5: "주봉", 21: "월봉"}[horizon]
        forecast["model"] = (
            f"완료 {period} 벡터 → 다음 거래일 수익률 · "
            + {
                "pearson": "OLS",
                "spearman": "순위 회귀·분위수 역변환",
                "kendall": "Theil–Sen",
            }[value["method"]]
        )
        if effective < MIN_SAMPLES:
            forecast.update(
                predicted_return=None,
                predicted_close=None,
                status="unavailable",
                reason=f"독립 입력 봉 부족: {effective}개 / 최소 {MIN_SAMPLES}개 (일별 반복 입력은 봉 수로 중복 계산하지 않음)",
            )
    return comparisons


def horizon_analysis(sessions, stock, factors, payload, cutoff):
    """Calendar daily/weekly/monthly close bars, not overlapping N-day returns."""
    from calendar import monthrange

    selected = [day for day in sessions if str(payload.start) <= day <= str(cutoff)]
    calendar_sessions = krx_dates(
        payload.start.replace(day=1) - timedelta(days=7),
        cutoff.replace(day=monthrange(cutoff.year, cutoff.month)[1]),
    )
    bases = {}
    for dimension, values in factors:
        bases[dimension["id"]] = next(
            (values[d] for d in selected if d in values and finite(values[d]) and abs(values[d]) > 1e-12),
            None,
        )
    daily = {}
    for day in selected:
        complete = factors and all(
            day in values and bases[dimension["id"]] is not None for dimension, values in factors
        )
        vector = (
            mean(
                100 + 100 * (values[day] - bases[dimension["id"]]) / abs(bases[dimension["id"]])
                for dimension, values in factors
            )
            if complete
            else None
        )
        daily[day] = {"stock_close": stock.get(day), "vector_score": vector}
    results = []
    for horizon in payload.horizons:
        buckets = {}
        for day in selected:
            dt = date.fromisoformat(day)
            if horizon == 1:
                first = last = dt
            elif horizon == 5:
                first = dt - timedelta(days=dt.weekday())
                last = first + timedelta(days=4)
            else:
                first = dt.replace(day=1)
                last = dt.replace(day=monthrange(dt.year, dt.month)[1])
            buckets.setdefault((first, last), []).append(day)
        rows = []
        for (first, last), days in buckets.items():
            expected = calendar_sessions[
                bisect_left(calendar_sessions, str(first)) : bisect_right(calendar_sessions, str(last))
            ]
            if not expected:
                continue
            close_day = min(str(cutoff), expected[-1])
            observed = daily.get(close_day, {})
            partial = str(payload.start) > expected[0] or str(cutoff) < expected[-1]
            row = {
                "date": close_day,
                "period_start": str(first),
                "period_end": str(last),
                "partial": partial,
                "stock_close": observed.get("stock_close"),
                "vector_score": observed.get("vector_score"),
                "stock_return": None,
                "vector_return": None,
            }
            previous = rows[-1] if rows else None
            if previous and finite(previous["stock_close"], True) and finite(row["stock_close"], True):
                row["stock_return"] = row["stock_close"] / previous["stock_close"] - 1
            if previous and finite(previous["vector_score"], True) and finite(row["vector_score"], True):
                row["vector_return"] = row["vector_score"] / previous["vector_score"] - 1
            rows.append(row)
        paired = [
            r
            for r in rows
            if not r["partial"] and finite(r["stock_close"], True) and finite(r["vector_score"])
        ]
        changes = [
            r
            for r in rows
            if not r["partial"] and r["stock_return"] is not None and r["vector_return"] is not None
        ]
        comparison = []
        for method, label in METHODS.items():
            value = correlation(
                [r["stock_close"] for r in paired], [r["vector_score"] for r in paired], method, min_samples=3
            )
            change = correlation(
                [r["stock_return"] for r in changes],
                [r["vector_return"] for r in changes],
                method,
                min_samples=3,
            )
            comparison.append(
                {
                    "method": method,
                    "label": label,
                    "correlation": value,
                    "return_correlation": change,
                    "samples": len(paired),
                    "return_samples": len(changes),
                    "strength": strength(value, payload.levels),
                    "low_sample": len(paired) < MIN_SAMPLES,
                }
            )
        results.append(
            {
                "horizon": horizon,
                "points": rows,
                "comparison": comparison,
                "forecasts": bar_forecasts(rows, horizon, forecast_clock(now())[1], payload.end),
                "today_comparison": bar_today_comparison(
                    rows,
                    daily,
                    selected,
                    horizon,
                    cutoff,
                    forecast_clock(now())[1],
                    payload.end,
                    payload.levels,
                    3 if payload.input_policy == "lagged_available" else 0,
                ),
                "first": paired[0]["date"] if paired else None,
                "last": paired[-1]["date"] if paired else None,
                "window_sessions": len(rows),
                "sampling": "calendar_close_bars",
                "partial_bars": sum(r["partial"] for r in rows),
            }
        )
    return results


def analyze(db, user, payload, config):
    available_cutoff, today = forecast_clock(now())
    if payload.end_mode == "latest_completed":
        payload = payload.model_copy(update={"end": available_cutoff})
    dates(payload.start, payload.end, config.timezone)
    cutoff = min(payload.end, available_cutoff)
    if payload.start > cutoff:
        raise HTTPException(422, "오늘 자료는 사용하지 않습니다. 어제 이전의 분석 기간을 선택하세요.")
    if (payload.end - payload.start).days > 731:
        raise HTTPException(422, "탐색 분석은 한 번에 최대 2년 범위로 선택하세요.")
    instrument = db.scalar(
        select(Instrument)
        .join(WatchlistItem, WatchlistItem.instrument_id == Instrument.id)
        .where(
            Instrument.code == payload.code,
            Instrument.tenant_id == user.tenant_id,
            WatchlistItem.owner_id == user.id,
            WatchlistItem.tenant_id == user.tenant_id,
        )
    )
    if not instrument or instrument.value.get("status") != "active":
        held = next((item for item in owned_instruments(db, user) if item["code"] == payload.code), None)
        if not held:
            raise HTTPException(404, "현재 사용자의 활성 관심·보유종목에서 선택하세요.")
        instrument = SimpleNamespace(code=payload.code, value=held)
    excluded = excluded_ids(db, user)
    if any(key not in BY_ID or key in excluded for key in payload.dimensions):
        raise HTTPException(422, "활성 차원 목록에서만 선택할 수 있습니다.")
    snapshot = db.scalar(
        select(BrokerSnapshot)
        .where(
            BrokerSnapshot.owner_id == user.id,
            BrokerSnapshot.tenant_id == user.tenant_id,
            BrokerSnapshot.kind == "market",
            BrokerSnapshot.symbol == payload.code,
        )
        .order_by(BrokerSnapshot.created_at.desc(), BrokerSnapshot.id.desc())
        .limit(1)
    )
    start = max(date(2000, 1, 1), payload.start - timedelta(days=550))
    sessions = krx_dates(start, cutoff)
    stock = {
        row["date"]: row["close"]
        for row in (snapshot.value.get("chart", []) if snapshot else [])
        if not row.get("provisional") and finite(row.get("close"), True) and row["date"] <= str(cutoff)
    }
    for history in db.scalars(
        select(BrokerSnapshot)
        .where(
            BrokerSnapshot.owner_id == user.id,
            BrokerSnapshot.tenant_id == user.tenant_id,
            BrokerSnapshot.kind == "vector_history",
            BrokerSnapshot.symbol == payload.code,
        )
        .order_by(BrokerSnapshot.created_at, BrokerSnapshot.id)
    ):
        for row in history.value.get("chart", []):
            if row["date"] <= str(cutoff) and finite(row.get("close"), True):
                stock[row["date"]] = row["close"]
    factors, metadata = [], []
    for key in payload.dimensions:
        dimension = BY_ID[key]
        if not dimension.get("daily_analysis_eligible", True):
            raise HTTPException(409, "월간 원자료는 일별 벡터/예측 입력에 사용할 수 없습니다. 일별 차원만 선택하세요.")
        data = series_view(db, user, dimension, start, cutoff, config.timezone)
        lag = dimension.get("provider") not in {"namuh_etf", "kiwoom_index"}
        points = align_observations(data["points"], sessions, lag)
        factors.append((dimension, points))
        metadata.append(
            {
                "id": key,
                "name": dimension["name"],
                "source": dimension["source_name"]
                + (
                    " · 과거 구간 키움 ka10081 비수정 종가"
                    if "kiwoom_domestic_history" in data.get("retrieval_providers", [])
                    else ""
                ),
                "source_url": dimension["source_url"],
                "representation": dimension.get("representation"),
                "proxy_definition": dimension.get("proxy_definition"),
                "series_code": dimension["series_code"],
                "aligned_observations": len(points),
                "lag": "next_krx_session" if lag else "same_date",
                "last_observation": data["coverage"]["last"],
                "same_instrument": dimension["series_code"] == payload.code,
            }
        )
    rows, features, definitions = construct(sessions, stock, factors, payload.horizons, payload.window)
    rows = [row for row in rows if str(payload.start) <= row["date"] <= str(cutoff)]
    comparison = forecast_comparison(
        rows,
        [row["date"] for row in rows],
        cutoff,
        today,
        payload.end,
        payload.window,
        payload.levels,
        3 if payload.input_policy == "lagged_available" else 0,
    )
    for i, row in enumerate(rows):
        sample = [
            r
            for r in rows[max(0, i - payload.window + 1) : i + 1]
            if r["stock_return"] is not None and r["vector_score"] is not None
        ]
        row["correlations"] = {
            method: correlation(
                [r["stock_return"] for r in sample], [r["vector_score"] for r in sample], method
            )
            for method in METHODS
        }
        row["correlation"] = row["correlations"][payload.method]
        row["sample_count"] = len(sample)
    window_rows = rows[-payload.window :]
    paired = [
        row for row in window_rows if row["stock_return"] is not None and row["vector_score"] is not None
    ]
    result = correlation(
        [r["stock_return"] for r in paired], [r["vector_score"] for r in paired], payload.method
    )
    per_feature = []
    for key, values in features.items():
        sample = [r for r in window_rows if r["stock_return"] is not None and r["date"] in values]
        value = correlation(
            [r["stock_return"] for r in sample], [values[r["date"]] for r in sample], payload.method
        )
        per_feature.append(
            {
                **definitions[key],
                "correlation": value,
                "samples": len(sample),
                "first": sample[0]["date"] if sample else None,
                "last": sample[-1]["date"] if sample else None,
                "correlations": {
                    method: correlation(
                        [r["stock_return"] for r in sample], [values[r["date"]] for r in sample], method
                    )
                    for method in METHODS
                },
            }
        )
    return {
        "schema_version": 6,
        "horizon_results": horizon_analysis(sessions, stock, factors, payload, cutoff),
        "data_cutoff": str(cutoff),
        "today": str(today),
        "comparison": comparison,
        "status": "completed" if result is not None else "insufficient_data",
        "instrument": {"code": instrument.code, **instrument.value},
        "request": payload.model_dump(mode="json"),
        "method_label": METHODS[payload.method],
        "points": rows,
        "dimensions": metadata,
        "features": per_feature,
        "summary": {
            "correlation": result,
            "strength": strength(result, payload.levels),
            "samples": len(paired),
            "required_samples": MIN_SAMPLES,
            "window_sessions": len(window_rows),
            "missing_sessions": len(window_rows) - len(paired),
            "first": paired[0]["date"] if paired else None,
            "last": paired[-1]["date"] if paired else None,
            "stock_last": max(stock) if stock else None,
            "stock_collected_at": local_time(snapshot.created_at, config.timezone) if snapshot else None,
        },
        "construction": {
            "weights": "equal_fixed_features",
            "standardization": "past_only_rolling_zscore",
            "warmup": WARMUP,
            "stock_transform": "daily_simple_return",
            "missing": "no_fill_complete_feature_set_only",
        },
        "warnings": [
            "오늘 예상 종가는 상관계수를 직접 가격으로 바꾼 값이 아니라, 전일 벡터로 다음 거래일 수익률을 학습한 별도 모델의 탐색 추정치입니다. 검증된 예측 성과가 없습니다.",
            "탐색용 관측 관계이며 인과·예측 성과·자동매매 적합성을 의미하지 않습니다.",
            "해외 원천 관측은 다음 한국 거래일로 이동하지만 공개 지연·수정 이력은 미검증이며 point-in-time 백테스트가 아닙니다.",
            "ETF는 원지수·현물·금리와 다릅니다. 수정주가·분배금·환율·롤오버 효과는 별도 검증해야 합니다.",
            "같은 종목 또는 중복 ETF를 요인으로 선택하면 자기상관·중복 가중 효과가 있을 수 있습니다.",
            "5일·21일 중첩 변화량과 과거 표준화 점수는 자기상관을 가질 수 있어 유의확률·신뢰구간을 표시하지 않습니다.",
        ],
    }


def vectors_router(current_user, db_session, config, kiwoom_transport=None, namuh_transport=None):
    router = APIRouter()

    @router.get("/api/vectors/forecast-clock")
    def forecast_timing(user=Depends(current_user)):
        cutoff, target = forecast_clock(now())
        return {"available_cutoff": str(cutoff), "target_date": str(target), "timezone": "Asia/Seoul"}

    @router.post("/api/vectors/preview")
    def preview(payload: VectorRequest, user=Depends(current_user), db=Depends(db_session)):
        """Read-only display of a matching saved result, NOT a freshness approval."""
        if payload.code not in {item["code"] for item in owned_instruments(db, user)}:
            raise HTTPException(404, "본인의 활성 관심·보유종목을 선택하세요.")
        cutoff, target = forecast_clock(now())
        payload = payload.model_copy(
            update={
                "end": cutoff if payload.end_mode == "latest_completed" else payload.end,
                "dimensions": sorted(payload.dimensions),
                "horizons": sorted(payload.horizons),
            }
        )
        dates(payload.start, payload.end, config.timezone)
        expected = payload.model_dump(mode="json")
        excluded = excluded_ids(db, user)
        if any(key not in BY_ID or key in excluded for key in payload.dimensions):
            raise HTTPException(422, "활성 차원 목록에서만 선택할 수 있습니다.")
        rows = db.scalars(
            select(VectorResult)
            .where(
                VectorResult.owner_id == user.id,
                VectorResult.tenant_id == user.tenant_id,
                VectorResult.value["request"]["code"].as_string() == payload.code,
                VectorResult.value["today"].as_string() == str(target),
                VectorResult.value["status"].as_string() == "completed",
            )
            .order_by(VectorResult.created_at.desc(), VectorResult.id.desc())
            .limit(20)
        )
        for row in rows:
            if row.value.get("request") == expected:
                return {
                    **row.value,
                    "freshness": {**row.value.get("freshness", {}), "verification_pending": True},
                    "persistence": {
                        "id": row.id,
                        "saved": True,
                        "reused": True,
                        "preview": True,
                        "created_at": local_time(row.created_at, config.timezone),
                    },
                }
        return {"cache_hit": False}

    class HistoryRequest(BaseModel):
        code: str = Field(pattern=r"^\d{6}$")
        start: date
        end: date
        provider: Literal["namuh", "kiwoom"] = "namuh"

    @router.post("/api/vectors/stock-history")
    def stock_history(payload: HistoryRequest, user=Depends(current_user), db=Depends(db_session)):
        if user.role == "viewer":
            raise HTTPException(403, "조회 전용입니다.")
        dates(payload.start, payload.end, config.timezone)
        end = min(payload.end, forecast_clock(now())[0])
        if payload.start > end or payload.code not in {i["code"] for i in owned_instruments(db, user)}:
            raise HTTPException(422, "본인의 관심·보유종목과 유효한 과거 기간을 선택하세요.")
        snapshots = db.scalars(
            select(BrokerSnapshot)
            .where(
                BrokerSnapshot.owner_id == user.id,
                BrokerSnapshot.tenant_id == user.tenant_id,
                BrokerSnapshot.symbol == payload.code,
                BrokerSnapshot.kind.in_(["market", "vector_history"]),
            )
            .order_by(BrokerSnapshot.created_at, BrokerSnapshot.id)
        ).all()
        points = {
            p["date"]: p
            for s in snapshots
            for p in s.value.get("chart", [])
            if not p.get("provisional") and finite(p.get("close"), True)
        }
        missing = [d for d in krx_dates(payload.start, end) if d not in points]
        already_attempted = any(
            s.kind == "vector_history"
            and s.value.get("provider") == f"{payload.provider}_domestic_history"
            and s.value.get("requested_start", "9999") <= str(payload.start)
            and s.value.get("requested_end", "") >= str(end)
            for s in snapshots
        )
        if missing and not already_attempted:
            from .price_history import fetch_history, fetch_namuh_history

            fetch = fetch_history if payload.provider == "kiwoom" else fetch_namuh_history

            try:
                value = fetch(
                    db,
                    user,
                    config,
                    payload.code,
                    date.fromisoformat(missing[0]),
                    date.fromisoformat(missing[-1]),
                    kiwoom_transport if payload.provider == "kiwoom" else namuh_transport,
                )
            except HTTPException as error:
                # A failed optional backfill must not discard valid owned history.
                if not points:
                    raise
                return {
                    "mode": "cached_partial",
                    "start": str(payload.start),
                    "end": str(end),
                    "remaining": missing,
                    "chart": [points[d] for d in sorted(points)],
                    "warning": f"{payload.provider} 과거자료 보충 실패 · 저장 자료로 분석합니다: {error.detail}",
                }
            saved = BrokerSnapshot(
                owner_id=user.id,
                tenant_id=user.tenant_id,
                kind="vector_history",
                symbol=payload.code,
                environment=value["environment"],
                value={**value, "requested_start": str(payload.start), "requested_end": str(end)},
            )
            db.add(saved)
            audit(
                db,
                user,
                "vector.stock_history_synced",
                payload.code,
                {"start": str(payload.start), "end": str(end), "count": len(value["chart"])},
            )
            db.commit()
            points.update({p["date"]: p for p in value["chart"]})
        remaining = [d for d in krx_dates(payload.start, end) if d not in points]
        return {
            "mode": "cached" if not missing or already_attempted else "fetched",
            "start": str(payload.start),
            "end": str(end),
            "remaining": remaining,
            "chart": [points[d] for d in sorted(points)],
            "warning": "소급 조회 후에도 상장 전·미제공 구간이 남을 수 있으며 채우지 않습니다."
            if remaining
            else None,
        }

    def selection_view(row):
        return (
            {
                "version": row.version,
                "value": row.value,
                "updated_at": local_time(row.updated_at, config.timezone),
            }
            if row
            else {"version": 0, "value": None}
        )

    @router.get("/api/vectors/state")
    def state(user=Depends(current_user), db=Depends(db_session)):
        row = db.get(VectorSelection, user.id)
        recent = db.scalar(
            select(VectorResult)
            .where(VectorResult.owner_id == user.id, VectorResult.tenant_id == user.tenant_id)
            .order_by(VectorResult.created_at.desc(), VectorResult.id.desc())
            .limit(1)
        )
        return {
            "selection": selection_view(row),
            "latest_result": {
                "id": recent.id,
                "created_at": local_time(recent.created_at, config.timezone),
                "result": recent.value,
            }
            if recent
            else None,
        }

    @router.put("/api/vectors/selection")
    def save(payload: SelectionSave, user=Depends(current_user), db=Depends(db_session)):
        if user.role == "viewer":
            raise HTTPException(403, "조회 전용 사용자는 선택 조건을 저장할 수 없습니다.")
        db.scalar(select(User).where(User.id == user.id).with_for_update())
        row = db.get(VectorSelection, user.id)
        if payload.version != (row.version if row else 0):
            raise HTTPException(409, "다른 화면에서 조건이 변경되었습니다. 화면을 다시 열어주세요.")
        if any(key not in BY_ID for key in payload.value.dimensions):
            raise HTTPException(422, "등록된 차원만 저장하세요.")
        if row and row.value == payload.value.model_dump(mode="json"):
            return selection_view(row)
        if not row:
            row = VectorSelection(owner_id=user.id, tenant_id=user.tenant_id, version=0, value={})
            db.add(row)
        row.version += 1
        row.value = payload.value.model_dump(mode="json")
        row.updated_at = now()
        audit(db, user, "vector.selection_saved", user.id, {"version": row.version})
        db.commit()
        return selection_view(row)

    @router.get("/api/vectors/history")
    def history(user=Depends(current_user), db=Depends(db_session)):
        rows = db.scalars(
            select(VectorResult)
            .where(VectorResult.owner_id == user.id, VectorResult.tenant_id == user.tenant_id)
            .order_by(VectorResult.created_at.desc(), VectorResult.id.desc())
            .limit(20)
        ).all()
        return {
            "items": [
                {
                    "id": row.id,
                    "created_at": local_time(row.created_at, config.timezone),
                    "request": row.value["request"],
                    "summary": row.value["summary"],
                    "data_cutoff": row.value["data_cutoff"],
                }
                for row in rows
            ]
        }

    @router.get("/api/vectors/history/{result_id}")
    def saved_result(result_id: str, user=Depends(current_user), db=Depends(db_session)):
        row = db.scalar(
            select(VectorResult).where(
                VectorResult.id == result_id,
                VectorResult.owner_id == user.id,
                VectorResult.tenant_id == user.tenant_id,
            )
        )
        if not row:
            raise HTTPException(404, "저장된 분석 결과를 찾지 못했습니다.")
        return {"id": row.id, "created_at": local_time(row.created_at, config.timezone), "result": row.value}

    @router.post("/api/vectors/analyze")
    def run(payload: VectorRequest, user=Depends(current_user), db=Depends(db_session)):
        # Ordering is not a different analysis condition; share detail/tab cache.
        payload = payload.model_copy(
            update={"dimensions": sorted(payload.dimensions), "horizons": sorted(payload.horizons)}
        )
        cutoff, target = forecast_clock(now())
        effective = (
            payload.model_copy(update={"end": cutoff}) if payload.end_mode == "latest_completed" else payload
        )
        dates(effective.start, effective.end, config.timezone)
        if (effective.end - effective.start).days > 731:
            raise HTTPException(422, "탐색 분석은 한 번에 최대 2년 범위로 선택하세요.")
        if payload.code not in {item["code"] for item in owned_instruments(db, user)}:
            raise HTTPException(404, "본인의 활성 관심·보유종목을 선택하세요.")
        input_data = {
            "engine": "vector-input-cache-v2",
            "request": effective.model_dump(mode="json"),
            "target": str(target),
            "stock": {},
            "dimensions": {},
        }
        snapshots = db.scalars(
            select(BrokerSnapshot)
            .where(
                BrokerSnapshot.owner_id == user.id,
                BrokerSnapshot.tenant_id == user.tenant_id,
                BrokerSnapshot.symbol == payload.code,
                BrokerSnapshot.kind.in_(["market", "vector_history"]),
            )
            .order_by(BrokerSnapshot.created_at, BrokerSnapshot.id)
        ).all()
        for snapshot in snapshots:
            for point in snapshot.value.get("chart", []):
                if not point.get("provisional") and str(
                    max(date(2000, 1, 1), effective.start - timedelta(days=550))
                ) <= point["date"] <= str(effective.end):
                    input_data["stock"][point["date"]] = point.get("close")
        for key in payload.dimensions:
            if key in BY_ID:
                series = series_view(
                    db,
                    user,
                    BY_ID[key],
                    max(date(2000, 1, 1), effective.start - timedelta(days=550)),
                    min(effective.end, cutoff),
                    config.timezone,
                )
                input_data["dimensions"][key] = {
                    "definition": BY_ID[key],
                    "points": [{"date": p["date"], "value": p["value"]} for p in series["points"]],
                }
        input_data["excluded"] = sorted(excluded_ids(db, user))
        signature = hashlib.sha256(
            json.dumps(input_data, sort_keys=True, ensure_ascii=False, default=str).encode()
        ).hexdigest()
        from .data_collection import freshness

        input_freshness = freshness(
            db, user, payload.dimensions, effective.start, min(effective.end, cutoff), config.timezone
        )
        with VECTOR_CACHE_LOCKS[(user.tenant_id, user.id)]:
            # A signature is derived from personal snapshots, so tenant and owner
            # are part of both the lookup and its database index.
            cached = db.scalar(
                select(VectorResult).where(
                    VectorResult.owner_id == user.id,
                    VectorResult.tenant_id == user.tenant_id,
                    VectorResult.input_signature == signature,
                )
            )
            # Compatibility fallback for results created before the indexed column.
            if not cached:
                previous = db.scalars(
                    select(VectorResult)
                    .where(
                        VectorResult.owner_id == user.id,
                        VectorResult.tenant_id == user.tenant_id,
                        VectorResult.input_signature.is_(None),
                    )
                    .order_by(VectorResult.created_at.desc())
                    .limit(100)
                ).all()
                cached = next((r for r in previous if r.value.get("input_signature") == signature), None)
            if cached:
                return {
                    **cached.value,
                    "freshness": input_freshness,
                    "persistence": {
                        "saved": True,
                        "id": cached.id,
                        "created_at": local_time(cached.created_at, config.timezone),
                        "reused": True,
                    },
                }
            if payload.cache_only:
                return {"cache_hit": False}
            value = analyze(db, user, payload, config)
            value["freshness"] = input_freshness
            value["input_signature"] = signature
            if user.role == "viewer":
                return {**value, "persistence": {"saved": False, "reason": "조회 전용"}}
            fingerprint = hashlib.sha256(
                json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()
            db.scalar(select(User).where(User.id == user.id).with_for_update())
            if not db.get(VectorSelection, user.id):
                # Backward-compatible save for an already-open pre-upgrade browser.
                day = now().astimezone(ZoneInfo("Asia/Seoul")).date() - timedelta(days=1)
                draft = SelectionValue(
                    code=payload.code,
                    start=payload.start,
                    end=payload.end,
                    dimensions=payload.dimensions,
                    horizons=payload.horizons,
                    window=payload.window,
                    levels=payload.levels,
                    end_mode="rolling_yesterday" if payload.end >= day else "fixed",
                )
                db.add(
                    VectorSelection(
                        owner_id=user.id,
                        tenant_id=user.tenant_id,
                        version=1,
                        value=draft.model_dump(mode="json"),
                    )
                )
            row = db.scalar(
                select(VectorResult).where(
                    VectorResult.owner_id == user.id, VectorResult.fingerprint == fingerprint
                )
            )
            if not row:
                row = VectorResult(
                    owner_id=user.id,
                    tenant_id=user.tenant_id,
                    fingerprint=fingerprint,
                    input_signature=signature,
                    value=value,
                )
                db.add(row)
                db.flush()
                audit(db, user, "vector.result_saved", row.id, {"fingerprint": fingerprint})
            db.commit()
            return {
                **value,
                "persistence": {
                    "saved": True,
                    "id": row.id,
                    "created_at": local_time(row.created_at, config.timezone),
                    "reused": False,
                },
            }

    class AttributeSave(BaseModel):
        result_id: str = Field(min_length=1, max_length=36)
        auto_initial: bool = False

    @router.post("/api/vectors/attributes")
    def attributes(payload: AttributeSave, user=Depends(current_user), db=Depends(db_session)):
        if user.role == "viewer":
            raise HTTPException(403, "조회 전용 사용자는 속성을 저장할 수 없습니다.")
        db.scalar(select(User).where(User.id == user.id).with_for_update())
        row = db.scalar(
            select(VectorResult).where(
                VectorResult.id == payload.result_id,
                VectorResult.owner_id == user.id,
                VectorResult.tenant_id == user.tenant_id,
            )
        )
        if not row:
            raise HTTPException(404, "본인의 저장된 분석 결과를 선택하세요.")
        result = row.value
        instrument = db.scalar(
            select(Instrument)
            .join(WatchlistItem, WatchlistItem.instrument_id == Instrument.id)
            .where(
                Instrument.code == result["instrument"]["code"],
                Instrument.tenant_id == user.tenant_id,
                WatchlistItem.owner_id == user.id,
                WatchlistItem.tenant_id == user.tenant_id,
            )
        )
        if (not instrument or instrument.value.get("status") != "active") and not any(
            item["code"] == result["instrument"]["code"] for item in owned_instruments(db, user)
        ):
            raise HTTPException(409, "활성 관심·보유종목만 속성을 저장할 수 있습니다.")
        if result.get("status") != "completed" or any(
            key in excluded_ids(db, user) for key in result["request"]["dimensions"]
        ):
            raise HTTPException(409, "유효한 상관도 분석을 완료한 차원 조합만 저장하세요.")
        if payload.auto_initial:
            from .models import StockFactorProfile

            entry = db.scalar(select(BrokerSnapshot).where(
                BrokerSnapshot.owner_id == user.id, BrokerSnapshot.tenant_id == user.tenant_id,
                BrokerSnapshot.kind == "analysis_entry", BrokerSnapshot.symbol == result["instrument"]["code"]))
            if not entry:
                raise HTTPException(403, "본인의 추천종목 초기 분석에서만 자동 할당할 수 있습니다.")
            existing = db.get(StockFactorProfile, (user.id, result["instrument"]["code"]))
            if existing and existing.value.get("origin") != "llm_default":
                return existing.value
        profile = save_profile(db, user, result, row.id)
        if payload.auto_initial:
            profile.update(origin="recommendation_initial", scope="추천종목 최초 조회의 관측 상관 기반 초기 벡터",
                           rule="초기 할당만 수행 · 기존 수동 속성은 자동 교체하지 않음")
            db.get(StockFactorProfile, (user.id, profile["code"])).value = dict(profile)
        audit(
            db,
            user,
            "vector.attributes_saved",
            result["instrument"]["code"],
            {"result_id": row.id, "dimensions": result["request"]["dimensions"]},
        )
        db.commit()
        return profile

    return router
