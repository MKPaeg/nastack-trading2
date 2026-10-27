"""Request/task-local numeric usage only; no prompts, keys or cross-owner cache."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime
from functools import wraps
import re
from zoneinfo import ZoneInfo

SOURCE = "https://developers.openai.com/api/docs/pricing"
AS_OF = "2026-10-27"
# USD / million tokens, Standard speed, short context. No alias guessing.
RATES = {
    "gpt-5.6-sol": (4, .4, 20),
    "gpt-6.1-sol": (2, .1, 10),
    "gpt-6.1-terra": (1, .05, 5),
    "gpt-6-astra": (10, 1, 50),
    "gpt-6-luna": (.1, .01, .5),
}
_ledger = ContextVar("llm_usage_ledger", default=None)


@contextmanager
def usage_scope():
    ledger = []
    token = _ledger.set(ledger)
    try:
        yield ledger
    finally:
        _ledger.reset(token)


def tracked_usage(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with usage_scope():
            return function(*args, **kwargs)
    return wrapped


def count(value):
    return value if type(value) is int and 0 <= value <= 10**10 else None


def begin_call(provider, payload):
    ledger = _ledger.get()
    if ledger is None or provider not in {"openai", "local_model"} or not payload.get("model"):
        return None
    model = str(payload["model"])
    entry = {"provider": provider, "model": model if re.fullmatch(r"[\w.:-]{1,120}", model) else "unknown"}
    ledger.append(entry)
    return entry


def finish_call(entry, response):
    if entry is None or not isinstance(response, dict):
        return
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return
    incoming = count(usage.get("input_tokens", usage.get("prompt_tokens")))
    outgoing = count(usage.get("output_tokens", usage.get("completion_tokens")))
    total = count(usage.get("total_tokens"))
    details = usage.get("input_tokens_details", usage.get("prompt_tokens_details")) or {}
    details = details if isinstance(details, dict) else {}
    cached = count(details.get("cached_tokens"))
    writes = count(details.get("cache_write_tokens", usage.get("cache_write_tokens", 0)))
    if total is None and incoming is not None and outgoing is not None:
        total = incoming + outgoing
    entry.update(input_tokens=incoming, output_tokens=outgoing, total_tokens=total,
                 cached_input_tokens=cached, cache_write_tokens=writes,
                 service_tier=response.get("service_tier", "default") in {"default", "standard", None})


def estimate(entry, today=None):
    today = today or datetime.now(ZoneInfo("Asia/Seoul")).date()
    model = entry["model"]
    rates = RATES.get(model) if entry["provider"] == "openai" else None
    if not rates or today < date(2026, 10, 27) or today > date(2026, 11, 21):
        return None
    incoming, outgoing = entry.get("input_tokens"), entry.get("output_tokens")
    cached = entry.get("cached_input_tokens") or 0
    # Cache-write TTL/rates, long context, Fast/Flex/Batch require separate pricing.
    if incoming is None or outgoing is None or incoming > 272000 or cached > incoming or entry.get("cache_write_tokens") != 0 or not entry.get("service_tier"):
        return None
    if entry.get("total_tokens") != incoming + outgoing:
        return None
    return ((incoming-cached)*rates[0]+cached*rates[1]+outgoing*rates[2])/1_000_000


def usage_snapshot(ledger=None):
    entries = _ledger.get() if ledger is None else ledger
    if entries is None:
        return None
    totals = {key: sum(e.get(key) or 0 for e in entries) for key in
              ("input_tokens", "output_tokens", "total_tokens", "cached_input_tokens")}
    costs = [estimate(e) for e in entries]
    known = sum(e.get("total_tokens") is not None for e in entries)
    priced = sum(c is not None for c in costs)
    return {**totals, "llm_calls": len(entries), "reported_calls": known,
            "unreported_calls": len(entries)-known, "priced_calls": priced,
            "known": known == len(entries), "cost_known": priced == len(entries),
            "estimated_usd": round(sum(c or 0 for c in costs), 10),
            "pricing_as_of": AS_OF, "pricing_source": SOURCE,
            "pricing_valid_until": "2026-11-21", "pricing_basis": "Standard speed, short context, text tokens only",
            "currency": "USD", "scope": "LLM only; taxes, search, tools and infrastructure excluded"}


def usage_mark():
    return len(_ledger.get() or [])


def usage_since(mark):
    entries = _ledger.get()
    return usage_snapshot(entries[mark:]) if entries is not None else None
