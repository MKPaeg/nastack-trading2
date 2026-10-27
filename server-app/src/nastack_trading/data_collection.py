"""Collection control plane. No orders, LLM evaluation or implicit paid planning.

Public inputs are reusable across one owner's stocks, never across owners. Publication
windows below are conservative polling policies, NOT point-in-time release guarantees.
"""

import logging
import threading
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, update

from .db import aware, now
from .dimension_catalog import BY_ID
from .dimensions import collect, excluded_ids, krx_dates, missing_ranges, scoped
from .live_worker import leader
from .models import (
    CollectionWorkflow,
    DataCollectionPolicy,
    DataCollectionTask,
    DimensionCollection,
    StatisticSeries,
    User,
)
from .security import audit
from .statistics import SERIES, refresh_statistics

KST = ZoneInfo("Asia/Seoul")
DEFAULTS = {
    "enabled": True,
    "dimensions_enabled": True,
    "statistics_enabled": True,
    "news_enabled": False,
    "news_hours": 12,
    "dimension_daily_limit": 20,
    "statistics_daily_limit": 4,
    "search_daily_limit": 10,
}


class PolicyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=0)
    enabled: bool
    dimensions_enabled: bool
    statistics_enabled: bool
    news_enabled: bool
    news_hours: int = Field(ge=1, le=168)
    dimension_daily_limit: int = Field(ge=0, le=200)
    statistics_daily_limit: int = Field(ge=0, le=30)
    search_daily_limit: int = Field(ge=0, le=100)


def policy(db, user):
    row = db.get(DataCollectionPolicy, user.id)
    if row and row.tenant_id != user.tenant_id:
        raise HTTPException(404, "본인의 수집 설정이 아닙니다.")
    return {**DEFAULTS, **(row.value.get("settings", {}) if row else {})}


def charge(db, user, kind):
    """Reserve before external I/O, including failures. No secrets or billing estimates.

    Counts collector attempts/search requests, not provider pagination or invoice USD.
    This is shared by manual and scheduled paths; cached reads do not consume it.
    """
    expected_tenant = user.tenant_id
    db.execute(update(User).where(User.id == user.id).values(active=User.active))
    current = db.get(User, user.id, populate_existing=True)
    if not current or current.tenant_id != expected_tenant or not current.active or current.role == "viewer":
        db.rollback()
        raise HTTPException(403, "수집 권한이 변경되었습니다.")
    row = db.get(DataCollectionPolicy, user.id, populate_existing=True)
    settings = policy(db, current)
    day = str(now().astimezone(KST).date())
    usage = dict(row.value.get("usage", {})) if row else {}
    if usage.get("date") != day:
        usage = {"date": day}
    key = {
        "dimension": "dimension_daily_limit",
        "statistics": "statistics_daily_limit",
        "search": "search_daily_limit",
    }[kind]
    if usage.get(kind, 0) >= settings[key]:
        db.rollback()
        retry_at = datetime.combine(now().astimezone(KST).date() + timedelta(days=1), datetime.min.time(), KST)
        raise HTTPException(429, "오늘의 수집 시도 한도에 도달했습니다. 데이터 수집 화면에서 확인하세요.",
                            headers={"X-Collection-Limit": kind, "Retry-After": str(max(1, int((retry_at - now()).total_seconds())))})
    usage[kind] = usage.get(kind, 0) + 1
    if not row:
        row = DataCollectionPolicy(owner_id=user.id, tenant_id=user.tenant_id, value={})
        db.add(row)
    row.value = {**row.value, "usage": usage}
    db.commit()


def statistic_interval(key):
    # Monthly releases: weekly polling, annual releases: monthly polling. Historical
    # revisions are checked too. Exact provider release calendars aren't assumed.
    return timedelta(days=7 if SERIES[key]["frequency"] == "월간" else 30)


