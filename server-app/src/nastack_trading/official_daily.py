"""Fixed official daily sources. Never splice different fixing definitions."""

import hashlib
import json
import logging
import math
import re
from datetime import date, datetime, timedelta

import httpx
from fastapi import HTTPException

from .db import now
from .monthly_statistics import EIATable

ECOS_ROOT = "https://ecos.bok.or.kr/api"


class _RedactECOSKey(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        if "/StatisticSearch/" in message:
            record.msg = re.sub(r"(/StatisticSearch/)[^/\s]+", r"\1[REDACTED]", message)
            record.args = ()
        return True


# ECOS requires a path credential. Also sanitize httpx's INFO request URLs and
# httpcore DEBUG request targets, not just our own errors/audit records.
for _logger_name in ("httpx", "httpcore.http11", "httpcore.http2", "httpcore.connection", "httpcore.proxy"):
    logging.getLogger(_logger_name).addFilter(_RedactECOSKey())
EIA = {
    "DCOILWTICO": (
        "https://www.eia.gov/dnav/pet/hist/RWTCD.htm",
        "Cushing, OK WTI Spot Price FOB (Dollars per Barrel)",
    ),
    "DCOILBRENTEU": (
        "https://www.eia.gov/dnav/pet/hist/RBRTED.htm",
        "Europe Brent Spot Price FOB (Dollars per Barrel)",
    ),
    "DHHNGSP": (
        "https://www.eia.gov/dnav/ng/hist/rngwhhdd.htm",
        "Henry Hub Natural Gas Spot Price (Dollars per Million Btu)",
    ),
}


def apply_official_daily(row):
    if row["name"] == "USD/KRW":
        row.update(
            provider="ecos",
            series_code="731Y001/0000001",
            source_name="한국은행 ECOS · 원/미국달러 매매기준율",
            source_url="https://ecos.bok.or.kr/",
            access_method="ECOS 공식 API · 본인 인증키 필요",
            license="한국은행 Open API 이용약관 · 출처 표기",
            unit="KRW/USD",
            source_version="ecos-fixing-v1",
            reason="FRED 뉴욕 정오 환율과 다른 기준입니다. ECOS 과거 자료로 전체 구간을 재수집하며 기존 자료는 이력으로 보존합니다.",
        )
    elif row.get("series_code") in EIA:
        row.update(
            provider="eia_daily",
            source_url=EIA[row["series_code"]][0],
            source_name="U.S. EIA · " + EIA[row["series_code"]][1],
            access_method="EIA 공식 일별 표 · 인증키 불필요",
            source_version="eia-daily-v1",
            reason="FRED 재배포 대신 동일 현물 계열의 EIA 원천을 직접 조회합니다. 원천 발표 지연·휴장 결측은 추정하지 않습니다.",
        )


def read_public(url, transport=None):
    # Callers use fixed hosts/paths only. Do not attach private credentials here.
    try:
        with httpx.Client(timeout=20, trust_env=False, follow_redirects=False, transport=transport) as client:
            with client.stream("GET", url) as response:
                if response.status_code != 200:
                    raise HTTPException(502, "공식 원천 응답을 확인하지 못했습니다. 기존 자료를 보존합니다.")
                raw = bytearray()
                for part in response.iter_bytes():
                    raw.extend(part)
                    if len(raw) > 2_000_000:
                        raise HTTPException(502, "공식 원천 응답 크기 제한을 초과했습니다.")
                return bytes(raw)
    except httpx.HTTPError:
        raise HTTPException(502, "공식 원천 연결 실패 · 기존 자료를 보존합니다.") from None


def packaged(points, dimension, raw):
    return {
        "points": points,
        "provider": dimension["provider"],
        "series_code": dimension["series_code"],
        "source_version": dimension["source_version"],
        "source_url": dimension["source_url"],
        "sha256": hashlib.sha256(raw).hexdigest(),
        "retrieved_at": now().isoformat(),
        "publication_times_verified": False,
    }


def eia_daily_points(raw, code, start, end):
    text = raw.decode("utf-8-sig")
    title = EIA[code][1]
    plain = " ".join(re.sub(r"<[^>]*>", " ", text).split())
    if title not in plain or "Week Of" not in plain:
        raise ValueError("Wrong product or frequency")
    parser = EIATable()
    parser.feed(text)
    points, latest = {}, None
    for row in parser.rows:
        if not row or row[0][0] != "B6":
            continue
        match = re.fullmatch(
            r"\s*(\d{4}) ([A-Za-z]{3})-\s*(\d{1,2}) to ([A-Za-z]{3})-\s*(\d{1,2})\s*", row[0][1]
        )
        if not match or len(row) != 6 or any(cell[0] != "B3" for cell in row[1:]):
            raise ValueError("Unexpected daily table")
        monday = datetime.strptime(f"{match[1]} {match[2]} {match[3]}", "%Y %b %d").date()
        if monday.weekday() != 0:
            raise ValueError("Wrong weekday")
        friday = monday + timedelta(days=4)
        if friday.strftime("%b") != match[4] or friday.day != int(match[5]):
            raise ValueError("Wrong week boundary")
        for offset, (_, item) in enumerate(row[1:]):
            stamp = monday + timedelta(days=offset)
            value = None if item.strip() in {"", "-", "--", "NA", "W"} else float(item.strip())
            if value is not None:
                if not math.isfinite(value):
                    raise ValueError("Nonfinite observation")
                latest = max(latest or stamp, stamp)
            if start <= stamp <= end:
                if str(stamp) in points:
                    raise ValueError("Duplicate observation")
                points[str(stamp)] = {"date": str(stamp), "value": value}
    if not any(p["value"] is not None for p in points.values()):
        if latest and latest < start:
            raise HTTPException(
                409,
                f"EIA 최신 관측일은 {latest}입니다. 요청 기간의 새 자료는 발표 대기 중입니다.",
                headers={"X-Collection-State": "not_published"},
            )
        raise ValueError("No observations")
    return [points[k] for k in sorted(points)]


def fetch_eia(dimension, start, end, transport=None):
    raw = read_public(EIA[dimension["series_code"]][0], transport)
    try:
        return packaged(eia_daily_points(raw, dimension["series_code"], start, end), dimension, raw)
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise HTTPException(502, "EIA 일별 상품·단위·날짜 검증 실패 · 기존 자료 보존") from None


def ecos_key(db, user, config):
    from .personal_connections import values

    connection = values(db, user, config, "ecos") or {}
    key = connection.get("api_key")
    if (
        not connection.get("enabled")
        or not isinstance(key, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{8,200}", key)
    ):
        raise HTTPException(
            409,
            "설정 → 내 외부 서비스 연결에서 본인의 ECOS 정식 인증키를 등록·활성화하세요. 공용·sample 키로 우회하지 않습니다.",
        )
    return key


def fetch_ecos(dimension, start, end, key, transport=None):
    points, hashes = {}, []
    # One calendar year per request, at most 366 daily rows; sample keys forbidden.
    for year in range(start.year, end.year + 1):
        first, last = max(start, date(year, 1, 1)), min(end, date(year, 12, 31))
        url = (
            f"{ECOS_ROOT}/StatisticSearch/{key}/json/kr/1/366/731Y001/D/{first:%Y%m%d}/{last:%Y%m%d}/0000001"
        )
        raw = read_public(url, transport)
        hashes.append(hashlib.sha256(raw).hexdigest())
        try:
            payload = json.loads(raw)
            error = payload.get("RESULT", {}).get("CODE")
            if error == "INFO-200":
                continue
            data = payload["StatisticSearch"]
            rows = data["row"]
            if not isinstance(rows, list) or int(data["list_total_count"]) != len(rows) or len(rows) > 366:
                raise ValueError("Truncated response")
            for row in rows:
                stamp = datetime.strptime(row["TIME"], "%Y%m%d").date()
                value = float(row["DATA_VALUE"])
                if (
                    row["STAT_CODE"] != "731Y001"
                    or row["ITEM_CODE1"] != "0000001"
                    or row["ITEM_NAME1"] != "원/미국달러(매매기준율)"
                    or row["UNIT_NAME"] != "원"
                    or not first <= stamp <= last
                    or not math.isfinite(value)
                    or value <= 0
                    or str(stamp) in points
                ):
                    raise ValueError("Wrong series/unit/date")
                points[str(stamp)] = {"date": str(stamp), "value": value}
        except (ValueError, TypeError, KeyError, AttributeError):
            # No raw provider message or URL: ECOS puts private keys in the URL.
            raise HTTPException(
                502, "ECOS 인증·계열·단위·응답 검증 실패 · 개인 연결과 원천 상태를 확인하세요."
            ) from None
    if not points:
        raise HTTPException(
            409,
            "ECOS 요청 기간의 자료가 아직 제공되지 않았습니다.",
            headers={"X-Collection-State": "not_published"},
        )
    return packaged([points[k] for k in sorted(points)], dimension, "".join(hashes).encode())
