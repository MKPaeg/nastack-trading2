"""Public KIND issuer identity/products, distinct from private broker metadata.

Only this fixed public download is shared in a bounded process cache. No user
credentials, exclusion policy, private selections, or model outputs enter it.
"""

import hashlib
import re
import threading
import time
from html.parser import HTMLParser
from urllib.parse import urlsplit

import httpx

from .db import now
from .research import public_url

KIND_LIST = "https://kind.krx.co.kr/corpgeneral/corpList.do?method=download&searchType=13"
_lock = threading.Lock()
_cache = None


class ListingParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self.row, self.cell = [], None, None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        elif tag in {"td", "th"} and self.row is not None:
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self.cell is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None


def parse_listing(html):
    parser = ListingParser()
    parser.feed(html)
    expected = ["회사명", "시장구분", "종목코드", "업종", "주요제품", "상장일", "결산월", "대표자명", "홈페이지", "지역"]
    if not parser.rows or parser.rows[0] != expected:
        raise ValueError("KIND issuer schema changed")
    result = {}
    for row in parser.rows[1:]:
        if len(row) != len(expected) or not re.fullmatch(r"[0-9A-Z]{6}", row[2]):
            continue
        if row[2] in result:
            raise ValueError("KIND duplicate code")
        result[row[2]] = dict(zip(expected, row, strict=True))
    if not result:
        raise ValueError("KIND empty listing")
    return result


def issuer_listing(instrument, transport=None):
    global _cache
    with _lock:
        # Test transports never populate or consume the live shared public cache.
        if transport is None and _cache and time.monotonic() - _cache[0] < 86400:
            rows, retrieved = _cache[1:]
        else:
            with httpx.Client(timeout=15, trust_env=False, follow_redirects=False, transport=transport) as client:
                with client.stream("GET", KIND_LIST) as response:
                    response.raise_for_status()
                    content = bytearray()
                    for chunk in response.iter_bytes():
                        content.extend(chunk)
                        if len(content) > 3_000_000:
                            raise ValueError("KIND listing too large")
            rows, retrieved = parse_listing(content.decode("euc-kr")), now().isoformat()
            if transport is None:
                _cache = (time.monotonic(), rows, retrieved)
    row = rows.get(instrument["code"])
    def normalize(value):
        return "".join(value.split()).casefold()
    if not row or normalize(row["회사명"]) != normalize(instrument["name"]):
        return None
    homepage = public_url(row["홈페이지"])
    if homepage and urlsplit(homepage).username:
        homepage = None
    snippet = f'회사명 {row["회사명"]}; 종목코드 {row["종목코드"]}; 업종 {row["업종"]}; 주요제품 {row["주요제품"]}; 결산월 {row["결산월"]}.'
    return {"title": f'{row["회사명"]} 공식 상장법인 정보', "url": KIND_LIST, "snippet": snippet,
            "source_kind": "official_issuer_listing", "retrieved_at": retrieved,
            "content_hash": hashlib.sha256(snippet.encode()).hexdigest(),
            "homepage": homepage, "products": row["주요제품"], "industry": row["업종"]}


def business_text(text):
    """Reject navigation/meeting-only excerpts before paying a summarizer."""
    stripped = re.sub(r"\d+\.\s*(사업의 개요|주요 제품 및 서비스|원재료 및 생산설비|매출 및 수주상황|위험관리 및 파생거래|주요 계약 및 연구개발활동|기타 참고사항)", "", text)
    return bool(re.search(r"제조|개발|공급|솔루션|생산|판매|운영|추종|제품은|사업은|사업을|사업으로|서비스를|manufactur|develop|solution|product|business", stripped, re.I))


def broker_identity(db, user, config, instrument, transport=None):
    """Private owner-checked identity/industry, never a detailed business claim."""
    from datetime import timedelta

    from fastapi import HTTPException
    from sqlalchemy import select

    from .brokers import NamuhAuth
    from .db import aware
    from .models import BrokerConnection, BrokerSnapshot, User
    from .namuh_reads import read_lane
    from .trade_gateway import TradeGateway
    from .user_brokers import personal_config

    tenant = user.tenant_id
    user = db.get(User, user.id, populate_existing=True)
    if not user or user.tenant_id != tenant or not user.active or user.role == "viewer":
        return {"status": "not_authorized"}
    config = personal_config(db, user, config, "namuh")
    conn = db.scalar(select(BrokerConnection).where(BrokerConnection.owner_id == user.id,
                     BrokerConnection.tenant_id == user.tenant_id))
    if not conn or not conn.enabled or config.namuh_owner_email.lower() != user.email.lower():
        return {"status": "not_connected"}
    scope = NamuhAuth(config).scope()
    version = conn.version
    cached = db.scalar(select(BrokerSnapshot).where(BrokerSnapshot.owner_id == user.id,
        BrokerSnapshot.tenant_id == user.tenant_id, BrokerSnapshot.kind == "issuer_identity",
        BrokerSnapshot.symbol == instrument["code"], BrokerSnapshot.environment == conn.environment)
        .order_by(BrokerSnapshot.created_at.desc()).limit(1))
    if cached and cached.value.get("scope") == scope and cached.value.get("version") == conn.version and aware(cached.created_at) > now() - timedelta(days=1):
        return {**cached.value["metadata"], "reused": True}
    try:
        with read_lane("background"):
            data, environment = TradeGateway(config, namuh_transport=transport).request(db, user, "namuh",
                "/krstock/quote/v1/currentPrice", {"market_cd": "KRX", "iem_cd": instrument["code"]})
    except HTTPException:
        return {"status": "unavailable"}
    quote = data.get("Output_0") or {}
    name = str(quote.get("iem_nm") or "").lstrip("*#").strip()
    if str(quote.get("iem_cd")) != instrument["code"] or name != instrument["name"]:
        return {"status": "identity_mismatch"}
    current_user = db.get(User, user.id, populate_existing=True)
    db.refresh(conn)
    if not current_user or not current_user.active or current_user.role == "viewer" or not conn.enabled or conn.version != version or NamuhAuth(personal_config(db, current_user, config, "namuh")).scope() != scope:
        return {"status": "policy_changed"}
    metadata = {"status": "confirmed", "code": instrument["code"], "name": name,
                "industry": str(quote.get("bstp_kor_isnm") or "")[:100],
                "industry_code": str(quote.get("bstp_cls_code") or "")[:20],
                "source": "NH PLUG currentPrice", "retrieved_at": now().isoformat()}
    cached = cached or BrokerSnapshot(owner_id=user.id, tenant_id=user.tenant_id, kind="issuer_identity",
        symbol=instrument["code"], environment=environment)
    cached.value = {"scope": scope, "version": conn.version, "metadata": metadata}
    cached.created_at = now()
    db.add(cached)
    db.flush()
    return metadata
