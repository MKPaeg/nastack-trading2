"""Fixed public monthly industrial statistics, isolated from news and trading."""

import csv
import io
import math
from datetime import date, timedelta

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from .db import aware, local_time, now
from .models import PersonalSource, StatisticSeries, User
from .monthly_statistics import MONTHLY_SERIES, fetch_monthly
from .news import capability
from .security import audit
from .source_visibility import successful_source_ids

SERIES = {
    "INDPRO": {
        "name": "미국 산업생산 총지수",
        "unit": "2017=100 · 계절조정",
        "frequency": "월간",
        "source": "Federal Reserve · FRED",
        "url": "https://fred.stlouisfed.org/series/INDPRO",
    },
    "WB_KOR_MANUFACTURING": {
        "name": "한국 제조업 부가가치",
        "unit": "current US$ · 명목",
        "frequency": "연간",
        "source": "World Bank · WDI",
        "url": "https://data.worldbank.org/indicator/NV.IND.MANF.CD?locations=KR",
        "domain": "datahelpdesk.worldbank.org",
        "indicator": "NV.IND.MANF.CD",
    },
    "WB_KOR_EXPORT_GROWTH": {
        "name": "한국 재화·서비스 수출 실질 증가율",
        "unit": "% · 연간 실질 증가율",
        "frequency": "연간",
        "source": "World Bank · WDI",
        "url": "https://data.worldbank.org/indicator/NE.EXP.GNFS.KD.ZG?locations=KR",
        "domain": "datahelpdesk.worldbank.org",
        "indicator": "NE.EXP.GNFS.KD.ZG",
    },
}
SERIES.update(MONTHLY_SERIES)


def worldbank_points(transport=None, indicator="NV.IND.MANF.CD"):
    """Bounded official indicator response; year is an observation period, not publication."""
    with httpx.Client(timeout=20, trust_env=False, follow_redirects=False, transport=transport) as client:
        with client.stream(
            "GET",
            "https://api.worldbank.org/v2/country/KOR/indicator/" + indicator,
            params={"format": "json", "date": f"2020:{now().year}", "per_page": 100},
        ) as response:
            response.raise_for_status()
            raw = bytearray()
            for chunk in response.iter_bytes():
                raw.extend(chunk)
                if len(raw) > 1_000_000:
                    raise ValueError("Oversized response")
    import json

    value = json.loads(raw)
    if not isinstance(value, list) or len(value) != 2 or value[0].get("pages") != 1:
        raise ValueError("Unexpected pagination")
    points = []
    for item in value[1]:
        if item["countryiso3code"] != "KOR" or item["indicator"]["id"] != indicator:
            raise ValueError("Wrong series")
        if item["value"] is None:
            continue
        year, amount = int(item["date"]), float(item["value"])
        if 2020 <= year <= now().year and math.isfinite(amount):
            points.append({"date": f"{year}-01-01", "value": amount})
    if not points:
        raise ValueError("No observations")
    return sorted(points, key=lambda item: item["date"])