def statistic_due(row):
    announced = row.value.get("next_release_date")
    if announced:
        try:
            released = date.fromisoformat(announced)
            return max(
                aware(row.updated_at) + timedelta(days=1),
                datetime.combine(released + timedelta(days=1), datetime.min.time(), KST).replace(hour=9),
            )
        except (ValueError, TypeError):
            pass
    return aware(row.updated_at) + statistic_interval(row.series_id)


def dimension_target(dimension, at):
    local = at.astimezone(KST)
    domestic = dimension.get("provider") in {"namuh_etf", "kiwoom_index", "ecos"}
    # Domestic close after 16:15; overseas prior observation after 09:00 KST.
    offset = 0 if domestic and (local.hour, local.minute) >= (16, 15) else 1
    if not domestic and local.hour < 9:
        offset = 2
    return local.date() - timedelta(days=offset)


def resources(db, user):
    settings = policy(db, user)
    excluded = excluded_ids(db, user)
    recent = (
        select(
            DimensionCollection.id,
            func.row_number()
            .over(
                partition_by=DimensionCollection.dimension_id,
                order_by=(DimensionCollection.created_at.desc(), DimensionCollection.id.desc()),
            )
            .label("rank"),
        )
        .where(
            DimensionCollection.owner_id == user.id,
            DimensionCollection.tenant_id == user.tenant_id,
            DimensionCollection.status == "completed",
        )
        .subquery()
    )
    batches = db.scalars(
        select(DimensionCollection)
        .join(recent, recent.c.id == DimensionCollection.id)
        .where(recent.c.rank == 1)
        .order_by(DimensionCollection.created_at)
    ).all()
    dims = {r.dimension_id: r for r in batches if r.dimension_id in BY_ID and r.dimension_id not in excluded}
    result = []
    for key, row in dims.items():
        definition = BY_ID[key]
        if not definition["can_collect"] or definition.get("manual_only"):
            continue
        # Update stored dimensions once per release window. Do not discover/fetch
        # all 95 candidates simply because a user visits a stock.
        target = dimension_target(definition, now())
        last = aware(row.created_at).astimezone(KST)
        domestic = definition.get("provider") in {"namuh_etf", "kiwoom_index", "ecos"}
        next_day = last.date() + timedelta(days=1)
        if domestic:
            sessions = krx_dates(next_day, next_day + timedelta(days=20))
            if sessions:
                next_day = date.fromisoformat(sessions[0])
        else:
            while next_day.weekday() in {0, 6}:
                next_day += timedelta(days=1)
        next_at = datetime.combine(next_day, datetime.min.time(), KST).replace(
            hour=16 if domestic else 9,
            minute=15 if domestic else 0,
        )
        result.append(
            {
                "resource": "dimension:" + key,
                "kind": "dimension",
                "id": key,
                "name": definition["name"],
                "enabled": settings["enabled"] and settings["dimensions_enabled"],
                "last_at": row.created_at,
                "latest_observation": max(
                    (p["date"] for p in row.value.get("points", []) if p.get("value") is not None),
                    default=None,
                ),
                "due_at": next_at,
                "target": str(target),
                "cadence": "일별 · 국내 마감 후 / 해외 다음날 오전 · 최근 7일 수정·결측 재점검",
            }
        )
    for row in db.scalars(
        select(StatisticSeries).where(
            StatisticSeries.owner_id == user.id,
            StatisticSeries.tenant_id == user.tenant_id,
        )
    ):
        if row.series_id not in SERIES:
            continue
        key = row.series_id
        result.append(
            {
                "resource": "statistics:" + key,
                "kind": "statistics",
                "id": key,
                "name": SERIES[key]["name"],
                "enabled": settings["enabled"] and settings["statistics_enabled"],
                "last_at": row.updated_at,
                "due_at": statistic_due(row),
                "cadence": "월간 발표·수정 확인 주 1회"
                if SERIES[key]["frequency"] == "월간"
                else "연간 발표·수정 확인 월 1회",
            }
        )
    from .vectors import owned_instruments

    owned = {r["code"] for r in owned_instruments(db, user)}
    for row in db.scalars(
        select(CollectionWorkflow).where(
            CollectionWorkflow.owner_id == user.id,
            CollectionWorkflow.tenant_id == user.tenant_id,
        )
    ):
        if row.code not in owned or not row.value.get("plan"):
            continue
        checked = row.value.get("collected_at")
        last = aware(datetime.fromisoformat(checked)) if checked else aware(row.updated_at)
        result.append(
            {
                "resource": "news:" + row.code,
                "kind": "news",
                "id": row.code,
                "name": row.code + " · 저장 수집 계획",
                "enabled": settings["enabled"] and settings["news_enabled"],
                "last_at": last,
                "due_at": last + timedelta(hours=settings["news_hours"]),
                "cadence": f"{settings['news_hours']}시간 · 저장 계획만 · LLM 호출 없음",
            }
        )
    tasks = {
        r.resource: r
        for r in db.scalars(
            select(DataCollectionTask).where(
                DataCollectionTask.owner_id == user.id,
                DataCollectionTask.tenant_id == user.tenant_id,
            )
        )
    }
    for item in result:
        task = tasks.get(item["resource"])
        if task:
            item.update(
                status=task.status, error=task.value.get("error"), history=task.value.get("history", [])
            )
            item["due_at"] = max(aware(item["due_at"]), aware(task.next_at))
        item["due"] = aware(item["due_at"]) <= now()
    return result


