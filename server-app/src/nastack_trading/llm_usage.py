"""Request/task-local numeric usage only; no prompts, keys or cross-owner cache."""
import logging
import re
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime
from functools import wraps
from uuid import uuid4
from zoneinfo import ZoneInfo

from .model_pricing import PRICING_SOURCE, ModelPrice, current_model_price

_ledger = ContextVar("llm_usage_ledger", default=None)
_category = ContextVar("llm_usage_category", default="evaluation")


@contextmanager
def usage_scope(category="evaluation"):
    ledger = []
    token = _ledger.set(ledger)
    category_token = _category.set(category)
    try:
        yield ledger
    finally:
        _category.reset(category_token)
        _ledger.reset(token)


@contextmanager
def usage_category(category):
    if category not in {"collection", "evaluation"}:
        raise ValueError("unknown LLM usage category")
    token = _category.set(category)
    try:
        yield
    finally:
        _category.reset(token)


def tracked_usage(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with usage_scope() as ledger:
            try:
                return function(*args, **kwargs)
            finally:
                if ledger and len(args) >= 3:
                    try:
                        from .llm_usage_history import save_usage
                        from .models import User

                        factory, owner_id = args[0], args[2]
                        with factory() as db:
                            user = db.get(User, owner_id)
                            tenant_id = user.tenant_id if user else None
                        if tenant_id:
                            save_usage(factory, owner_id, tenant_id, ledger)
                    except Exception:
                        logging.getLogger(__name__).warning("LLM usage history could not be saved")
    return wrapped


def count(value):
    return value if type(value) is int and 0 <= value <= 10**10 else None


def begin_call(provider, payload):
    ledger = _ledger.get()
    if ledger is None or provider not in {"openai", "local_model"} or not payload.get("model"):
        return None
    model = str(payload["model"])
    options = payload.get("prompt_cache_options") or {}
    entry = {"provider": provider, "model": model if re.fullmatch(r"[\w.:-]{1,120}", model) else "unknown",
             "category": _category.get(), "started_at": datetime.now(ZoneInfo("UTC")).isoformat(), "event_id": str(uuid4()),
             "cache_write_ttl": options.get("ttl", "30m") if isinstance(options, dict) else "unknown"}
    ledger.append(entry)
    return entry


def finish_call(entry, response):
    if entry is None or not isinstance(response, dict):
        return
    entry["completed_at"] = datetime.now(ZoneInfo("UTC")).isoformat()
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return
    incoming = count(usage.get("input_tokens", usage.get("prompt_tokens")))
    outgoing = count(usage.get("output_tokens", usage.get("completion_tokens")))
    total = count(usage.get("total_tokens"))
    details = usage.get("input_tokens_details", usage.get("prompt_tokens_details")) or {}
    details = details if isinstance(details, dict) else {}
    cached = count(details.get("cached_tokens"))
    writes = count(details.get("cache_write_tokens", usage.get("cache_write_tokens")))
    if total is None and incoming is not None and outgoing is not None:
        total = incoming + outgoing
    entry.update(input_tokens=incoming, output_tokens=outgoing, total_tokens=total,
                 cached_input_tokens=cached, cache_write_tokens=writes,
                 service_tier=response.get("service_tier", "default") in {"default", "standard", None})


def _estimate_with_price(entry, price: ModelPrice | None):
    if not price:
        return None
    incoming, outgoing = entry.get("input_tokens"), entry.get("output_tokens")
    cached, written = entry.get("cached_input_tokens"), entry.get("cache_write_tokens")
    # Long context, non-default cache lifetimes and Fast/Flex/Batch require separate pricing.
    if (incoming is None or outgoing is None or cached is None or written is None or incoming > 272000
            or cached + written > incoming or entry.get("cache_write_ttl", "30m") != "30m"
            or not entry.get("service_tier") or (cached and price.cached_input_usd is None)
            or (written and price.cache_write_30m_usd is None)):
        return None
    if entry.get("total_tokens") != incoming + outgoing:
        return None
    return ((incoming-cached-written)*price.input_usd + cached*(price.cached_input_usd or 0)
            + written*(price.cache_write_30m_usd or 0)
            + outgoing*price.output_usd)/1_000_000


def estimate(entry, today: date | None = None):
    if entry.get("provider") != "openai":
        return None
    return _estimate_with_price(entry, current_model_price(entry["model"], today))


def usage_snapshot(ledger=None):
    entries = _ledger.get() if ledger is None else ledger
    if entries is None:
        return None
    totals = {key: sum(e.get(key) or 0 for e in entries) for key in
              ("input_tokens", "output_tokens", "total_tokens", "cached_input_tokens", "cache_write_tokens")}
    today = datetime.now(ZoneInfo("Asia/Seoul")).date()
    prices = {model: current_model_price(model, today) for model in {
        e["model"] for e in entries if e.get("provider") == "openai"
    }}
    costs = [_estimate_with_price(e, prices.get(e["model"])) for e in entries]
    known = sum(e.get("total_tokens") is not None for e in entries)
    cache_write_known = all(e.get("cache_write_tokens") is not None for e in entries)
    priced = sum(c is not None for c in costs)
    groups = {}
    for entry, cost in zip(entries, costs):
        key = (entry["provider"], entry["model"])
        group = groups.setdefault(key, {"provider": key[0], "model": key[1], "llm_calls": 0,
                                        "reported_calls": 0, "priced_calls": 0, "input_tokens": 0,
                                        "output_tokens": 0, "cached_input_tokens": 0, "cache_write_tokens": 0,
                                        "total_tokens": 0,
                                        "estimated_usd": 0.0})
        group["llm_calls"] += 1
        group["reported_calls"] += entry.get("total_tokens") is not None
        group["priced_calls"] += cost is not None
        for field in ("input_tokens", "output_tokens", "cached_input_tokens", "cache_write_tokens", "total_tokens"):
            group[field] += entry.get(field) or 0
        group["estimated_usd"] += cost or 0
    model_usage = []
    for group in groups.values():
        group["estimated_usd"] = round(group["estimated_usd"], 10)
        group["cost_known"] = group["priced_calls"] == group["llm_calls"]
        model_usage.append(group)
    model_usage.sort(key=lambda group: (group["provider"], group["model"]))
    return {**totals, "llm_calls": len(entries), "reported_calls": known,
            "unreported_calls": len(entries)-known, "priced_calls": priced,
            "known": known == len(entries), "cache_write_known": cache_write_known,
            "cost_known": priced == len(entries),
            "estimated_usd": round(sum(c or 0 for c in costs), 10),
            "model_usage": model_usage,
            "pricing_as_of": str(today) if any(price is not None for price in prices.values()) else None,
            "pricing_source": PRICING_SOURCE,
            "pricing_models": {model: {"source": price.source, "checked_on": str(price.checked_on)}
                               for model, price in prices.items() if price},
            "pricing_valid_until": None,
            "pricing_basis": "Standard speed, short context, text tokens and default 30m cache writes only",
            "pricing_refresh": "Official model page checked on first use each Seoul calendar day",
            "currency": "USD", "scope": "LLM only; taxes, search, tools and infrastructure excluded"}


def usage_mark():
    return len(_ledger.get() or [])


def usage_since(mark):
    entries = _ledger.get()
    return usage_snapshot(entries[mark:]) if entries is not None else None
