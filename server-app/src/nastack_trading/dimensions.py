"""Bounded public-series collection and observation-date (not PIT) alignment."""

import csv
import hashlib
import io
import math
import threading
from datetime import date, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import load_only

from .db import aware, local_time, now
from .dimension_catalog import BY_ID, DIMENSIONS
from .models import DimensionCollection, DimensionExclusion, User
from .security import audit

FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv"
MAX_BYTES = 2_000_000


def excluded_ids(db, user):
    return set(
        db.scalars(
            select(DimensionExclusion.dimension_id).where(
                DimensionExclusion.owner_id == user.id,
                DimensionExclusion.tenant_id == user.tenant_id,
            )
        ).all()
    )


def exclude_uncollected(db, user, dimension, reason):
    """Operator action: recoverably remove only an owner’s zero-observation candidate."""
    if user.role == "viewer":
        raise HTTPException(403, "조회 전용 사용자는 목록을 변경할 수 없습니다.")
    db.scalar(select(User).where(User.id == user.id).with_for_update())
    existing = db.get(DimensionExclusion, (user.id, dimension["id"]))
    if existing:
        return existing
    batches = db.scalars(
        select(DimensionCollection).where(
            DimensionCollection.owner_id == user.id,
            DimensionCollection.tenant_id == user.tenant_id,
            DimensionCollection.dimension_id == dimension["id"],
            DimensionCollection.status == "completed",
        )
    ).all()
    if any(batch.value.get("points") for batch in batches) or db.scalar(
        select(DimensionCollection.id)
        .where(
            DimensionCollection.owner_id == user.id,
            DimensionCollection.dimension_id == dimension["id"],
            DimensionCollection.status == "fetching",
        )
        .limit(1)
    ):
        raise HTTPException(409, "수집 이력이 있는 차원은 미수집 제외 대상이 아닙니다.")
    record = DimensionExclusion(
        owner_id=user.id,
        tenant_id=user.tenant_id,
        dimension_id=dimension["id"],
        reason=reason,
        definition=dict(dimension),
    )
    db.add(record)
    audit(db, user, "dimension.exclude", dimension["id"], {"reason": reason})
    return record


class CollectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: date
    end: date
    force: bool = False


def missing_ranges(db, user, dimension, start, end, timezone):
    """Subtract successful DB intervals; retry delayed trailing observations next day."""
    today = now().astimezone(ZoneInfo(timezone)).date()
    if dimension.get("provider") == "krx_vkospi":
        end = min(end, now().astimezone(ZoneInfo("Asia/Seoul")).date() - timedelta(days=1))
        if start > end:
            return []
    if dimension.get("representation") == "monthly_benchmark":
        # Re-read the whole selected monthly window after a day, so revisions
        # and newly published months are not hidden by old successful intervals.
        fresh = any(b.status == "completed" and b.value.get("source_version") == dimension.get("source_version")
                    and b.value.get("provider") == dimension.get("provider")
                    and b.value.get("series_code") == dimension.get("series_code")
                    and b.start_date <= start and b.end_date >= end
                    and aware(b.created_at) > now() - timedelta(hours=24)
                    for b in scoped(db, user, dimension["id"], start, end))
        return [] if fresh else [(start, end)]
    if dimension.get("provider") == "twse_taiex":
        from .dimension_twse import completed_cutoff

        end = completed_cutoff(end)
        if start > end:
            return []
    if dimension.get("provider") == "kiwoom_us_etf":
        local = now().astimezone(ZoneInfo("America/New_York"))
        cutoff = local.date() - timedelta(days=1) if (local.hour, local.minute) < (16, 15) else local.date()
        calendar = xcals.get_calendar("XNYS", start=str(cutoff - timedelta(days=20)), end=str(cutoff))
        # Calendar construction can end on a weekend/holiday, so its last session
        # precedes cutoff. sessions_in_range rejects that otherwise valid boundary.
        completed = calendar.sessions[
            (calendar.sessions.date >= cutoff - timedelta(days=14)) & (calendar.sessions.date <= cutoff)
        ]
        if len(completed):
            end = min(end, completed[-1].date())
        if start > end:
            return []
    covered = []
    for batch in scoped(db, user, dimension["id"], start, end):
        if batch.status != "completed" or batch.value.get("series_code") != dimension["series_code"]:
            continue
        if batch.value.get("provider", "fred") != dimension.get("provider"):
            continue
        if dimension.get("provider") == "krx_vkospi":
            from .dimension_krx import version_matches
            if batch.value.get("source_version") != dimension.get("source_version") or not version_matches(db, user, batch.value):
                continue
        points = [point["date"] for point in batch.value.get("points", []) if point.get("value") is not None]
        if not points:
            continue
        through = (
            batch.end_date
            if aware(batch.created_at).astimezone(ZoneInfo(timezone)).date() == today
            else min(batch.end_date, date.fromisoformat(max(points)))
        )
        collected_local = aware(batch.created_at).astimezone(ZoneInfo("Asia/Seoul"))
        if dimension.get("provider") == "krx_vkospi":
            through = min(through, date.fromisoformat(batch.value["scanned_through"]), date.fromisoformat(max(points)))
        if dimension.get("provider") == "twse_taiex":
            # A partial month / empty holiday tail must not cover unpublished days.
            through = min(through, date.fromisoformat(max(points)))
        if dimension.get("provider") in {"namuh_etf", "kiwoom_index", "kiwoom_us_etf"}:
            last_observation = date.fromisoformat(max(points))
            if last_observation < batch.end_date and krx_dates(
                last_observation + timedelta(days=1), batch.end_date
            ):
                through = min(through, last_observation)
        if (
            dimension.get("provider") in {"namuh_etf", "kiwoom_index"}
            and max(points) == collected_local.date().isoformat()
            and (collected_local.hour, collected_local.minute) < (15, 45)
        ):
            through = min(through, collected_local.date() - timedelta(days=1))
        if batch.start_date <= through:
            first = max(batch.start_date, date.fromisoformat(min(points)))
            expected = krx_dates(batch.start_date, through)
            if expected and min(points) <= expected[0]:
                first = batch.start_date
            covered.append((max(start, first), min(end, through)))
    cursor, missing = start, []
    for first, last in sorted(covered):
        if first > last or last < cursor:
            continue
        if first > cursor:
            missing.append((cursor, first - timedelta(days=1)))
        cursor = max(cursor, last + timedelta(days=1))
    if cursor <= end:
        missing.append((cursor, end))
    return missing


