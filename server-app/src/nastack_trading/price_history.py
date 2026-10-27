"""Owner-scoped unadjusted history via official read-only ka10081; no orders."""

import re
import time
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import select

from .kiwoom import KiwoomReader
from .models import KiwoomConnection


def fetch_namuh_history(db, user, config, code, start, end, transport=None):
    """Owner-scoped default NH history; no silent cross-provider fallback."""
    from .market import daily_date, numeric
    from .trade_gateway import TradeGateway

    if not re.fullmatch(r"\d{6}", code):
        raise HTTPException(422, "국내 종목 코드만 조회합니다.")
    gateway = TradeGateway(config, namuh_transport=transport)
    raw, environment = gateway.request(
        db,
        user,
        "namuh",
        "/krstock/quote/v1/currentDaily",
        {"market_cd": "KRX", "iem_cd": code, "array_cnt": "250", "view_main_yn": "Y"},
    )
    bars = raw.get("Output_0")
    if not isinstance(bars, list) or not bars:
        raise HTTPException(502, "나무증권 과거 일봉 목록을 확인하지 못했습니다.")
    points = {}
    for bar in bars:
        if not isinstance(bar, dict):
            raise HTTPException(502, "나무증권 과거 일봉 형식 오류")
        day = daily_date(bar.get("bsop_date"))
        price = numeric(bar.get("stck_clpr"))
        if price is None:
            price = numeric(bar.get("clpr"))
        if not day or price is None or price <= 0 or day in points:
            raise HTTPException(502, "나무증권 과거 일봉 날짜·가격 검증 실패")
        points[day] = {"date": day, "close": price, "provisional": False}
    chart = [points[d] for d in sorted(points) if str(start) <= d <= str(end)]
    return {
        "chart": chart,
        "provider": "namuh_domestic_history",
        "series_code": code,
        "environment": environment,
        "adjustment": "provider_default",
        "adjustment_verified": False,
        "source_url": "https://www.nhplug.com/openapi-docs/krstock/openapi.json",
    }


def fetch_history(db, user, config, code, start, end, transport=None):
    from .user_brokers import personal_config

    config = personal_config(db, user, config, "kiwoom")
    if not re.fullmatch(r"\d{6}", code):
        raise HTTPException(422, "국내 종목 코드만 조회합니다.")
    reader = KiwoomReader(config, transport)
    token, environment = reader.token(db, user)
    version = db.get(KiwoomConnection, user.id).version
    points, seen, continuation, previous = {}, set(), None, None
    for page in range(10):
        data, headers = reader.read(
            token,
            environment,
            "ka10081",
            {"stk_cd": code, "base_dt": end.strftime("%Y%m%d"), "upd_stkpc_tp": "0"},
            continuation,
        )
        bars = data.get("stk_dt_pole_chart_qry")
        if data.get("stk_cd") != code or not isinstance(bars, list) or not bars or len(bars) > 2000:
            raise HTTPException(502, "키움 과거 일봉 코드·목록 검증 실패")
        days = []
        for bar in bars:
            if not isinstance(bar, dict):
                raise HTTPException(502, "키움 과거 일봉 형식 오류")
            raw_day, raw_price = str(bar.get("dt", "")), str(bar.get("cur_prc", "")).strip()
            if not re.fullmatch(r"\d{8}", raw_day) or not re.fullmatch(r"[+-]?\d{1,12}", raw_price):
                raise HTTPException(502, "키움 과거 일봉 날짜·가격 검증 실패")
            try:
                day = datetime.strptime(raw_day, "%Y%m%d").date()
            except ValueError:
                raise HTTPException(502, "키움 과거 일봉 날짜 오류") from None
            price = abs(int(raw_price))
            if price <= 0 or day > end:
                raise HTTPException(502, "키움 과거 일봉 범위·가격 오류")
            days.append(day)
            if start <= day <= end:
                key = day.isoformat()
                if key in points:
                    raise HTTPException(502, "키움 과거 일봉 중복 날짜")
                points[key] = {"date": key, "close": price, "provisional": False}
        oldest = min(days)
        if previous and oldest >= previous:
            raise HTTPException(502, "키움 과거 일봉 연속조회 진전 없음")
        if oldest <= start or headers.get("cont-yn") != "Y":
            break
        continuation = headers.get("next-key")
        if not continuation or continuation in seen or page == 9:
            raise HTTPException(502, "키움 과거 일봉 연속조회 미완료")
        seen.add(continuation)
        previous = oldest
        time.sleep(0.3)
    current = db.scalar(
        select(KiwoomConnection)
        .where(KiwoomConnection.owner_id == user.id, KiwoomConnection.tenant_id == user.tenant_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if (
        not current
        or not current.enabled
        or current.version != version
        or current.environment != environment
        or current.token_scope != reader.scope(environment)
    ):
        raise HTTPException(409, "과거 조회 중 키움 연결 정책 변경")
    if not points:
        raise HTTPException(422, "요청 구간에 과거 일봉이 없습니다.")
    return {
        "chart": [points[d] for d in sorted(points)],
        "provider": "kiwoom_domestic_history",
        "series_code": code,
        "environment": environment,
        "adjustment": "unadjusted",
        "adjustment_verified": False,
        "source_url": "https://openapi.kiwoom.com/m/guide/apiguide?apiId=ka10081",
    }
