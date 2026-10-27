"""Fixed, issuer-reviewed US ETF proxies using Kiwoom read-only endpoints."""

import hashlib
import json
import re
import time
from datetime import datetime, timedelta
from datetime import time as day_time
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import select

from .db import now
from .etf_expansion import US_ETFS
from .kiwoom import KiwoomReader
from .models import KiwoomConnection


def fetch_us_etf(db, user, config, dimension, start, end, transport=None):
    from .user_brokers import personal_config

    config = personal_config(db, user, config, "kiwoom")
    expected = US_ETFS.get(dimension["name"])
    if not expected or (dimension["series_code"], dimension["exchange_code"]) != expected[:2]:
        raise HTTPException(409, "검증된 미국 ETF 매핑만 수집할 수 있습니다.")
    reader = KiwoomReader(config, transport)
    token, environment = reader.token(db, user)
    version = db.get(KiwoomConnection, user.id).version
    exchange, symbol = dimension["exchange_code"], dimension["series_code"]
    master, _ = reader.read(token, environment, "usa10100", {"stex_tp": exchange, "stk_cd": symbol})
    if master.get("stk_cd") != symbol or master.get("stex_tp") != exchange or master.get("isEtf") != "Y":
        raise HTTPException(
            409, "키움에서 동일 거래소·티커의 ETF임을 확인하지 못했습니다. ETF/ETN을 임의 대체하지 않습니다."
        )
    # A Korean 'today' is not a completed US daily bar. Retain only past/closed US dates.
    us_now = now().astimezone(ZoneInfo("America/New_York"))
    cutoff = min(
        end, us_now.date() - timedelta(days=1) if us_now.time() < day_time(16, 15) else us_now.date()
    )
    points, page_hashes, continuation, keys, previous_oldest = {}, [], None, set(), None
    try:
        for page in range(10):
            data, headers = reader.read(
                token,
                environment,
                "usa06012",
                {
                    "stex_tp": exchange,
                    "stk_cd": symbol,
                    "strt_dt": cutoff.strftime("%Y%m%d"),
                    "upd_stkpc_tp": "1",
                    "exrt_appl_tp": "0",
                },
                continuation,
            )
            rows = data.get("result_list")
            if not isinstance(rows, list) or not rows or len(rows) > 2000:
                raise ValueError
            page_hashes.append(hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest())
            days = []
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError
                raw_day, raw_value = str(row.get("dt", "")), str(row.get("cur_prc", "")).strip()
                if not re.fullmatch(r"\d{8}", raw_day) or not re.fullmatch(
                    r"\+?\d{1,12}(?:\.\d{1,8})?", raw_value
                ):
                    raise ValueError
                day = datetime.strptime(raw_day, "%Y%m%d").date()
                value = Decimal(raw_value)
                if not value.is_finite() or value <= 0:
                    raise ValueError
                days.append(day)
                if start <= day <= cutoff:
                    key = day.isoformat()
                    if key in points:
                        raise ValueError
                    points[key] = {"date": key, "value": float(value)}
            oldest = min(days)
            if previous_oldest and oldest >= previous_oldest:
                raise ValueError
            if oldest <= start or headers.get("cont-yn") != "Y":
                break
            continuation = headers.get("next-key")
            if not continuation or len(continuation) > 256 or continuation in keys or page == 9:
                raise ValueError
            keys.add(continuation)
            previous_oldest = oldest
            time.sleep(0.3)
        if not points:
            raise HTTPException(
                422, "선택 기간에 확정 미국 ETF 일봉이 없습니다. 미국 당일 미확정 봉은 제외합니다."
            )
        connection = db.scalar(
            select(KiwoomConnection)
            .where(KiwoomConnection.owner_id == user.id, KiwoomConnection.tenant_id == user.tenant_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if (
            not connection
            or not connection.enabled
            or connection.version != version
            or connection.environment != environment
            or connection.token_scope != reader.scope(environment)
        ):
            raise HTTPException(409, "수집 도중 키움 정책이 변경되어 결과를 저장하지 않습니다.")
        values = [points[key] for key in sorted(points)]
        return {
            "points": values,
            "provider": "kiwoom_us_etf",
            "representation": "etf_proxy",
            "series_code": symbol,
            "exchange_code": exchange,
            "currency": "USD",
            "definition": dimension,
            "instrument_name": dimension["instrument_name"],
            "provider_instrument_name": master.get("stk_enm"),
            "sha256": hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest(),
            "sha256_basis": "validated_points_json",
            "page_sha256": page_hashes,
            "source_url": dimension["source_url"],
            "retrieved_at": now().isoformat(),
            "us_cutoff": cutoff.isoformat(),
            "adjustment_requested": True,
            "adjustment_verified": False,
            "publication_times_verified": False,
            "environment": environment,
        }
    except (ValueError, TypeError, KeyError, InvalidOperation, OverflowError):
        raise HTTPException(
            502, "미국 ETF 일봉의 날짜·가격·중복·연속조회를 검증하지 못했습니다. 기존 자료를 유지합니다."
        ) from None