def dates(start, end, timezone):
    today = now().astimezone(ZoneInfo(timezone)).date()
    if start > end or end > today or start < date(2000, 1, 1) or (end - start).days > 1826:
        raise HTTPException(422, "기간은 2000년 이후, 시작일 ≤ 종료일 ≤ 오늘, 최대 5년으로 선택하세요.")


CALENDAR_LOCK = threading.RLock()


@lru_cache(maxsize=32)
def _krx_year(year):
    """Immutable PUBLIC exchange calendar only; never cache personal observations.

    Fixed year bounds avoid rebuilding XKRX's holiday rules for every collection
    interval. The process-local cache expires with the installed calendar version.
    """
    calendar = xcals.get_calendar("XKRX", start=f"{year}-01-01", end=f"{year}-12-31")
    return tuple(day.date().isoformat() for day in calendar.sessions)


def krx_dates(start, end):
    try:
        if start > end:
            raise ValueError("Invalid interval")
        # Serialize cold initialization too: lru_cache alone can build a missing
        # year multiple times when analysis and the independent worker overlap.
        with CALENDAR_LOCK:
            sessions = tuple(day for year in range(start.year, end.year + 1) for day in _krx_year(year))
        first, last = start.isoformat(), end.isoformat()
        return [day for day in sessions if first <= day <= last]
    except (ValueError, IndexError, OverflowError):
        raise HTTPException(422, "지원되는 KRX 거래일 달력 범위 밖입니다.") from None


def parse_csv(raw, code, start, end):
    try:
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
        fields = reader.fieldnames or []
        date_key = "observation_date" if "observation_date" in fields else "DATE"
        if date_key not in fields or code not in fields:
            raise ValueError
        points = {}
        last_observation = None
        for row in reader:
            day = date.fromisoformat(row[date_key])
            if row.get(code) not in (None, "", "."):
                last_observation = max(last_observation, day) if last_observation else day
            if not start <= day <= end:
                continue
            text = (row.get(code) or "").strip()
            value = None if text in {"", "."} else float(text)
            if value is not None and not math.isfinite(value):
                raise ValueError
            key = day.isoformat()
            if key in points:
                raise ValueError
            points[key] = {"date": key, "value": value}
        if not any(item["value"] is not None for item in points.values()):
            if last_observation and last_observation < start:
                raise HTTPException(409, f"공식 원천의 최신 관측일은 {last_observation}입니다. 요청 기간의 새 자료가 아직 제공되지 않았습니다. 이전 자료는 유지하며 결측을 임의로 채우지 않습니다.",
                                    headers={"X-Collection-State": "not_published"})
            raise ValueError
        return [points[key] for key in sorted(points)]
    except (ValueError, KeyError, TypeError, UnicodeError, csv.Error):
        raise HTTPException(
            502, "공급자의 유효한 일별 CSV 관측값을 확인하지 못했습니다. 이전 수집 기록은 유지합니다."
        ) from None


