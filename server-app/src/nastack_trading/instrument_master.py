"""Bounded public NH master download; never execute remotely supplied code."""

import hashlib
import re
import time

import httpx
from sqlalchemy import select

from .db import now
from .models import InstrumentMaster, Tenant
from .security import audit

SOURCE = "https://www.nhplug.com/instruments/m_new_stock.mst"
HEADER = "https://www.nhplug.com/instruments/m_new_stock.h"


def parse_master(header: str, data: bytes):
    fields = re.findall(r"char\s+(\w+)\[(\d+)\]\s*;", header)
    offsets, offset = {}, 0
    for name, width in fields:
        if name in offsets:
            raise ValueError("종목 명세 중복 필드")
        offsets[name] = (offset, int(width))
        offset += int(width)
    expected = {
        "sCode": (0, 6),
        "sMarket": (6, 1),
        "sKorName": (7, 41),
        "gVenture": (165, 1),
        "eStop": (161, 1),
        "dummy": (236, 1),
    }
    if offset != 237 or any(offsets.get(k) != v for k, v in expected.items()):
        raise ValueError("공식 종목 명세가 변경되었습니다. 파서를 확인하세요.")
    if not data or len(data) % 237 or len(data) > 5_000_000:
        raise ValueError("종목 파일 크기 불일치")
    rows, seen, excluded = [], set(), 0
    for start in range(0, len(data), 237):
        row = data[start : start + 237]
        if row[-1:] != b"\n":
            raise ValueError("종목 레코드 경계 오류")

        def field(name):
            pos, length = offsets[name]
            return row[pos : pos + length].decode("cp949").rstrip()

        code, market = field("sCode"), field("sMarket")
        if not re.fullmatch(r"[0-9]{6}", code) or market not in {"1", "4"}:
            excluded += 1
            continue
        name = field("sKorName").lstrip("*#").strip()
        if not name or code in seen:
            raise ValueError("종목 이름 또는 중복 코드 오류")
        seen.add(code)
        rows.append(
            {
                "code": code,
                "name": name,
                "market": "KOSPI" if market == "1" else "KOSDAQ",
                "asset_type": "etf" if market == "1" and field("gVenture") == "8" else "stock",
                "status": "active",
                "trading_halted": field("eStop") == "Y",
            }
        )
    if not rows:
        raise ValueError("지원 가능한 종목이 없습니다.")
    return {
        "items": sorted(rows, key=lambda r: r["code"]),
        "source_url": SOURCE,
        "sha256": hashlib.sha256(data).hexdigest(),
        "excluded": excluded,
    }


def download_master(transport=None):
    deadline = time.monotonic() + 25

    def read(client, url, limit):
        with client.stream("GET", url) as response:
            response.raise_for_status()
            result = bytearray()
            for chunk in response.iter_bytes():
                result.extend(chunk)
                if len(result) > limit or time.monotonic() > deadline:
                    raise ValueError("종목 다운로드 제한 초과")
            return bytes(result)

    with httpx.Client(timeout=10, follow_redirects=False, trust_env=False, transport=transport) as client:
        header = read(client, HEADER, 64_000).decode("utf-8-sig")
        data = read(client, SOURCE, 5_000_000)
    return parse_master(header, data)


def save_master(db, user, value):
    # Serialize refreshes at tenant level, without holding a lock during HTTP.
    db.scalar(select(Tenant).where(Tenant.id == user.tenant_id).with_for_update())
    item = db.get(InstrumentMaster, user.tenant_id)
    if item:
        item.value, item.updated_at = value, now()
    else:
        item = InstrumentMaster(tenant_id=user.tenant_id, value=value)
        db.add(item)
    audit(
        db,
        user,
        "instrument.master_refreshed",
        user.tenant_id,
        {"count": len(value["items"]), "source_url": SOURCE, "sha256": value["sha256"]},
    )
    db.flush()
    return item
