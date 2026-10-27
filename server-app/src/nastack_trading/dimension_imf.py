"""IMF iron-ore monthly benchmark via FRED. Not the World Bank/IODEX series."""

import calendar
import csv
import hashlib
import io
import math
import re
from datetime import date
from html import unescape

from fastapi import HTTPException

from .db import now
from .official_daily import read_public

CODE = "PIORECRUSDM"
PAGE = "https://fred.stlouisfed.org/series/" + CODE
CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=" + CODE


def apply_imf(row):
    if row["id"] != "commodity-11":
        return
    row.update(
        name="철광석 IMF · 월평균",
        provider="imf_monthly", series_code=CODE, source_version="imf-iron-monthly-v1",
        source_name="International Monetary Fund · Primary Commodity Prices (FRED 배포)",
        source_url=PAGE, unit="USD/mt", frequency="월간", representation="monthly_benchmark",
        can_collect=True, manual_only=True, daily_analysis_eligible=False,
        assessment="collectable_monthly", assessment_date="2026-10-27",
        implementation_status="implemented_monthly", implementation_label="월간 수집 구현",
        access_method="FRED 공식 CSV + 계열 메타데이터 · 인증키 불필요 · 수동 전용",
        license="IMF Statistical Data 이용조건·출처 표기 · 비상업 개인 연구용; 상업 재사용은 IMF 허락 확인 필요",
        reason="IMF 월평균 원자료입니다. World Bank의 단위 불일치를 임의 보정하지 않고 별도 제공자/계열로 저장합니다. 두 값은 동일하지 않으며 연결하지 않습니다. 일별 벡터·예측과 월간 분석에는 사용하지 않습니다.",
        calendar_note="월말은 관측월 표기이며 공표일이 아닙니다. 원천 월초 날짜도 보존하며 보간·일별 복제는 하지 않습니다.",
    )


def parse_iron(raw, start, end):
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    if reader.fieldnames != ["observation_date", CODE]:
        raise ValueError("Unexpected series")
    points, seen = [], set()
    for row in reader:
        if None in row or set(row) != {"observation_date", CODE}:
            raise ValueError("Invalid CSV row")
        original = date.fromisoformat(row["observation_date"])
        if original.day != 1 or original in seen:
            raise ValueError("Duplicate/nonmonthly observation")
        seen.add(original)
        stamp = date(original.year, original.month, calendar.monthrange(original.year, original.month)[1])
        if stamp >= now().date():
            raise ValueError("Uncompleted/future month")
        value = row[CODE]
        if value in {"", "."}:
            number = None
        else:
            number = float(value)
            if not math.isfinite(number) or number <= 0:
                raise ValueError("Invalid price")
        if start <= stamp <= end:
            points.append({"date": str(stamp), "source_date": str(original), "period": original.strftime("%Y-%m"), "value": number})
    if not any(p["value"] is not None for p in points):
        raise HTTPException(409, "선택 기간의 IMF 월간 자료가 없습니다. 이전 자료는 유지합니다.", headers={"X-Collection-State": "not_published"})
    return sorted(points, key=lambda p: p["date"])


def fetch_iron(dimension, start, end, transport=None):
    try:
        metadata = read_public(PAGE, transport)
        plain = " ".join(unescape(re.sub(r"<[^>]*>", " ", metadata.decode("utf-8"))).split())
        for marker in [CODE, "Global price of Iron Ore", "U.S. Dollars per Metric Ton", "Monthly", "International Monetary Fund"]:
            if marker not in plain:
                raise ValueError("Metadata changed")
        raw = read_public(CSV, transport)
        return {
            "provider": "imf_monthly", "series_code": CODE, "source_version": dimension["source_version"],
            "frequency": "monthly", "source_url": CSV, "metadata_url": PAGE,
            "points": parse_iron(raw, start, end), "sha256": hashlib.sha256(raw).hexdigest(),
            "metadata_sha256": hashlib.sha256(metadata).hexdigest(),
            "available_at": now().isoformat(), "retrieved_at": now().isoformat(),
            "publication_times_verified": False, "daily_analysis_eligible": False,
        }
    except HTTPException:
        raise
    except (ValueError, TypeError, KeyError, UnicodeError, csv.Error):
        raise HTTPException(502, "IMF 철광석의 계열·단위·월간 형식을 확인하지 못했습니다. 이전 자료를 보존합니다.") from None