def fetch_points(dimension, start, end, transport=None):
    if dimension.get("provider") == "bls_steel_monthly" and dimension.get("can_collect"):
        from .dimension_steel import fetch_steel

        return fetch_steel(dimension, start, end, transport)
    if dimension.get("provider") == "imf_monthly" and dimension.get("can_collect"):
        from .dimension_imf import fetch_iron

        return fetch_iron(dimension, start, end, transport)
    if dimension.get("provider") == "worldbank_monthly" and dimension.get("can_collect"):
        from .dimension_worldbank import fetch_monthly

        return fetch_monthly(dimension, start, end, transport)
    if dimension.get("provider") == "twse_taiex":
        from .dimension_twse import fetch_taiex

        return fetch_taiex(dimension, start, end, transport)
    if not dimension["can_collect"]:
        raise HTTPException(409, dimension["reason"])
    if dimension.get("provider") == "eia_daily":
        from .official_daily import fetch_eia

        return fetch_eia(dimension, start, end, transport)
    if dimension.get("provider") not in {None, "fred"}:
        raise HTTPException(409, "이 계열은 공급자 전용 수집 경로를 사용해야 합니다.")
    try:
        with httpx.Client(timeout=20, follow_redirects=False, trust_env=False, transport=transport) as client:
            with client.stream(
                "GET",
                FRED_CSV,
                params={"id": dimension["series_code"], "cosd": str(start), "coed": str(end)},
            ) as response:
                if response.status_code != 200:
                    raise HTTPException(502, "공급자가 수집 요청을 거부했습니다. 이전 기록은 유지합니다.")
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_BYTES:
                        raise HTTPException(502, "공급자 응답 크기가 허용 범위를 초과했습니다.")
        return {
            "points": parse_csv(bytes(raw), dimension["series_code"], start, end),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "source_url": dimension["source_url"],
            "series_code": dimension["series_code"],
            "retrieved_at": now().isoformat(),
            "publication_times_verified": False,
        }
    except httpx.HTTPError:
        raise HTTPException(
            502, "공급자 연결에 실패했습니다. 잠시 후 다시 시도하세요. 이전 기록은 유지합니다."
        ) from None


def scoped(db, user, dimension_id, start, end):
    return db.scalars(
        select(DimensionCollection)
        .where(
            DimensionCollection.owner_id == user.id,
            DimensionCollection.tenant_id == user.tenant_id,
            DimensionCollection.dimension_id == dimension_id,
            DimensionCollection.start_date <= end,
            DimensionCollection.end_date >= start,
        )
        .order_by(DimensionCollection.created_at, DimensionCollection.id)
    ).all()


