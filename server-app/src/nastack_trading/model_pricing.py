"""Verified, day-scoped public OpenAI model prices; never guess an unknown rate."""

import re
from dataclasses import dataclass
from datetime import date, datetime
from html.parser import HTMLParser
from threading import Lock
from time import monotonic
from zoneinfo import ZoneInfo

import httpx

PRICING_SOURCE = "https://developers.openai.com/api/docs/pricing"
MODEL_SOURCE = "https://developers.openai.com/api/docs/models/"
_MODEL_ID = re.compile(r"[a-z0-9][a-z0-9.-]{1,119}\Z")
_MONEY = re.compile(r"\$([0-9][0-9,]*(?:\.[0-9]+)?)\Z")
_CACHE_WRITE_MULTIPLIER = re.compile(
    r"Cache writes are billed at (1\.25)x the uncached input token rate\."
)
_CACHE: dict[str, "ModelPrice"] = {}
_FAILED_UNTIL: dict[str, float] = {}
_LOCK = Lock()


@dataclass(frozen=True)
class ModelPrice:
    model: str
    input_usd: float
    cached_input_usd: float | None
    cache_write_30m_usd: float | None
    output_usd: float
    checked_on: date
    source: str


class _VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "template", "noscript"}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style", "template", "noscript"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden and data.strip():
            self.parts.append(" ".join(data.split()))


def parse_model_price(page: str, model: str, checked_on: date) -> ModelPrice:
    """Read the Standard text-token rates from an exact model's official page."""
    parser = _VisibleText()
    parser.feed(page)
    parts = parser.parts
    for index in range(len(parts)):
        if parts[index:index + 3] != ["Text tokens", "Per 1M tokens", "Input"]:
            continue
        if model not in parts[max(0, index - 100):index] or "Pricing" not in parts[max(0, index - 15):index]:
            continue
        fields = parts[index + 2:index + 10]
        if fields[0::2] == ["Input", "Cached input", "Cache writes", "Output"]:
            input_rate, cached_rate, write_rate, output_rate = fields[1::2]
        elif fields[:6][0::2] == ["Input", "Cached input", "Output"]:
            input_rate, cached_rate, output_rate = fields[:6][1::2]
            write_rate = None
        else:
            continue
        numbers = [_MONEY.fullmatch(value) if value is not None else None
                   for value in (input_rate, cached_rate, write_rate, output_rate)]
        if not numbers[0] or not numbers[3]:
            continue
        values = [float(match.group(1).replace(",", "")) if match else None for match in numbers]
        if not 0 < values[0] <= 1000 or not 0 < values[3] <= 1000:
            raise ValueError("official rate outside accepted range")
        # Some official model pages state the default 30-minute write rate as a
        # multiplier below the three-rate table rather than as a fourth row.
        if values[2] is None and _CACHE_WRITE_MULTIPLIER.search(" ".join(parts[index:index + 45])):
            values[2] = values[0] * 1.25
        return ModelPrice(model, values[0], values[1], values[2], values[3], checked_on, MODEL_SOURCE + model)
    raise ValueError("standard text-token rates not found on official price page")


def fetch_model_price(model: str, checked_on: date) -> ModelPrice:
    if not _MODEL_ID.fullmatch(model):
        raise ValueError("unsupported model identifier")
    url = MODEL_SOURCE + model
    with httpx.Client(timeout=5, follow_redirects=False, trust_env=False) as client:
        with client.stream("GET", url, headers={"Accept": "text/html"}) as response:
            response.raise_for_status()
            if "text/html" not in response.headers.get("content-type", ""):
                raise ValueError("official price page is not HTML")
            content = bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > 1_000_000:
                    raise ValueError("official price page is too large")
    return parse_model_price(content.decode("utf-8"), model, checked_on)


def current_model_price(model: str, today: date | None = None) -> ModelPrice | None:
    """Refresh on first use each Seoul calendar day; stale rates never price a call."""
    today = today or datetime.now(ZoneInfo("Asia/Seoul")).date()
    if not _MODEL_ID.fullmatch(model):
        return None
    with _LOCK:
        cached = _CACHE.get(model)
        if cached and cached.checked_on == today:
            return cached
        if monotonic() < _FAILED_UNTIL.get(model, 0):
            return None
        try:
            checked = fetch_model_price(model, today)
        except (httpx.HTTPError, UnicodeError, ValueError):
            _CACHE.pop(model, None)
            _FAILED_UNTIL[model] = monotonic() + 3600
            return None
        _CACHE[model] = checked
        _FAILED_UNTIL.pop(model, None)
        return checked