def freshness(db, user, dimensions, start, end, timezone):
    items = []
    for key in dimensions:
        if key not in BY_ID:
            continue
        ranges = missing_ranges(db, user, BY_ID[key], start, end, timezone)
        rows = scoped(db, user, key, start, end)
        rows = [r for r in rows if r.value.get("series_code") == BY_ID[key]["series_code"]
                and r.value.get("provider", "fred") == BY_ID[key].get("provider")]
        latest = max((aware(r.created_at) for r in rows if r.status == "completed"), default=None)
        task = db.get(DataCollectionTask, (user.id, "dimension:" + key))
        if task and task.tenant_id == user.tenant_id and task.status == "completed":
            latest = max(latest or aware(task.updated_at), aware(task.updated_at))
        # Stored coverage and a recent check are independent properties.
        stale = not latest or latest + timedelta(days=2) < now()
        items.append(
            {
                "id": key,
                "missing_ranges": [[str(a), str(b)] for a, b in ranges],
                "name": BY_ID[key]["name"],
                "latest_observation": max((p["date"] for r in rows if r.status == "completed" for p in r.value.get("points", []) if p.get("value") is not None), default=None),
                "last_checked_at": latest.isoformat() if latest else None,
                "stale": stale,
            }
        )
    return {
        "checked_at": now().isoformat(),
        "network_called": False,
        "items": items,
        "needs_refresh": any(r["stale"] or r["missing_ranges"] for r in items),
        "note": "저장 자료 최신성 점검 · 부족·지연 자료는 데이터 수집 화면에서 확인. 공개시각 검증을 뜻하지 않습니다.",
    }


