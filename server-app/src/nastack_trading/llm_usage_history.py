"""Private, per-call LLM usage ledger and Seoul-calendar summaries."""

from collections import deque
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select

from .db import aware, now
from .llm_usage import _estimate_with_price
from .model_pricing import current_model_price
from .models import LLMUsageRecord, User

KST = ZoneInfo("Asia/Seoul")


def save_usage(factory, owner_id: str, tenant_id: str, entries: list[dict]) -> int:
    """Persist only numeric usage and model metadata from one authenticated execution."""
    if not entries:
        return 0
    with factory() as db:
        user = db.get(User, owner_id)
        if not user or user.tenant_id != tenant_id:
            return 0
        for entry in entries:
            stamp = entry.get("completed_at") or entry.get("started_at") or now()
            if isinstance(stamp, str):
                stamp = datetime.fromisoformat(stamp)
            stamp = aware(stamp)
            day = stamp.astimezone(KST).date()
            price = current_model_price(entry["model"], day) if entry["provider"] == "openai" else None
            cost = _estimate_with_price(entry, price)
            db.add(LLMUsageRecord(
                **({"id": entry["event_id"]} if entry.get("event_id") else {}),
                owner_id=owner_id, tenant_id=tenant_id, local_day=day, occurred_at=stamp,
                category=entry.get("category") if entry.get("category") in {"collection", "evaluation"} else "evaluation",
                provider=entry["provider"], model=entry["model"],
                input_tokens=entry.get("input_tokens"), output_tokens=entry.get("output_tokens"),
                cached_input_tokens=entry.get("cached_input_tokens"),
                cache_write_tokens=entry.get("cache_write_tokens"), total_tokens=entry.get("total_tokens"),
                estimated_usd=Decimal(str(cost)) if cost is not None else None,
                pricing_checked_on=price.checked_on if price else None,
            ))
        db.commit()
    return len(entries)


def period_bounds(period: str, anchor: date) -> tuple[date, date]:
    if period == "day":
        return anchor, anchor
    if period == "week":
        start = anchor - timedelta(days=anchor.weekday())
        return start, start + timedelta(days=6)
    if period == "month":
        start = anchor.replace(day=1)
        following = date(start.year + 1, 1, 1) if start.month == 12 else date(start.year, start.month + 1, 1)
        return start, following - timedelta(days=1)
    if period == "year":
        return date(anchor.year, 1, 1), date(anchor.year, 12, 31)
    raise ValueError("unsupported period")


def _empty():
    return {"calls": 0, "reported_calls": 0, "priced_calls": 0, "input_tokens": 0,
            "output_tokens": 0, "cached_input_tokens": 0, "cache_write_tokens": 0,
            "total_tokens": 0, "estimated_usd": Decimal(0)}


def _add(bucket, row):
    bucket["calls"] += 1
    bucket["reported_calls"] += row.total_tokens is not None
    bucket["priced_calls"] += row.estimated_usd is not None
    for field in ("input_tokens", "output_tokens", "cached_input_tokens", "cache_write_tokens", "total_tokens"):
        bucket[field] += getattr(row, field) or 0
    bucket["estimated_usd"] += row.estimated_usd or Decimal(0)


def _view(bucket):
    return {**bucket, "estimated_usd": f'{bucket["estimated_usd"]:.10f}',
            "unreported_calls": bucket["calls"] - bucket["reported_calls"],
            "unknown_cost_calls": bucket["calls"] - bucket["priced_calls"],
            "cost_known": bucket["calls"] == bucket["priced_calls"]}


def usage_report(db, user, period: str, anchor: date, model: str | None, category: str | None):
    start, end = period_bounds(period, anchor)
    query = select(LLMUsageRecord).where(
        LLMUsageRecord.owner_id == user.id, LLMUsageRecord.tenant_id == user.tenant_id,
        LLMUsageRecord.local_day >= start, LLMUsageRecord.local_day <= end,
    )
    if model:
        query = query.where(LLMUsageRecord.model == model)
    if category:
        query = query.where(LLMUsageRecord.category == category)
    overall = _empty()
    days, models, categories = {}, {}, {}
    recent = deque(maxlen=50)
    for row in db.scalars(query.order_by(LLMUsageRecord.occurred_at, LLMUsageRecord.id)).yield_per(500):
        _add(overall, row)
        _add(days.setdefault(str(row.local_day), _empty()), row)
        _add(models.setdefault((row.provider, row.model), _empty()), row)
        _add(categories.setdefault(row.category, _empty()), row)
        recent.append({"id": row.id, "occurred_at": aware(row.occurred_at).astimezone(KST).isoformat(),
                       "local_day": str(row.local_day), "category": row.category, "provider": row.provider,
                       "model": row.model, "input_tokens": row.input_tokens,
                       "output_tokens": row.output_tokens, "cached_input_tokens": row.cached_input_tokens,
                       "cache_write_tokens": row.cache_write_tokens, "total_tokens": row.total_tokens,
                       "estimated_usd": f"{row.estimated_usd:.10f}" if row.estimated_usd is not None else None,
                       "pricing_checked_on": str(row.pricing_checked_on) if row.pricing_checked_on else None})
    return {"period": period, "anchor": str(anchor), "start": str(start), "end": str(end),
            "timezone": "Asia/Seoul", "model_filter": model, "category_filter": category,
            "summary": _view(overall),
            "by_day": [{"date": day, **_view(value)} for day, value in sorted(days.items())],
            "by_model": [{"provider": provider, "model": model_id, **_view(value)}
                         for (provider, model_id), value in sorted(models.items())],
            "by_category": [{"category": kind, **_view(value)} for kind, value in sorted(categories.items())],
            "recent": list(reversed(recent)),
            "note": "실제 청구액이 아닌 호출 당시 확인된 LLM 단가의 USD 추정치입니다. 비용 미확인 호출은 소계에 포함하지 않으며, 검색·도구·세금·환율·서버 비용은 별도입니다. 이 기능 도입 이전 호출은 소급 집계하지 않습니다."}


def llm_usage_router(current_user, db_session):
    router = APIRouter(prefix="/api/llm-usage")

    @router.get("")
    def report(period: str = Query("day", pattern="^(day|week|month|year)$"),
               anchor: date | None = None, model: str | None = Query(None, max_length=120),
               category: str | None = Query(None, pattern="^(collection|evaluation)$"),
               user=Depends(current_user), db=Depends(db_session)):
        chosen = anchor or datetime.now(KST).date()
        return usage_report(db, user, period, chosen, model, category)

    return router
