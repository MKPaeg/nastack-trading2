"""Read-only projections of personal persisted analysis and confirmed sales."""

from datetime import date, datetime, time, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select

from .db import aware, local_time
from .models import AnalysisRun, NewsArticle, NewsRun, RealizedSale, VectorResult
from .research import public_url

KINDS = {"vector": VectorResult, "news": NewsRun, "research": AnalysisRun, "sale": RealizedSale}
KST = ZoneInfo("Asia/Seoul")


def pick(value, keys):
    return {key: value.get(key) for key in keys}


def item_view(kind, row, timezone):
    value = row.value if kind != "research" else (row.result or {})
    if kind == "vector":
        instrument = value.get("instrument", {})
        code = value.get("request", {}).get("code", instrument.get("code", ""))
        status = value.get("status", "insufficient_data")
        name = instrument.get("name", code)
        summary = f"자료 {value.get('data_cutoff', '미확인')} · 예측 대상 {value.get('today', '미확인')}"
    elif kind == "news":
        instrument = value.get("instrument") or {}
        code, name = instrument.get("code", ""), instrument.get("name", "전체 산업·뉴스")
        status = row.status
        summary = f"기사 {len(value.get('article_ids', []))}개 · 평가 {len(value.get('assessments', []))}건"
        if value.get("mode") == "collect":
            summary = "수집 전용 · " + summary
    elif kind == "sale":
        code, name, status = (
            row.code,
            value.get("name", row.code),
            "confirmed" if row.profit is not None else "missing_cost",
        )
        summary = f"매도 체결 {row.quantity}주 · 수수료·세금 제외"
    else:
        code, name, status = row.symbol, (row.instrument_snapshot or {}).get("name", row.symbol), row.status
        summary = value.get("summary", row.message)
    return {
        "id": row.id,
        "kind": kind,
        "code": code,
        "name": name,
        "status": status,
        "simulation": kind == "research" and row.kind != "research",
        "created_at": local_time(row.updated_at if kind == "sale" else row.created_at, timezone),
        "trade_day": str(row.trade_day) if kind == "sale" else None,
        "summary": summary,
        "profit": str(row.profit) if kind == "sale" and row.profit is not None else None,
    }


def detail_view(db, user, kind, row, timezone):
    result = {"item": item_view(kind, row, timezone)}
    value = row.value if kind != "research" else (row.result or {})
    if kind == "vector":
        result["data"] = pick(
            value,
            [
                "request",
                "data_cutoff",
                "today",
                "summary",
                "comparison",
                "horizon_results",
                "dimensions",
                "features",
                "construction",
                "warnings",
                "schema_version",
            ],
        )
    elif kind == "news":
        article_ids = value.get("article_ids", [])
        articles = db.scalars(
            select(NewsArticle).where(
                NewsArticle.owner_id == user.id,
                NewsArticle.tenant_id == user.tenant_id,
                NewsArticle.id.in_(article_ids),
            )
        ).all()
        result["articles"] = [
            {
                "id": article.id,
                **pick(article.value, ["title", "excerpt", "published_at"]),
                "url": public_url(article.value.get("url", "")),
            }
            for article in articles
        ]
        allowed = {a.id for a in articles}
        result["data"] = {
            **pick(value, ["model", "provider", "mode", "prompt_version", "pending_articles", "error"]),
            "prompt_sha256": (value.get("system_prompt") or {}).get("sha256"),
            "assessments": [
                pick(a, ["article_id", "dimension_id", "score9", "reason", "evidence", "relevance"])
                for a in value.get("assessments", [])
                if a.get("article_id") in allowed
            ],
        }
    elif kind == "sale":
        result["data"] = {
            "broker": row.broker,
            "quantity": row.quantity,
            "trade_day": str(row.trade_day),
            "proceeds": str(row.proceeds),
            "cost": str(row.cost) if row.cost is not None else None,
            "profit": str(row.profit) if row.profit is not None else None,
            **pick(value, ["reason", "purchase_price", "fill_price", "cost_status", "fees_tax"]),
        }
    else:
        result["data"] = {
            **pick(value, ["summary", "outlook", "risks", "model", "evidence_ids"]),
            "settings_version": row.settings_version,
            "attempts": row.attempts,
            "error_code": row.error_code,
            "evidence": [
                {
                    **pick(e, ["id", "title", "snippet", "disposition", "published_at", "retrieved_at"]),
                    "url": public_url(e.get("url", "")),
                }
                for e in value.get("evidence", [])
            ],
        }
    return result


def reports_router(current_user, db_session, config):
    router = APIRouter(prefix="/api/reports")

    @router.get("")
    def history(
        start: date,
        end: date,
        kind: Literal["all", "vector", "news", "research", "sale"] = "all",
        code: str = Query("", pattern=r"^(\d{6})?$"),
        include_demo: bool = False,
        page: int = Query(1, ge=1, le=1000),
        page_size: int = Query(10, ge=5, le=50),
        user=Depends(current_user),
        db=Depends(db_session),
    ):
        if start > end or end == date.max or (end - start).days > 3660:
            raise HTTPException(422, "조회 기간은 시작일 ≤ 종료일, 최대 10년입니다.")
        # SQLite drops offsets: bind UTC for consistent boundaries on both databases.
        lower = datetime.combine(start, time.min, KST).astimezone(timezone.utc)
        upper = datetime.combine(end + timedelta(days=1), time.min, KST).astimezone(timezone.utc)
        counts, candidates = {}, []
        for label, model in KINDS.items():
            scope = [model.owner_id == user.id, model.tenant_id == user.tenant_id]
            stamp = model.updated_at if label == "sale" else model.created_at
            if label == "sale":
                scope += [
                    model.trade_day.between(start, end),
                    model.value["excluded_at"].as_string().is_(None),
                ]
            else:
                scope += [stamp >= lower, stamp < upper]
            if label == "research" and not include_demo:
                scope.append(model.kind == "research")
            if code:
                field = {
                    "vector": lambda: model.value["request"]["code"].as_string(),
                    "news": lambda: model.value["instrument"]["code"].as_string(),
                    "research": lambda: model.symbol,
                    "sale": lambda: model.code,
                }[label]()
                scope.append(field == code)
            counts[label] = db.scalar(select(func.count()).select_from(model).where(*scope))
            if kind in {"all", label}:
                rows = db.scalars(
                    select(model)
                    .where(*scope)
                    .order_by(stamp.desc(), model.id.desc())
                    .limit(page * page_size)
                ).all()
                candidates.extend(
                    (aware(getattr(row, "updated_at" if label == "sale" else "created_at")), label, row)
                    for row in rows
                )
        candidates.sort(key=lambda entry: (entry[0], entry[1], entry[2].id), reverse=True)
        offset = (page - 1) * page_size
        return {
            "items": [
                item_view(label, row, config.timezone)
                for _, label, row in candidates[offset : offset + page_size]
            ],
            "counts": counts,
            "total": sum(counts.values()) if kind == "all" else counts[kind],
            "page": page,
            "page_size": page_size,
        }

    @router.get("/{kind}/{report_id}")
    def detail(
        kind: Literal["vector", "news", "research", "sale"],
        report_id: str,
        user=Depends(current_user),
        db=Depends(db_session),
    ):
        model = KINDS[kind]
        row = db.scalar(
            select(model).where(
                model.id == report_id, model.owner_id == user.id, model.tenant_id == user.tenant_id
            )
        )
        if not row or (kind == "sale" and row.value.get("excluded_at")):
            raise HTTPException(404, "본인의 저장된 리포트를 찾을 수 없습니다.")
        return detail_view(db, user, kind, row, config.timezone)

    return router
