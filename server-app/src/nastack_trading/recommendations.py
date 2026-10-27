"""Owner-private, read-only KRX daily movers. Not an investment recommendation.

Official contract: https://openapi.kiwoom.com/m/guide/apiguide/05/ka10027
Only the first ranking page is used. No orders, stock registration or LLM calls.
"""

import math
import re
import time
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, select, update

from .db import aware, now
from .kiwoom import KiwoomReader
from .models import BrokerSnapshot, CollectionWorkflow, KiwoomConnection, StockFactorProfile, User
from .user_brokers import personal_config


class InitializationConsent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed: Literal[True]
    cloud_confirmed: bool = False


def number(value):
    try:
        result = float(str(value).replace(",", "").strip())
        if not math.isfinite(result):
            raise ValueError
        return result
    except (ValueError, TypeError):
        raise HTTPException(502, "키움 순위 응답의 숫자 형식을 확인하지 못했습니다.") from None


def movers(data, direction):
    rows = data.get("pred_pre_flu_rt_upper")
    if not isinstance(rows, list):
        raise HTTPException(502, "키움 등락률 순위 응답 형식을 확인하지 못했습니다.")
    result, seen = [], set()
    for row in rows[:1000]:
        if not isinstance(row, dict):
            raise HTTPException(502, "키움 등락률 순위 항목을 확인하지 못했습니다.")
        code = str(row.get("stk_cd", ""))
        if code.startswith("A"):
            code = code[1:]
        if not re.fullmatch(r"\d{6}", code) or code in seen:
            continue
        price, change, rate = (number(row.get(k)) for k in ("cur_prc", "pred_pre", "flu_rt"))
        volume = number(row.get("now_trde_qty"))
        sign = str(row.get("pred_pre_sig", ""))
        if sign in {"1", "2"}:
            change, rate = abs(change), abs(rate)
        elif sign in {"4", "5"}:
            change, rate = -abs(change), -abs(rate)
        elif sign == "3":
            change, rate = 0, 0
        else:
            raise HTTPException(502, "키움 등락 방향 기호를 확인하지 못했습니다.")
        if abs(price) <= 0 or volume < 0 or not volume.is_integer():
            raise HTTPException(502, "키움 가격·거래량 응답을 확인하지 못했습니다.")
        if (direction == "up" and rate <= 0) or (direction == "down" and rate >= 0):
            continue
        seen.add(code)
        result.append({"code": code, "name": str(row.get("stk_nm") or code)[:80],
                       "price": abs(price), "change": change, "rate": rate, "volume": int(volume)})
    result.sort(key=lambda item: item["rate"], reverse=direction == "up")
    return result[:20]


