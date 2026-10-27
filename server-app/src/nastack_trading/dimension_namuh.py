"""Read-only fixed NH daily endpoint for explicitly named ETF proxies."""

import hashlib
import json
from datetime import timedelta

import httpx
from cryptography.fernet import InvalidToken
from fastapi import HTTPException
from sqlalchemy import func, select

from .brokers import API_URLS, NamuhAuth
from .db import aware, now
from .market import daily_date, numeric
from .models import BrokerConnection, InstrumentMaster, User


def fetch_etf(db, user, config, dimension, start, end, transport=None, history_transport=None):
    from .user_brokers import personal_config

    original_config = config
    config = personal_config(db, user, config, "namuh")
    owner = config.namuh_owner_email.strip().lower()
    if user.email.lower() != owner or (
        config is original_config
        and db.scalar(select(func.count()).select_from(User).where(User.email == owner)) != 1
    ):
        raise HTTPException(403, "이 ETF 수집은 기존 나무 API 소유자만 사용할 수 있습니다.")
    auth = NamuhAuth(config, transport)
    connection = db.scalar(
        select(BrokerConnection).where(
            BrokerConnection.owner_id == user.id, BrokerConnection.tenant_id == user.tenant_id
        )
    )
    if not connection or not connection.enabled or auth.missing():
        raise HTTPException(409, "관리자 설정의 기존 나무 API 연결·인증을 확인하세요.")
    if (
        not connection.token_ciphertext
        or connection.token_scope != auth.scope()
        or not connection.token_expires_at
        or aware(connection.token_expires_at) <= now() + timedelta(seconds=60)
    ):
        raise HTTPException(409, "나무 인증이 만료되었습니다. 관리자 설정에서 인증 확인 후 다시 수집하세요.")
    master = db.get(InstrumentMaster, user.tenant_id)
    item = (
        next((x for x in master.value.get("items", []) if x.get("code") == dimension["series_code"]), None)
        if master
        else None
    )
    if (
        not item
        or item.get("asset_type") != "etf"
        or item.get("status") != "active"
        or item.get("trading_halted")
        or item.get("name") != dimension["instrument_name"]
    ):
        raise HTTPException(
            409,
            "공식 종목 마스터의 ETF 코드·상품명·거래 상태를 확인하세요. 이름 변경 시 매핑 재검토가 필요합니다.",
        )
    try:
        token = auth.cipher().decrypt(connection.token_ciphertext.encode()).decode()
        from .namuh_reads import read_gate, read_lane

        gate = read_gate(user, connection.environment, auth.scope())
        with read_lane("background"):
            gate.acquire()
        with httpx.Client(timeout=20, transport=transport, trust_env=False, follow_redirects=False) as client:
            response = client.post(
                API_URLS[connection.environment] + "/krstock/quote/v1/currentDaily",
                json={
                    "Input_0": {
                        "market_cd": "KRX",
                        "iem_cd": dimension["series_code"],
                        "array_cnt": "250",
                        "view_main_yn": "Y",
                    }
                },
                headers={
                    "x-client-id": config.namuh_app_key.get_secret_value(),
                    "x-client-secret": config.namuh_app_secret.get_secret_value(),
                    "authorization": f"Bearer {token}",
                },
            )
        if response.status_code == 429:
            gate.limited(response.headers.get("Retry-After"))
        if response.status_code != 200 or len(response.content) > 2_000_000:
            raise HTTPException(502, "나무 일별 시세 조회를 확인하지 못했습니다. 기존 기록은 유지합니다.")
        raw = response.json()
        bars = raw.get("Output_0") if isinstance(raw, dict) else None
        if not isinstance(bars, list):
            raise ValueError
        # Keep available NH observations. Older unavailable dates are reported as
        # gaps, not silently fetched via another user's/provider's connection.
        points = {}
        for bar in bars:
            if not isinstance(bar, dict):
                raise ValueError
            day = daily_date(bar.get("bsop_date"))
            value = numeric(bar.get("stck_clpr"))
            if value is None:
                value = numeric(bar.get("clpr"))
            if not day or value is None or value <= 0:
                raise ValueError
            if str(start) <= day <= str(end):
                if day in points:
                    raise ValueError
                points[day] = {"date": day, "value": value}
        if not points:
            raise HTTPException(
                422,
                "선택 기간에 반환된 ETF 시세가 없습니다. 최근 최대 250거래일만 조회하며 과거 자료를 채우지 않습니다.",
            )
        values = [points[key] for key in sorted(points)]
        return {
            "points": values,
            "sha256": hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest(),
            "sha256_basis": "validated_points_json",
            "source_url": dimension["source_url"],
            "series_code": dimension["series_code"],
            "provider": "namuh_etf",
            "representation": "etf_proxy",
            "instrument_name": dimension["instrument_name"],
            "definition": dimension,
            "environment": connection.environment,
            "retrieved_at": now().isoformat(),
            "adjustment_verified": False,
            "publication_times_verified": False,
            "history_limit": 250,
        }
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError, TypeError, InvalidToken, UnicodeDecodeError):
        raise HTTPException(
            502, "나무 ETF 일별 응답을 검증하지 못했습니다. 기존 자료는 유지합니다."
        ) from None