class DataCollector:
    def __init__(
        self,
        config,
        engine,
        factory,
        dimension_transport=None,
        broker_transport=None,
        kiwoom_transport=None,
        news_transport=None,
    ):
        self.config, self.engine, self.factory = config, engine, factory
        self.transports = (dimension_transport, broker_transport, kiwoom_transport, news_transport)
        self.stop_event, self.thread = threading.Event(), None

    def start(self):
        self.thread = threading.Thread(target=self.loop, daemon=True, name="data-collector")
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)

    def loop(self):
        while not self.stop_event.wait(60):
            try:
                self.tick()
            except Exception:
                logging.getLogger(__name__).warning("Data collector tick failed; retry deferred")

    def tick(self):
        # Different lock from order runner; one collector across server processes.
        with leader(self.engine, 2026102403) as acquired:
            if not acquired:
                return
            with self.factory() as db:
                users = db.scalars(select(User).where(User.active.is_(True), User.role != "viewer")).all()
                candidates = []
                for user in users:
                    if policy(db, user)["enabled"]:
                        candidates.extend(
                            (r, user.id, user.tenant_id)
                            for r in resources(db, user)
                            if r["enabled"] and r["due"]
                        )
            # Oldest due first, including across users; bounded to one resource/tick.
            if candidates:
                item, owner, tenant = min(candidates, key=lambda r: aware(r[0]["due_at"]))
                self.perform(item, owner, tenant)

    def perform(self, item, owner, tenant):
        dim_transport, broker_transport, kiwoom_transport, news_transport = self.transports
        with self.factory() as db:
            db.execute(
                update(User).where(User.id == owner, User.tenant_id == tenant).values(active=User.active)
            )
            user = db.get(User, owner)
            if not user or user.tenant_id != tenant or not user.active or user.role == "viewer":
                return
            settings = policy(db, user)
            if (
                not settings["enabled"]
                or not settings[
                    {
                        "dimension": "dimensions_enabled",
                        "statistics": "statistics_enabled",
                        "news": "news_enabled",
                    }[item["kind"]]
                ]
            ):
                return
            task = db.get(DataCollectionTask, (owner, item["resource"]))
            if task and aware(task.next_at) > now():
                return
            if not task:
                task = DataCollectionTask(owner_id=owner, tenant_id=tenant, resource=item["resource"])
                db.add(task)
            if task.tenant_id != tenant:
                return
            # Reserve a crash-safe interval before I/O. Unknown attempts aren't
            # immediately replayed on restart. All errors are sanitized below.
            task.status, task.next_at, task.updated_at = "running", now() + timedelta(hours=6), now()
            task.value = {**(task.value or {}), "error": None}
            db.commit()
            try:
                if item["kind"] == "dimension":
                    definition = BY_ID[item["id"]]
                    if item["id"] in excluded_ids(db, user):
                        raise HTTPException(410, "excluded")
                    end = dimension_target(definition, now())
                    rows = scoped(db, user, item["id"], date(2000, 1, 1), end)
                    start = min((r.start_date for r in rows), default=end - timedelta(days=365))
                    gaps = missing_ranges(db, user, definition, start, end, self.config.timezone)
                    # One bounded request: extend first; otherwise recent revisions
                    # and interior nulls. Never repeatedly backfill national holidays.
                    revision_at = task.value.get("revision_checked_at")
                    revision_due = (
                        not revision_at
                        or aware(datetime.fromisoformat(revision_at)) + timedelta(days=7) <= now()
                    )
                    if gaps or revision_due:
                        begin, finish = gaps[0] if gaps else (max(start, end - timedelta(days=7)), end)
                        if revision_due and begin >= end - timedelta(days=7):
                            begin = max(start, end - timedelta(days=7))
                        finish = min(finish, begin + timedelta(days=365))
                        collect(
                            db,
                            user,
                            definition,
                            begin,
                            finish,
                            dim_transport,
                            self.config,
                            broker_transport,
                            kiwoom_transport,
                        )
                        if not gaps or (revision_due and finish == end and begin <= end - timedelta(days=7)):
                            task.value = {**task.value, "revision_checked_at": now().isoformat()}
                    delay = timedelta(days=1)
                elif item["kind"] == "statistics":
                    refresh_statistics(item["id"], user, db, self.config, news_transport)
                    series = db.get(StatisticSeries, (owner, item["id"]))
                    delay = max(timedelta(days=1), statistic_due(series) - now())
                else:
                    from .collection_pipeline import execute, inputs

                    workflow = db.get(CollectionWorkflow, (owner, item["id"]))
                    busy = db.scalar(
                        select(CollectionWorkflow).where(
                            CollectionWorkflow.owner_id == owner,
                            CollectionWorkflow.tenant_id == tenant,
                            CollectionWorkflow.status.in_(["running", "queued"]),
                        )
                    )
                    if (
                        busy
                        or not workflow
                        or workflow.tenant_id != tenant
                        or workflow.status in {"running", "queued"}
                    ):
                        raise HTTPException(409, "busy")
                    data = inputs(db, user, item["id"], self.config, True)
                    if workflow.value.get("plan_signature") != data["plan_signature"]:
                        raise HTTPException(409, "plan_changed")
                    from uuid import uuid4

                    token = str(uuid4())
                    workflow.status = "queued"
                    workflow.value = {
                        **workflow.value,
                        "token": token,
                        "signature": data["signature"],
                        "mode": "collect",
                        "error": None,
                    }
                    db.commit()
                    execute(
                        self.factory,
                        self.config,
                        owner,
                        tenant,
                        item["id"],
                        token,
                        True,
                        news_transport,
                        mode="collect",
                        allow_plan=False,
                    )
                    db.expire_all()
                    if db.get(CollectionWorkflow, (owner, item["id"])).status == "failed":
                        raise HTTPException(409, "collection_failed")
                    delay = timedelta(hours=settings["news_hours"])
                state, error = "completed", None
            except Exception as exc:
                db.rollback()
                state = "failed"
                error = (
                    f"REQUEST_REJECTED_{exc.status_code}"
                    if isinstance(exc, HTTPException)
                    else "COLLECTION_FAILED"
                )
                delay = timedelta(hours=12)
                if isinstance(exc, HTTPException) and (exc.headers or {}).get("X-Collection-State") == "not_published":
                    error = "SOURCE_NOT_PUBLISHED"
            task = db.get(DataCollectionTask, (owner, item["resource"]))
            history = task.value.get("history", [])[-19:] + [
                {"at": now().isoformat(), "status": state, "error": error}
            ]
            task.status, task.updated_at, task.next_at = state, now(), now() + delay
            task.value = {**task.value, "error": error, "history": history}
            audit(db, user, "data_collection." + state, item["resource"], {"error": error})
            db.commit()