def refresh_statistics(code, user, db, config, transport=None, *, checkpoint=None):
    owner_id, tenant_id = user.id, user.tenant_id
    if not user.active or user.role == "viewer":
        raise HTTPException(403, "조회 전용입니다.")
    if code not in SERIES:
        raise HTTPException(404, "등록되지 않은 통계입니다.")
    policy = db.scalars(
        select(PersonalSource).where(
            PersonalSource.owner_id == user.id, PersonalSource.tenant_id == user.tenant_id
        )
    ).all()
    if any(
        s.domain.removeprefix("www.") == SERIES[code].get("domain", "fred.stlouisfed.org")
        and (s.value.get("disposition") == "exclude" or s.value.get("status") == "retired")
        for s in policy
    ):
        raise HTTPException(409, "해당 통계 소스가 사용 제외되어 있습니다.")
    approved = [
        s
        for s in policy
        if s.domain.removeprefix("www.") == SERIES[code].get("domain", "fred.stlouisfed.org")
        and s.value.get("status") == "active"
        and s.value.get("disposition") == "use"
    ]
    if code in MONTHLY_SERIES and not approved:
        raise HTTPException(409, "본인 출처 목록에서 해당 공식 통계를 분석 사용으로 승인하세요.")
    versions = {s.id: s.version for s in approved}
    db.scalar(select(User).where(User.id == user.id).with_for_update())
    row = db.get(StatisticSeries, (user.id, code))
    if row and row.tenant_id != user.tenant_id:
        raise HTTPException(404, "본인의 통계가 아닙니다.")
    if row and aware(row.updated_at) > now() - timedelta(minutes=5):
        return {"cached": True, "count": len(row.value["points"])}
    from .data_collection import charge

    charge(db, user, "statistics")
    if code in MONTHLY_SERIES:
        try:
            value = fetch_monthly(code, transport)
        except (httpx.HTTPError, ValueError, KeyError, TypeError, UnicodeError):
            raise HTTPException(502, "공식 월간 원천 조회·단위 검증 실패 · 기존 자료를 보존합니다.") from None
        if checkpoint:
            checkpoint()
        db.expire_all()
        current = db.get(User, owner_id, populate_existing=True)
        if not current or not current.active or current.role == "viewer" or current.tenant_id != tenant_id:
            raise HTTPException(403, "수집 권한이 변경되었습니다.")
        for sid, version in versions.items():
            source = db.get(PersonalSource, sid, populate_existing=True, with_for_update=True)
            if (
                not source
                or source.owner_id != owner_id
                or source.tenant_id != tenant_id
                or source.version != version
                or source.value.get("status") != "active"
                or source.value.get("disposition") != "use"
            ):
                raise HTTPException(409, "통계 출처 승인이 변경되었습니다.")
        if not row:
            row = StatisticSeries(owner_id=user.id, tenant_id=user.tenant_id, series_id=code)
            db.add(row)
        row.value, row.updated_at = value, now()
        audit(
            db, user, "statistics.refresh", code, {"count": len(value["points"]), "source_versions": versions}
        )
        db.commit()
        return {"cached": False, "count": len(value["points"])}
    if code.startswith("WB_"):
        try:
            points = worldbank_points(transport, SERIES[code]["indicator"])
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise HTTPException(502, "World Bank 원천 조회 실패 · 기존 자료를 보존합니다.") from None
        if checkpoint:
            checkpoint()
        if not row:
            row = StatisticSeries(owner_id=user.id, tenant_id=user.tenant_id, series_id=code)
            db.add(row)
        row.value, row.updated_at = {"points": points}, now()
        audit(db, user, "statistics.refresh", code, {"count": len(points)})
        db.commit()
        return {"cached": False, "count": len(points)}
    try:
        with httpx.Client(timeout=20, follow_redirects=False, trust_env=False, transport=transport) as client:
            with client.stream(
                "GET",
                "https://fred.stlouisfed.org/graph/fredgraph.csv",
                params={"id": code, "cosd": "2020-01-01"},
            ) as response:
                response.raise_for_status()
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 1_000_000:
                        raise ValueError
        points = []
        for item in csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))):
            stamp = date.fromisoformat(item.get("observation_date") or item.get("DATE") or "")
            if item.get(code) in {".", "", None}:
                continue
            value = float(item[code])
            if math.isfinite(value) and date(2020, 1, 1) <= stamp <= now().date():
                points.append({"date": str(stamp), "value": value})
        points = sorted({p["date"]: p for p in points}.values(), key=lambda p: p["date"])
        if not points:
            raise ValueError
    except (httpx.HTTPError, ValueError, KeyError, UnicodeError):
        raise HTTPException(502, "통계 원천 조회 실패 · 기존 저장 자료를 보존합니다.") from None
    if checkpoint:
        checkpoint()
    if not row:
        row = StatisticSeries(owner_id=user.id, tenant_id=user.tenant_id, series_id=code)
        db.add(row)
    row.value, row.updated_at = {"points": points}, now()
    audit(db, user, "statistics.refresh", code, {"count": len(points)})
    db.commit()
    return {"cached": False, "count": len(points)}


def statistics_router(current_user, db_session, config, transport=None):
    router = APIRouter()

    @router.get("/api/statistics")
    def stored(user=Depends(current_user), db=Depends(db_session)):
        sources = db.scalars(
            select(PersonalSource).where(
                PersonalSource.owner_id == user.id, PersonalSource.tenant_id == user.tenant_id
            )
        ).all()
        rows = []
        successful = successful_source_ids(db, user)
        for code, definition in SERIES.items():
            row = db.get(StatisticSeries, (user.id, code))
            if row and row.tenant_id != user.tenant_id:
                row = None
            rows.append(
                {
                    "id": code,
                    **definition,
                    "points": row.value["points"] if row and row.tenant_id == user.tenant_id else [],
                    "collected_at": local_time(row.updated_at, config.timezone) if row else None,
                    "release_date": row.value.get("release_date")
                    if row and row.tenant_id == user.tenant_id
                    else None,
                }
            )
        return {
            "series": rows,
            "sources": [
                capability(source)
                for source in sources
                if capability(source)["kind"] == "statistics" and source.id in successful
            ],
            "note": "통계 원천값은 뉴스 점수 합계에서 제외합니다. 사업 기반 수집 계획의 통계 해석은 별도로 표시합니다. 월간·연간 원천 주기를 유지하며 거래일별로 보간하지 않습니다. 관측월은 발표일이 아닙니다. 최신 수정치이며 과거 시점 검증용 빈티지가 아닙니다.",
        }

    @router.post("/api/statistics/{code}/refresh")
    def refresh(code: str, user=Depends(current_user), db=Depends(db_session)):
        return refresh_statistics(code, user, db, config, transport)

    return router