def series_view(db, user, dimension, start, end, timezone):
    monthly = dimension.get("representation") == "monthly_benchmark"
    sessions = [] if monthly else krx_dates(start, end)
    rows = scoped(db, user, dimension["id"], start, end)
    points = {}
    last_success = None
    for batch in rows:
        if batch.status != "completed" or batch.start_date > end or batch.end_date < start:
            continue
        if batch.value.get("series_code") != dimension["series_code"]:
            continue
        if batch.value.get("provider", "fred") != dimension.get("provider"):
            continue
        if dimension.get("provider") == "krx_vkospi":
            from .dimension_krx import version_matches
            if batch.value.get("source_version") != dimension.get("source_version") or not version_matches(db, user, batch.value):
                continue
        if monthly and batch.value.get("source_version") != dimension.get("source_version"):
            continue
        last_success = batch
        for point in batch.value.get("points", []):
            if str(start) <= point["date"] <= str(end):
                points[point["date"]] = {**point, "collected_at": local_time(batch.created_at, timezone)}
    observed = [points[key] for key in sorted(points)]
    finite = [point for point in observed if point["value"] is not None]
    missing = []
    matched = []
    for day in sessions:
        point = points.get(day)
        if point and point["value"] is not None:
            matched.append(point)
        else:
            missing.append({"date": day, "reason": "원천 결측" if point else "미수집 또는 원천 관측 없음"})
    attempt = rows[-1] if rows else None
    return {
        "dimension": dimension,
        "retrieval_providers": sorted(
            {
                batch.value.get("retrieval_provider", batch.value.get("provider", "unknown"))
                for batch in rows
                if batch.status == "completed" and batch.value.get("series_code") == dimension["series_code"]
                and batch.value.get("provider", "fred") == dimension.get("provider")
            }
        ),
        "start": str(start),
        "end": str(end),
        "points": observed,
        "krx_points": matched,
        "missing": missing,
        "coverage": {
            "source_observations": len(finite),
            "krx_sessions": len(sessions),
            "matched": len(matched),
            "missing": len(missing),
            "ratio": len(matched) / len(sessions) if sessions else None,
            "first": finite[0]["date"] if finite else None,
            "last": finite[-1]["date"] if finite else None,
        },
        "calendar": {
            "name": "XKRX",
            "provider": "exchange_calendars",
            "version": xcals.__version__,
            "alignment": "monthly_observation_only" if monthly else "observation_date_only",
            "point_in_time_verified": False,
            "note": "월말 날짜는 관측월 표기입니다. 원래의 월간 가격·지수만 표시하며 KRX 거래일 대응·일별 벡터/예측 입력·보간은 사용하지 않습니다. 공표 당시 이용 가능성은 미검증입니다." if monthly else "한국 거래일과 원천 관측일의 날짜 교집합입니다. 해외 발표 시각·당시 이용 가능 여부는 검증 전이며 상관·자동매매 입력으로 사용하지 않습니다.",
        },
        "last_success_at": local_time(last_success.created_at, timezone) if last_success else None,
        "last_attempt": {
            "status": attempt.status,
            "error_code": attempt.error_code,
            "created_at": local_time(attempt.created_at, timezone),
        }
        if attempt
        else None,
    }


def collect(
    db, user, dimension, start, end, transport=None, config=None, broker_transport=None, kiwoom_transport=None
):
    owner_id, tenant_id = user.id, user.tenant_id
    if user.role == "viewer":
        raise HTTPException(403, "조회 전용 사용자는 외부 데이터 수집을 요청할 수 없습니다.")
    if not dimension["can_collect"]:
        raise HTTPException(409, dimension["reason"])
    key = None
    if dimension.get("provider") == "ecos":
        from .official_daily import ecos_key

        key = ecos_key(db, user, config)  # Missing/disabled key consumes no budget.
    if dimension.get("provider") == "krx_vkospi":
        from .dimension_krx import credentials
        key, connection_version = credentials(db, user, config, verified=True)
    # Serialize attempts per owner; persist the attempt before releasing the lock
    # and before starting external I/O. Failed calls also consume the cooldown.
    db.scalar(select(User).where(User.id == user.id).with_for_update())
    if dimension["id"] in excluded_ids(db, user):
        raise HTTPException(410, "활성 목록에서 제외된 차원입니다.")
    latest = db.scalar(
        select(DimensionCollection)
        .where(
            DimensionCollection.owner_id == user.id,
            DimensionCollection.tenant_id == user.tenant_id,
        )
        .order_by(DimensionCollection.created_at.desc())
        .limit(1)
    )
    if latest and aware(latest.created_at) > now() - timedelta(seconds=5):
        raise HTTPException(429, "수집 요청은 5초 간격으로 실행하세요.", headers={"Retry-After": "5"})
    from .data_collection import charge

    charge(db, user, "dimension")
    batch = DimensionCollection(
        tenant_id=user.tenant_id,
        owner_id=user.id,
        dimension_id=dimension["id"],
        start_date=start,
        end_date=end,
        status="fetching",
        value={"provider": dimension.get("provider"), "series_code": dimension["series_code"]},
    )
    db.add(batch)
    db.commit()
    try:
        if dimension.get("provider") == "krx_vkospi":
            from .dimension_krx import fetch_vkospi, validate_owner
            batch.value = fetch_vkospi(dimension, start, end, key, connection_version, transport,
                validate=lambda: validate_owner(db, owner_id, tenant_id, config, key, connection_version, verified=True))
        elif dimension.get("provider") == "ecos":
            from .official_daily import fetch_ecos

            batch.value = fetch_ecos(dimension, start, end, key, transport)
        elif dimension.get("provider") == "namuh_etf":
            from .dimension_namuh import fetch_etf

            if config is None:
                raise HTTPException(409, "나무 수집 서버 설정이 필요합니다.")
            batch.value = fetch_etf(
                db, user, config, dimension, start, end, broker_transport, kiwoom_transport
            )
        elif dimension.get("provider") == "kiwoom_us_etf":
            from .dimension_us_etf import fetch_us_etf

            if config is None:
                raise HTTPException(409, "키움 수집 서버 설정이 필요합니다.")
            batch.value = fetch_us_etf(db, user, config, dimension, start, end, kiwoom_transport)
        elif dimension.get("provider") == "kiwoom_index":
            from .kiwoom import fetch_index

            if config is None:
                raise HTTPException(409, "키움 수집 서버 설정이 필요합니다.")
            batch.value = fetch_index(db, user, config, dimension, start, end, kiwoom_transport)
        else:
            batch.value = fetch_points(dimension, start, end, transport)
        current = db.get(User, owner_id, populate_existing=True)
        if not current or current.tenant_id != tenant_id or not current.active or current.role == "viewer" or dimension["id"] in excluded_ids(db, current):
            raise HTTPException(403, "수집 권한이 변경되어 결과를 적용하지 않습니다.")
        if dimension.get("provider") == "ecos":
            from .models import UserSettings

            settings = db.get(UserSettings, current.id)
            if settings:
                db.refresh(settings)
            if ecos_key(db, current, config) != key:
                raise HTTPException(409, "ECOS 개인 연결이 변경되어 결과를 적용하지 않습니다.")
        if dimension.get("provider") == "krx_vkospi":
            from .models import UserSettings
            settings = db.get(UserSettings, current.id)
            if settings:
                db.refresh(settings)
            if credentials(db, current, config, verified=True) != (key, connection_version):
                raise HTTPException(409, "KRX 개인 연결이 변경되어 결과를 적용하지 않습니다.")
        batch.status = "completed"
        audit(
            db,
            user,
            "dimension.collected",
            batch.id,
            {
                "dimension_id": dimension["id"],
                "count": len(batch.value["points"]),
                "start": str(start),
                "end": str(end),
                "sha256": batch.value["sha256"],
            },
        )
        db.commit()
    except HTTPException as error:
        batch.status, batch.error_code = "failed", "SOURCE_NOT_PUBLISHED" if (error.headers or {}).get("X-Collection-State") == "not_published" else "PROVIDER_UNAVAILABLE"
        audit(db, user, "dimension.collection_failed", batch.id, {"dimension_id": dimension["id"]})
        db.commit()
        raise error
    return batch