def data_collection_router(current_user, db_session):
    router = APIRouter(prefix="/api/data-collection")

    @router.get("")
    def status(user=Depends(current_user), db=Depends(db_session)):
        row = db.get(DataCollectionPolicy, user.id)
        settings = policy(db, user)
        return {
            "version": row.version if row else 0,
            "settings": settings,
            "usage": row.value.get("usage", {}) if row else {},
            "resources": resources(db, user),
            "note": "수집 시도 한도는 실제 청구액·페이지별 API 호출 수가 아닙니다. 실패도 한도에 포함하며 자동 LLM 호출은 없습니다. 공개시각이 미검증인 원천은 보수적 점검 주기를 사용합니다.",
        }

    @router.put("")
    def save(payload: PolicyUpdate, user=Depends(current_user), db=Depends(db_session)):
        if not user.active or user.role == "viewer":
            raise HTTPException(403, "조회 전용입니다.")
        tenant = user.tenant_id
        db.execute(update(User).where(User.id == user.id).values(active=User.active))
        user = db.get(User, user.id, populate_existing=True)
        if not user or user.tenant_id != tenant or not user.active or user.role == "viewer":
            raise HTTPException(403, "수집 설정 변경 권한이 바뀌었습니다.")
        row = db.get(DataCollectionPolicy, user.id, populate_existing=True)
        policy(db, user)  # tenant validation
        if payload.version != (row.version if row else 0):
            raise HTTPException(409, "다른 화면에서 설정이 변경되었습니다. 다시 조회하세요.")
        if not row:
            row = DataCollectionPolicy(owner_id=user.id, tenant_id=user.tenant_id, version=0, value={})
            db.add(row)
        row.value = {**row.value, "settings": payload.model_dump(exclude={"version"})}
        row.version += 1
        row.updated_at = now()
        audit(db, user, "data_collection.settings", user.id, {"version": row.version})
        db.commit()
        return status(user, db)

    return router