def recommendations_router(current_user, db_session, config, transport=None, *, factory=None, news_transport=None):
    router = APIRouter(prefix="/api/recommendations")

    def connection(db, user):
        reader = KiwoomReader(personal_config(db, user, config, "kiwoom"), transport)
        reader.owner(db, user)
        row = db.scalar(select(KiwoomConnection).where(
            KiwoomConnection.owner_id == user.id, KiwoomConnection.tenant_id == user.tenant_id))
        if not row or not row.enabled:
            raise HTTPException(409, "본인의 키움 조회 연결을 먼저 활성화하세요.")
        return reader, row, reader.scope(row.environment)

    def latest(db, user, market, scope):
        row = db.scalar(select(BrokerSnapshot).where(
            BrokerSnapshot.owner_id == user.id, BrokerSnapshot.tenant_id == user.tenant_id,
            BrokerSnapshot.kind == "movers", BrokerSnapshot.symbol == market,
        ).order_by(BrokerSnapshot.created_at.desc()).limit(1))
        return row if row and row.value.get("scope") == scope else None

    def view(row, reused=False):
        return {"snapshot": {k: v for k, v in row.value.items() if k != "scope"} if row else None,
                "collected_at": row.created_at if row else None, "reused": reused,
                "stale": bool(row and aware(row.created_at) < now() - timedelta(seconds=3))}

    @router.get("")
    def saved(market: Literal["000", "001", "101"] = "000", user=Depends(current_user), db=Depends(db_session)):
        try:
            _, _, scope = connection(db, user)
        except HTTPException as error:
            return {**view(None), "available": False, "note": error.detail}
        return {**view(latest(db, user, market, scope)), "available": True}

    @router.post("/refresh")
    def refresh(market: Literal["000", "001", "101"] = "000", auto: bool = False,
                user=Depends(current_user), db=Depends(db_session)):
        if not user.active or user.role == "viewer":
            raise HTTPException(403, "조회 전용 사용자는 저장된 순위만 볼 수 있습니다.")
        if auto:
            from .market import market_session

            if not market_session()["is_open"]:
                raise HTTPException(409, "정규장 외에는 자동 순위 조회를 하지 않습니다. 수동 조회는 가능합니다.")
        owner, tenant = user.id, user.tenant_id
        reader, conn, scope = connection(db, user)
        version = conn.version
        db.execute(update(User).where(User.id == user.id, User.tenant_id == user.tenant_id).values(active=User.active))
        row = latest(db, user, market, scope)
        if row and aware(row.created_at) > now() - timedelta(seconds=3):
            return {**view(row, True), "available": True}
        attempt = db.scalar(select(BrokerSnapshot).where(
            BrokerSnapshot.owner_id == user.id, BrokerSnapshot.tenant_id == user.tenant_id,
            BrokerSnapshot.kind == "movers_attempt",
        ).order_by(BrokerSnapshot.created_at.desc()).limit(1))
        wait = 30 if attempt and attempt.value.get("failed") else 3
        if attempt and aware(attempt.created_at) > now() - timedelta(seconds=wait):
            raise HTTPException(429, f"순위 조회 대기 중입니다. {wait}초 간격으로 재점검합니다. 이전 자료는 유지합니다.", headers={"Retry-After": str(wait)})
        reservation = attempt or BrokerSnapshot(owner_id=user.id, tenant_id=user.tenant_id, kind="movers_attempt",
                                                symbol=market, environment=conn.environment, value={})
        reservation.created_at, reservation.value = now(), {}
        db.add(reservation)
        db.commit()  # Reserve before authentication/network; failed attempts also cool down.
        try:
            token, environment = reader.token(db, user)
        except HTTPException:
            reservation.value = {"failed": True}
            db.commit()
            raise
        payload = {"mrkt_tp": market, "trde_qty_cnd": "0000", "stk_cnd": "1", "crd_cnd": "0",
                   "updown_incls": "1", "pric_cnd": "0", "trde_prica_cnd": "0", "stex_tp": "1"}
        output, partial = {}, False
        for index, (direction, sort) in enumerate((("up", "1"), ("down", "3"))):
            if index:
                time.sleep(0.3)
            try:
                data, headers = reader.read(token, environment, "ka10027", {**payload, "sort_tp": sort})
                output[direction] = movers(data, direction)
            except HTTPException as error:
                reservation.value = {"failed": True}
                db.commit()
                raise HTTPException(error.status_code, error.detail, headers={"Retry-After": "30"}) from None
            partial |= headers.get("cont-yn") == "Y"
        db.expire_all()
        current = db.get(User, owner)
        if not current or current.tenant_id != tenant or not current.active or current.role == "viewer":
            raise HTTPException(403, "사용자 권한이 변경되어 결과를 적용하지 않습니다.")
        _, current_conn, current_scope = connection(db, current)
        if not current.active or current.role == "viewer" or current_scope != scope or current_conn.version != version:
            raise HTTPException(409, "사용자 권한·키움 연결이 변경되어 결과를 적용하지 않습니다.")
        row = BrokerSnapshot(owner_id=user.id, tenant_id=user.tenant_id, kind="movers", symbol=market,
                             environment=environment, value={**output, "scope": scope, "market": market,
                             "source": "키움 ka10027 · KRX · 전일 종가 대비", "first_page_only": partial,
                             "environment": environment, "filters": "관리종목 제외 · 상하한가 포함 · 거래량·가격 제한 없음"})
        db.add(row)
        db.flush()
        # Bound only disposable ranking cache, never analysis/order/history objects.
        keep = select(BrokerSnapshot.id).where(
            BrokerSnapshot.owner_id == owner, BrokerSnapshot.tenant_id == tenant,
            BrokerSnapshot.kind == "movers").order_by(BrokerSnapshot.created_at.desc(), BrokerSnapshot.id.desc()).limit(60)
        db.execute(delete(BrokerSnapshot).where(
            BrokerSnapshot.owner_id == owner, BrokerSnapshot.tenant_id == tenant,
            BrokerSnapshot.kind == "movers", BrokerSnapshot.id.not_in(keep)).execution_options(synchronize_session=False))
        db.commit()
        return {**view(row), "available": True}

    @router.post("/{code}/open")
    def open_analysis(code: str, user=Depends(current_user), db=Depends(db_session)):
        if not re.fullmatch(r"\d{6}", code):
            raise HTTPException(422, "6자리 종목 코드를 선택하세요.")
        if not user.active or user.role == "viewer":
            raise HTTPException(403, "종목 초기 분석은 조회 전용 사용자에게 허용되지 않습니다.")
        # An analysis enrollment is NOT a watchlist item or a trading plan.
        tenant = user.tenant_id
        db.execute(update(User).where(User.id == user.id, User.tenant_id == tenant).values(active=User.active))
        user = db.get(User, user.id, populate_existing=True)
        if not user or user.tenant_id != tenant or not user.active or user.role == "viewer":
            raise HTTPException(403, "종목 초기 분석 권한이 변경되었습니다.")
        entry = db.scalar(select(BrokerSnapshot).where(
            BrokerSnapshot.owner_id == user.id, BrokerSnapshot.tenant_id == user.tenant_id,
            BrokerSnapshot.kind == "analysis_entry", BrokerSnapshot.symbol == code))
        if not entry:
            ranked = db.scalars(select(BrokerSnapshot).where(
                BrokerSnapshot.owner_id == user.id, BrokerSnapshot.tenant_id == user.tenant_id,
                BrokerSnapshot.kind == "movers", BrokerSnapshot.created_at > now() - timedelta(days=1)
            ).order_by(BrokerSnapshot.created_at.desc()).limit(200)).all()
            match = next((item for snapshot in ranked for key in ("up", "down")
                          for item in snapshot.value.get(key, []) if item.get("code") == code), None)
            if not match:
                raise HTTPException(404, "본인이 조회한 추천종목에서 선택하세요.")
            entry = BrokerSnapshot(owner_id=user.id, tenant_id=user.tenant_id, kind="analysis_entry",
                symbol=code, environment="public", value={"code": code, "name": match["name"],
                "market": "KRX", "asset_type": "unknown", "status": "active",
                "source_note": "본인의 추천종목 상세 분석 · 관심종목 미등록"})
            db.add(entry)
            db.commit()
        profile = db.get(StockFactorProfile, (user.id, code))
        workflow = db.get(CollectionWorkflow, (user.id, code))
        return {"instrument": {**entry.value, "analysis_origin": "recommendation"}, "initialize": not profile or profile.value.get("origin") == "llm_default" or not workflow,
                "watchlist_added": False}

    @router.post("/{code}/initialize")
    def initialize(code: str, payload: InitializationConsent, background: BackgroundTasks,
                   user=Depends(current_user), db=Depends(db_session)):
        from .default_attributes import schedule
        from .models import UserSettings
        from .news_models import resolve_model
        from .personal_connections import external_config
        from .security import audit

        tenant = user.tenant_id
        if not user.active or user.role == "viewer":
            raise HTTPException(403, "초기 분석 권한이 없습니다.")
        db.execute(update(User).where(User.id == user.id, User.tenant_id == tenant).values(active=User.active))
        user = db.get(User, user.id, populate_existing=True)
        if not user or user.tenant_id != tenant or not user.active or user.role == "viewer":
            raise HTTPException(403, "초기 분석 권한이 변경되었습니다.")
        entry = db.scalar(select(BrokerSnapshot).where(
            BrokerSnapshot.owner_id == user.id, BrokerSnapshot.tenant_id == tenant,
            BrokerSnapshot.kind == "analysis_entry", BrokerSnapshot.symbol == code))
        if not entry or not factory:
            raise HTTPException(404, "본인의 추천종목 상세 분석에서만 1회 초기화할 수 있습니다.")
        scoped = external_config(db, user, config)
        resolve_model(db, user, scoped, "default", payload.cloud_confirmed)
        # Explicit permission applies to this code/job, not future automatic jobs.
        audit(db, user, "recommendation.initialization_consent", code,
              {"one_run": True, "cloud_confirmed": payload.cloud_confirmed})
        schedule(db, user, [entry.value], background, factory, config, news_transport, retry=True,
                 recommendation_once=True, cloud_confirmed=payload.cloud_confirmed)
        row = db.get(StockFactorProfile, (user.id, code))
        settings = db.get(UserSettings, user.id)
        return {"profile": row.value if row and row.tenant_id == tenant else None,
                "one_run": True, "settings_changed": False,
                "automatic_business_enabled": bool(settings and settings.value.get("default_attributes_business_enabled"))}

    return router
