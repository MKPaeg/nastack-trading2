"""Calendar window and transparent candidate relevance, not an impact model."""

import calendar
import re
import unicodedata
from datetime import timedelta


def news_start(day, days=14):
    return day - timedelta(days=days - 1)


def six_month_start(day):
    month_index = day.year * 12 + day.month - 1 - 6
    year, month = divmod(month_index, 12)
    return day.replace(year=year, month=month + 1, day=min(day.day, calendar.monthrange(year, month + 1)[1]))


TOPICS = {
    "디스플레이·OLED": [
        "디스플레이",
        "oled",
        "lcd",
        "display panel",
        "television",
        "tv demand",
        "tv sales",
        "tv shipments",
        "스마트폰",
        "smartphone",
        "노트북",
        "laptop",
        "태블릿",
        "tablet",
        "모니터",
        "차량용 디스플레이",
        "automotive display",
        "가전 디스플레이",
        "appliance display",
    ],
    "반도체": ["반도체", "semiconductor", "memory chip", "dram", "nand"],
    "반도체 장비·소재": ["반도체", "semiconductor", "wafer"],
    "가전·백색가전": ["가전", "appliance", "refrigerator", "washing machine"],
    "전자부품·IT하드웨어": ["전자부품", "electronic component", "mlcc", "camera module"],
    "자동차": ["자동차", "automotive", "vehicle"],
    "전기차": ["전기차", "electric vehicle", "battery electric"],
    "2차전지": ["2차전지", "이차전지", "배터리", "battery"],
    "배터리 소재": ["배터리", "양극재", "음극재", "battery", "cathode", "anode"],
    "재생에너지": ["재생에너지", "renewable", "solar", "wind generation"],
    "태양광": ["태양광", "solar"],
    "태양광·ESS": ["태양광", "solar", "energy storage"],
    "풍력": ["풍력", "wind generation", "wind turbine"],
    "수소·연료전지": ["수소", "hydrogen", "fuel cell"],
    "조선": ["조선", "shipbuilding", "shipyard"],
    "방산·항공우주": ["방산", "항공우주", "aerospace", "defense"],
    "건설": ["건설", "construction"],
    "로봇·자동화": ["로봇", "robot", "automation"],
    "구리": ["구리", "copper"],
    "금": ["금값", "gold price"],
    "은": ["은값", "silver price"],
    "니켈": ["니켈", "nickel"],
    "리튬": ["리튬", "lithium"],
    "화학": ["석유화학", "petrochemical"],
    "화장품·생활용품": ["화장품", "cosmetic", "personal care"],
}


def normalize(text):
    return unicodedata.normalize("NFKC", text).casefold()


def relevance(article, instrument):
    if not instrument:
        return None
    text = normalize(article.get("title", "") + " " + article.get("excerpt", ""))
    names = [normalize(instrument["name"])]
    if instrument["code"] == "005380":
        names.append("현대자동차")
    if article.get("content_type") == "disclosure":
        # A subsidiary, similarly named broker, or single-stock ETF is not
        # the issuer itself. Never use a substring in its filing as ownership.
        if article.get("instrument_code"):
            return (
                {"kind": "disclosure", "reason": "공시 기업 코드 일치", "verified": True}
                if article["instrument_code"] == instrument["code"]
                else None
            )
        issuer = re.sub(r"^\s*\[[^\]]+\]\s*", "", normalize(article.get("issuer") or "")).strip()
        if issuer:
            return (
                {"kind": "direct", "reason": "공시 발행사명 정확 일치 · 코드 미확인", "verified": False}
                if issuer in names
                else None
            )
        title = re.sub(r"^\s*\[[^\]]+\]\s*", "", normalize(article.get("title", ""))).strip()
        return (
            {"kind": "direct", "reason": "공시 제목의 발행사명 일치 · 코드 미확인", "verified": False}
            if any(re.match(re.escape(name) + r"(?=$|[\s·:])", title) for name in names if name)
            else None
        )
    if any(name and name in text for name in names):
        return {"kind": "direct", "reason": "종목명 직접 언급", "verified": False}
    if article.get("instrument_code") == instrument["code"]:
        return {"kind": "disclosure", "reason": "공시 기업 코드 일치", "verified": True}
    code = instrument["code"]
    if re.search(r"(?<!\d)" + re.escape(code) + r"(?!\d)", text):
        return {"kind": "direct", "reason": "종목 코드 직접 언급", "verified": False}
    # A different issuer's filing is not an industry-news candidate.
    if article.get("content_type") == "disclosure":
        return None
    profile = instrument.get("trading_factor_profile") or {}
    if not (profile.get("usable_for_news") or profile.get("usable_as_topic_hint")):
        return None
    names = [d.get("name", "") for d in profile.get("proposed_dimensions", [])]
    names += [d.get("name", "") for d in profile.get("factors", [])]
    if instrument.get("end_market_context") and instrument.get("code") == "034220":
        names.append("디스플레이·OLED")
    for topic in dict.fromkeys(names):
        if any(normalize(term) in text for term in TOPICS.get(topic, [topic] if len(topic) > 1 else [])):
            return {
                "kind": "industry_candidate",
                "reason": f"선택 종목 속성 ‘{topic}’ 관련 주제 후보 · 사업 영향 미검증",
                "verified": False,
            }
    return None