def dimensions_router(
    current_user, db_session, config, transport=None, broker_transport=None, kiwoom_transport=None
):
    router = APIRouter()

    def dimension_or_404(dimension_id):
        if dimension_id not in BY_ID:
            raise HTTPException(404, "등록된 차원만 조회할 수 있습니다.")
        return BY_ID[dimension_id]

    @router.get("/api/dimensions")
    def catalog(user=Depends(current_user), db=Depends(db_session)):
        excluded = excluded_ids(db, user)
        batches = db.scalars(
            select(DimensionCollection)
            .options(
                load_only(
                    DimensionCollection.dimension_id,
                    DimensionCollection.status,
                    DimensionCollection.created_at,
                    DimensionCollection.value,
                )
            )
            .where(
                DimensionCollection.owner_id == user.id,
                DimensionCollection.tenant_id == user.tenant_id,
            )
            .order_by(DimensionCollection.created_at, DimensionCollection.id)
        ).all()
        successes, attempts, observations = {}, {}, {}
        for batch in batches:
            attempts[batch.dimension_id] = batch
            definition = BY_ID.get(batch.dimension_id, {})
            version_matches = definition.get("representation") != "monthly_benchmark" or batch.value.get("source_version") == definition.get("source_version")
            if definition.get("provider") == "krx_vkospi":
                from .dimension_krx import version_matches as krx_version_matches
                version_matches = batch.value.get("source_version") == definition.get("source_version") and krx_version_matches(db, user, batch.value)
            if batch.status == "completed" and version_matches and batch.value.get("series_code") == definition.get("series_code") and batch.value.get("provider", "fred") == definition.get("provider"):
                successes[batch.dimension_id] = batch
                valid_points = observations.setdefault(batch.dimension_id, set())
                for point in batch.value.get("points", []):
                    value = point.get("value")
                    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and point.get("date"):
                        valid_points.add(point["date"])
        items = []
        from .dimension_krx import connected
        krx_ready = connected(db, user)
        for dimension in DIMENSIONS:
            if dimension["id"] in excluded:
                continue
            success, attempt = successes.get(dimension["id"]), attempts.get(dimension["id"])
            items.append(
                {
                    **dimension,
                    "source_changed": dimension["id"] not in successes and any(b.dimension_id == dimension["id"] and b.status == "completed" for b in batches),
                    "last_success_at": local_time(success.created_at, config.timezone) if success else None,
                    "last_attempt_status": attempt.status if attempt else "not_collected",
                }
            )
            if dimension.get("provider") == "krx_vkospi":
                items[-1].update(connection_verified=krx_ready, can_collect=krx_ready)
                if krx_ready:
                    items[-1].update(family="market", group_name="국가·시장", implementation_label="연결 확인 · 원지수 수집 구현")
            row = items[-1]
            count = len(observations.get(dimension["id"], set()))
            reason = (
                "미연결 · 수집 경로 또는 개인 연결 확인 필요" if not row.get("can_collect") else
                "일별 분석 미지원 · 월간 자료 또는 별도 검증 대기" if not row.get("daily_analysis_eligible", True) else
                "저장 데이터 없음 · 데이터 수집 화면에서 먼저 수집하세요" if not count else ""
            )
            row.update(stored_observations=count, vector_available=not bool(reason), vector_unavailable_reason=reason)
        if krx_ready:
            vkospi = next((r for r in items if r["id"] == "control-11"), None)
            if vkospi:
                items.remove(vkospi)
                at = next((i for i, r in enumerate(items) if r["id"] == "market-01"), 0)
                items.insert(at, vkospi)
        return {
            "items": items,
            "total": len(items),
            "collectable": sum(item["can_collect"] for item in items),
            "collected": len(set(successes) - excluded),
            "excluded": len(excluded),
            "note": "활성 후보의 정의·접근 조건 점검입니다. 실수집 성공은 별도 상태입니다.",
        }

    @router.get("/api/dimensions/{dimension_id}/series")
    def series(
        dimension_id: str,
        start: date = Query(date(2026, 1, 1)),
        end: date | None = Query(None),
        user=Depends(current_user),
        db=Depends(db_session),
    ):
        dimension = dimension_or_404(dimension_id)
        if dimension_id in excluded_ids(db, user):
            raise HTTPException(410, "활성 목록에서 제외된 차원입니다. 기존 수집 이력은 보존됩니다.")
        end = end or now().astimezone(ZoneInfo(config.timezone)).date()
        dates(start, end, config.timezone)
        value = series_view(db, user, dimension, start, end, config.timezone)
        value["sync_ranges"] = [
            [str(a), str(b)] for a, b in missing_ranges(db, user, dimension, start, end, config.timezone)
        ]
        return value

    @router.post("/api/dimensions/{dimension_id}/collect")
    def refresh(
        dimension_id: str, payload: CollectionRequest, user=Depends(current_user), db=Depends(db_session)
    ):
        dimension = dimension_or_404(dimension_id)
        if dimension_id in excluded_ids(db, user):
            raise HTTPException(410, "활성 목록에서 제외된 차원입니다.")
        dates(payload.start, payload.end, config.timezone)
        krx_dates(payload.start, payload.end)  # Fail before external effects for unsupported calendars.
        ranges = (
            [(payload.start, payload.end)]
            if payload.force
            else missing_ranges(db, user, dimension, payload.start, payload.end, config.timezone)
        )
        if user.role == "viewer":
            raise HTTPException(403, "조회 전용 사용자는 수집을 요청할 수 없습니다.")
        if not ranges:
            value = series_view(db, user, dimension, payload.start, payload.end, config.timezone)
            return {**value, "sync": {"mode": "cached", "fetched": None, "remaining": []}}
        fetch_start, fetch_end = ranges[0]
        batch = collect(
            db,
            user,
            dimension,
            fetch_start,
            fetch_end,
            transport,
            config,
            broker_transport,
            kiwoom_transport,
        )
        value = series_view(db, user, dimension, payload.start, payload.end, config.timezone)
        if dimension.get("provider") == "krx_vkospi":
            remaining = missing_ranges(db, user, dimension, payload.start, payload.end, config.timezone)
            return {**value, "sync": {"mode": "incremental", "fetched": [batch.value["points"][0]["date"], batch.value["scanned_through"]], "remaining": [[str(a), str(b)] for a, b in remaining]}}
        return {
            **value,
            "sync": {
                "mode": "force" if payload.force else "incremental",
                "fetched": [str(fetch_start), str(fetch_end)],
                "remaining": [[str(a), str(b)] for a, b in ranges[1:]],
            },
        }

    return router
