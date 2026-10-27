"""US BLS iron/steel PPI, monthly index; never a steel spot price or daily input."""
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

CODE = "WPU101"
PAGE = "https://fred.stlouisfed.org/series/WPU101"
CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=WPU101"
NAME = "철강 생산자물가지수 · 미국 · 월간"


def apply_steel(row):
    if row["id"] != "commodity-37":
        return
    row.update(provider="bls_steel_monthly", series_code=CODE, source_version="bls-wpu101-monthly-v1",
        name=NAME, source_name="U.S. BLS · Iron and Steel PPI (FRED 배포)", source_url=PAGE,
        unit="Index 1982=100 · 비계절조정", frequency="월간", representation="monthly_benchmark",
        monthly_measure="producer_price_index", can_collect=True, manual_only=True, daily_analysis_eligible=False,
        assessment="collectable_monthly", assessment_date="2026-10-27",
        implementation_status="implemented_monthly", implementation_label="월간 수집 구현 · 공개 CSV 확인",
        access_method="FRED 공식 계열 메타데이터 + CSV · 인증키 불필요 · 수동 전용",
        license="BLS 공공 통계 · 출처 표기 · FRED 이용약관 준수",
        reason="미국 철·철강 생산자물가의 월간 지수입니다. 국내 철강·중국 FOB 열연강판·철광석·톤당 현물가격과 다르며 대체/접합하지 않습니다. 월간 관측만 보존하고 일별 복제·벡터/예측에는 사용하지 않습니다.",
        calendar_note="월말은 관측월 표기이며 공표일이 아닙니다. 비계절조정 지수이며 과거 수치 개정·공표 시점은 별도 검증이 필요합니다.")


def parse_steel(raw, start, end):
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    if reader.fieldnames != ["observation_date", CODE]:
        raise ValueError("Wrong series")
    points, seen = [], set()
    for row in reader:
        if None in row or set(row) != {"observation_date", CODE}:
            raise ValueError("Invalid row")
        original = date.fromisoformat(row["observation_date"])
        if original.day != 1 or original in seen:
            raise ValueError("Invalid monthly observation")
        seen.add(original)
        stamp = date(original.year, original.month, calendar.monthrange(original.year, original.month)[1])
        if stamp >= now().date():
            raise ValueError("Incomplete month")
        value = None if row[CODE] in {"", "."} else float(row[CODE])
        if value is not None and (not math.isfinite(value) or value <= 0):
            raise ValueError("Invalid index")
        if start <= stamp <= end:
            points.append({"date": str(stamp), "source_date": str(original), "period": original.strftime("%Y-%m"), "value": value})
    if not any(p["value"] is not None for p in points):
        raise HTTPException(409, "선택 기간의 철강 생산자물가지수가 아직 제공되지 않았습니다. 이전 자료를 보존합니다.", headers={"X-Collection-State": "not_published"})
    return sorted(points, key=lambda p: p["date"])


def fetch_steel(dimension, start, end, transport=None):
    try:
        metadata = read_public(PAGE, transport)
        plain = " ".join(unescape(re.sub(r"<[^>]*>", " ", metadata.decode("utf-8"))).split())
        for marker in [CODE, "Iron and Steel", "Index 1982=100", "Monthly", "Bureau of Labor Statistics", "Not Seasonally Adjusted"]:
            if marker not in plain:
                raise ValueError("Metadata changed")
        raw = read_public(CSV, transport)
        return {"provider": "bls_steel_monthly", "series_code": CODE, "source_version": dimension["source_version"],
            "frequency": "monthly", "source_url": CSV, "metadata_url": PAGE,
            "points": parse_steel(raw, start, end), "sha256": hashlib.sha256(raw).hexdigest(),
            "metadata_sha256": hashlib.sha256(metadata).hexdigest(), "retrieved_at": now().isoformat(),
            "publication_times_verified": False, "daily_analysis_eligible": False}
    except HTTPException:
        raise
    except (ValueError, TypeError, KeyError, UnicodeError, csv.Error):
        raise HTTPException(502, "BLS 철강 생산자물가지수의 계열·단위·월간 형식을 확인하지 못했습니다. 이전 자료를 보존합니다.") from None
