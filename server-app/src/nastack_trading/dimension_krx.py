"""Owner-key KRX VKOSPI EOD only. No sample keys, proxies or implicit scans."""

import hashlib
import json
import math
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from fastapi import HTTPException

from .db import now

URL = "https://data-dbg.krx.co.kr/svc/apis/idx/drvprod_dd_trd"
MAX_DAYS = 5
NAMES = {"코스피200변동성지수", "VKOSPI", "VKOSPI200"}


def apply_krx(row):
    if row["id"] != "control-11":
        return
    row.update(provider="krx_vkospi", series_code="VKOSPI", source_version="krx-vkospi-eod-v1",
        representation="original_index", unit="index points", frequency="일별", can_collect=True,
        source_name="KRX Open API · 코스피 200 변동성지수", source_url="https://openapi.krx.co.kr/",
        access_method="본인 AUTH_KEY · 파생상품지수 시세정보 서비스 승인 필요",
        license="KRX Open API 이용약관·서비스 승인 범위; 개인 키·시세 재배포 권한 별도",
        assessment="conditional_personal_connection", assessment_date="2026-10-27",
        implementation_status="implemented_personal", implementation_label="수집 구현 · 연결 확인 필요",
        daily_feasibility="개인 연결 테스트 후 일별 원지수 수동 수집 · 1회 최대 5거래일",
        manual_only=True, daily_analysis_eligible=False,
        reason="KRX 연결 테스트로 실제 VKOSPI 행을 확인한 뒤 수동 수집합니다. 최근 미공표/결측을 채우지 않습니다. 일별 벡터/예측·자동 수집에는 아직 사용하지 않습니다.")


def credentials(db, user, config, verified=False):
    from .personal_connections import records, values
    record = records(db, user).get("krx", {})
    value = values(db, user, config, "krx") or {}
    key = value.get("api_key")
    if not value.get("enabled") or not isinstance(key, str) or not re.fullmatch(r"[!-~]{8,512}", key):
        raise HTTPException(409, "본인의 KRX 인증키를 등록·활성화하세요. 공용/sample 키로 우회하지 않습니다.")
    if verified and not connected(db, user):
        raise HTTPException(409, "저장된 KRX 키로 연결 테스트를 먼저 완료하세요. 키 발급과 서비스 승인은 별개입니다.")
    return key, record["version"]


def connected(db, user):
    from .models import UserSettings
    from .personal_connections import records
    row = db.get(UserSettings, user.id)
    record = records(db, user).get("krx", {})
    check = (row.value if row and row.tenant_id == user.tenant_id else {}).get("connection_checks", {}).get("krx", {})
    return bool(record and check.get("status") == "connected" and check.get("connection_version") == record.get("version"))


def version_matches(db, user, value):
    from .personal_connections import records
    return value.get("connection_version") == records(db, user).get("krx", {}).get("version")


def validate_owner(db, owner_id, tenant_id, config, key, version, verified=False):
    from .models import User, UserSettings
    current = db.get(User, owner_id, populate_existing=True)
    if not current or current.tenant_id != tenant_id or not current.active or current.role == "viewer":
        raise HTTPException(403, "KRX 조회 권한이 변경되었습니다.")
    settings = db.get(UserSettings, owner_id)
    if settings:
        db.refresh(settings)
    if credentials(db, current, config, verified=verified) != (key, version):
        raise HTTPException(409, "KRX 개인 연결이 변경되어 조회를 중단했습니다.")


def completed_days(start, end):
    from .dimensions import krx_dates
    # Conservative next-day policy: never label an intraday value as EOD.
    cutoff = now().astimezone(ZoneInfo("Asia/Seoul")).date() - timedelta(days=1)
    end = min(end, cutoff)
    return krx_dates(start, end) if start <= end else []


def read_day(day, key, transport=None):
    try:
        with httpx.Client(timeout=15, trust_env=False, follow_redirects=False, transport=transport) as client:
            with client.stream("GET", URL, params={"basDd": day.replace("-", "")}, headers={"AUTH_KEY": key}) as response:
                if response.status_code in {401, 403}:
                    raise HTTPException(409, "KRX 인증키·파생상품지수 시세정보 서비스 승인·접근 IP를 확인하세요.")
                if response.status_code == 429:
                    raise HTTPException(429, "KRX 호출 한도입니다. 잠시 후 다시 시도하세요.", headers={"Retry-After": "60"})
                if response.status_code != 200:
                    raise ValueError("Provider status")
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 2_000_000:
                        raise ValueError("Response limit")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("Invalid JSON object")
        rows = payload.get("OutBlock_1")
        if not isinstance(rows, list) or len(rows) > 10000:
            raise ValueError("Response shape")
        matches = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("Invalid row")
            name = re.sub(r"[\s-]", "", str(row.get("IDX_NM", ""))).upper()
            if name not in NAMES:
                continue
            stamp = str(row.get("BAS_DD", "")).replace("/", "").replace("-", "")
            if stamp != day.replace("-", ""):
                raise ValueError("Wrong observation date")
            number = str(row.get("CLSPRC_IDX", ""))
            if not re.fullmatch(r"(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?", number):
                raise ValueError("Invalid close")
            value = float(number.replace(",", ""))
            if not math.isfinite(value) or value <= 0:
                raise ValueError("Invalid close")
            matches.append({"date": day, "value": value})
        if len(matches) > 1 or (rows and not matches):
            raise ValueError("VKOSPI identity missing or ambiguous")
        return (matches[0] if matches else {"date": day, "value": None}), hashlib.sha256(raw).hexdigest()
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        # Never propagate provider text, request headers or exceptions with keys.
        raise HTTPException(502, "KRX 응답에서 정확한 VKOSPI 기준일·지수명·종가를 확인하지 못했습니다.") from None


def fetch_vkospi(dimension, start, end, key, version, transport=None, latest=False, validate=None):
    days = completed_days(start, end)
    if latest:
        days = list(reversed(days))[:3]
    else:
        days = days[:MAX_DAYS]
    points, hashes = [], []
    for day in days:
        if validate:
            validate()
        point, digest = read_day(day, key, transport)
        points.append(point); hashes.append(digest)
        if latest and point["value"] is not None:
            break
    if not any(p["value"] is not None for p in points):
        raise HTTPException(409, "KRX 요청 거래일의 VKOSPI가 아직 제공되지 않았습니다. 이전 자료를 보존합니다.", headers={"X-Collection-State": "not_published"})
    return {"provider": "krx_vkospi", "series_code": "VKOSPI", "source_version": dimension["source_version"],
        "connection_version": version, "points": sorted(points, key=lambda p: p["date"]),
        "scanned_through": max(days[:len(points)]), "source_url": URL,
        "sha256": hashlib.sha256("\n".join(hashes).encode()).hexdigest(), "requests": len(hashes),
        "retrieved_at": now().isoformat(), "publication_times_verified": False, "daily_analysis_eligible": False}
