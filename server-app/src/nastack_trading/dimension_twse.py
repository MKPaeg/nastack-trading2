"""Fixed public TAIEX daily close, not a Taiwan ETF or interpolated series.

Monthly history is the official TWSE historical report underlying its OpenAPI.
No user/model URL, credential, cache, order, or cross-owner stored observations.
"""
import hashlib
import json
import math
import re
import time
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from .db import now
from .official_daily import read_public

HISTORY = "https://www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_HIST"
OPENAPI = "https://openapi.twse.com.tw/v1/indicesReport/MI_5MINS_HIST"
FIELDS = ["日期", "開盤指數", "最高指數", "最低指數", "收盤指數"]


def apply_twse(row):
    if row["id"] != "market-15":
        return
    row.update(
        provider="twse_taiex", series_code="TAIEX", instrument_name="發行量加權股價指數",
        representation="original_series", source_name="TWSE · TAIEX 원지수 일별 종가",
        source_url=OPENAPI, source_version="twse-taiex-close-v1",
        unit="지수 포인트", frequency="대만 거래일", can_collect=True,
        assessment="collectable", assessment_date="2026-10-27",
        access_method="TWSE 공식 월별 과거 보고서 · 공개 데이터 · 인증키 불필요",
        license="TWSE 이용약관 및 Taiwan Government Data Open License 1.0 · 출처 표기",
        daily_feasibility="원지수 월별 일봉 연결 · 대만 당일 14:00 이전 봉 제외",
        reason="ETF 006204가 아닌 TAIEX 원지수 종가입니다. 공식 OpenAPI와 월별 이력의 겹치는 15개 종가 일치를 확인했습니다. "
               "대만 거래일·관측일 기준이며 한국 휴장과 다릅니다. 결측·비공개 값을 추정하지 않고 기존 다른 계열 이력과 섞지 않습니다.",
        calendar_note="대만 원천 관측일을 보존하고 한국 거래일과 날짜 교집합만 표시. 장중 봉 제외·발표시각 PIT 미검증.",
    )


def completed_cutoff(end):
    local = now().astimezone(ZoneInfo("Asia/Taipei"))
    # TWSE regular close is 13:30. Allow a conservative publication buffer.
    limit = local.date() - timedelta(days=1) if (local.hour, local.minute) < (14, 0) else local.date()
    return min(end, limit)


def months(start, end):
    result, cursor = [], start.replace(day=1)
    while cursor <= end:
        result.append(cursor)
        cursor = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
    if len(result) > 61:
        raise HTTPException(422, "TWSE 일별 자료는 최대 5년 범위로 수집하세요.")
    return result


def monthly_points(raw, month, start, cutoff):
    payload = json.loads(raw)
    if payload.get("stat") == "很抱歉，沒有符合條件的資料!":
        return []
    if (payload.get("stat") != "OK" or payload.get("date") != month.strftime("%Y%m%d")
            or payload.get("title") != f"{month.year - 1911}年{month.month:02d}月 發行量加權股價指數歷史資料"
            or payload.get("fields") != FIELDS):
        raise ValueError("Wrong report, index, month or fields")
    rows = payload.get("data")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 31:
        raise ValueError("Wrong row count")
    points, dates_seen = [], set()
    for row in rows:
        if not isinstance(row, list) or len(row) != 5:
            raise ValueError("Wrong daily row")
        match = re.fullmatch(r"(\d{3})/(\d{2})/(\d{2})", row[0])
        if not match:
            raise ValueError("Invalid ROC date")
        day = date(int(match[1]) + 1911, int(match[2]), int(match[3]))
        if (day.year, day.month) != (month.year, month.month) or day in dates_seen:
            raise ValueError("Wrong month or duplicate date")
        dates_seen.add(day)
        numbers = []
        for value in row[1:]:
            if not isinstance(value, str) or not re.fullmatch(r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?", value):
                raise ValueError("Invalid index point")
            number = float(value.replace(",", ""))
            if not math.isfinite(number) or number <= 0:
                raise ValueError("Nonfinite/nonpositive index")
            numbers.append(number)
        opening, high, low, close = numbers
        if not low <= min(opening, close) <= max(opening, close) <= high:
            raise ValueError("Invalid daily OHLC bounds")
        if start <= day <= cutoff:
            points.append({"date": day.isoformat(), "value": close})
    return points


def fetch_taiex(dimension, start, end, transport=None):
    if (dimension.get("id"), dimension.get("provider"), dimension.get("series_code")) != (
        "market-15", "twse_taiex", "TAIEX"
    ):
        raise HTTPException(409, "고정된 TWSE TAIEX 원지수만 조회할 수 있습니다.")
    cutoff = completed_cutoff(end)
    if start > cutoff:
        raise HTTPException(409, "대만 당일 확정 종가 발표를 기다려주세요.",
                            headers={"X-Collection-State": "not_published"})
    points, hashes = {}, []
    try:
        for index, month in enumerate(months(start, cutoff)):
            if index:
                time.sleep(0.35)
            raw = read_public(f"{HISTORY}?date={month:%Y%m%d}&response=json", transport)
            hashes.append(hashlib.sha256(raw).hexdigest())
            for point in monthly_points(raw, month, start, cutoff):
                if point["date"] in points:
                    raise ValueError("Duplicate across months")
                points[point["date"]] = point
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, UnicodeError):
        raise HTTPException(502, "TWSE TAIEX 계열·월·날짜·종가 검증 실패 · 기존 자료 보존") from None
    if not points:
        raise HTTPException(409, "요청 기간의 TWSE 확정 종가 자료가 아직 없습니다.",
                            headers={"X-Collection-State": "not_published"})
    return {
        "points": [points[k] for k in sorted(points)], "provider": "twse_taiex", "series_code": "TAIEX",
        "source_version": dimension["source_version"], "source_url": OPENAPI,
        "history_url": HISTORY, "representation": "original_series", "definition": dict(dimension),
        "sha256": hashlib.sha256("".join(hashes).encode()).hexdigest(), "page_sha256": hashes,
        "sha256_basis": "ordered_month_response_hashes", "request_count": len(hashes),
        "retrieved_at": now().isoformat(), "completed_cutoff": cutoff.isoformat(),
        "publication_times_verified": False,
    }
