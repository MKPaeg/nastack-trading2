"""Personal broker secrets; legacy env keys remain owner-only fallback."""

from typing import Literal

from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import select

from .db import now
from .models import BrokerConnection, KiwoomConnection, User, UserBrokerKey
from .security import audit


def cipher(config):
    try:
        return Fernet(config.broker_encryption_key.get_secret_value().encode())
    except (ValueError, TypeError):
        raise HTTPException(409, "서버의 증권사 비밀정보 암호화 키가 필요합니다.") from None


def personal_config(db, user, config, broker):
    row = db.scalar(
        select(UserBrokerKey).where(
            UserBrokerKey.owner_id == user.id,
            UserBrokerKey.tenant_id == user.tenant_id,
            UserBrokerKey.broker == broker,
        )
    )
    if not row:
        return config
    try:
        key = cipher(config).decrypt(row.key_ciphertext.encode()).decode()
        secret = cipher(config).decrypt(row.secret_ciphertext.encode()).decode()
    except (InvalidToken, UnicodeError):
        raise HTTPException(
            409, "개인 증권사 키를 복호화하지 못했습니다. 서버 암호화 키를 확인하세요."
        ) from None
    return config.model_copy(
        update={
            f"{broker}_owner_email": user.email,
            f"{broker}_app_key": SecretStr(key),
            f"{broker}_app_secret": SecretStr(secret),
        }
    )


class Keys(BaseModel):
    version: int = Field(ge=0)
    app_key: SecretStr = Field(min_length=1, max_length=2000)
    app_secret: SecretStr = Field(min_length=1, max_length=2000)


class DimensionScreenState(BaseModel):
    family: str = Field(default="commodity", max_length=30, pattern=r"^[a-z_]+$")
    selected: str = Field(default="commodity-17", max_length=120)
    start: str = Field(default="2026-01-01", pattern=r"^\d{4}-\d{2}-\d{2}$")
    end: str = Field(default="", pattern=r"^(\d{4}-\d{2}-\d{2})?$")
    alignment: Literal["source", "krx"] = "source"
    display: Literal["raw", "normalized"] = "raw"
    onlyCollected: bool = False


class ReportScreenState(BaseModel):
    start: str = Field(default="", pattern=r"^(\d{4}-\d{2}-\d{2})?$")
    end: str = Field(default="", pattern=r"^(\d{4}-\d{2}-\d{2})?$")
    code: str = Field(default="", pattern=r"^(\d{6})?$")
    kind: Literal["all", "vector", "news", "research", "sale"] = "all"
    include_demo: bool = False


class ScreenState(BaseModel):
    view: Literal[
        "dashboard",
        "watchlist",
        "recommendations",
        "holdings",
        "analysis",
        "orders",
        "reports",
        "logs",
        "settings",
        "users",
        "data",
    ] = "dashboard"
    analysis_tab: Literal["detail", "dimensions", "vectors", "sources"] = "detail"
    data_tab: Literal["status", "dimensions", "news", "statistics"] = "status"
    recommendations_market: Literal["000", "001", "101"] = "000"
    holdings_tab: Literal["positions", "returns"] = "positions"
    return_period: Literal["year", "quarter", "month", "week"] = "year"
    code: str = Field(default="", pattern=r"^(\d{6})?$")
    news_code: str = Field(default="", pattern=r"^(\d{6})?$")
    trade_broker: Literal["namuh", "kiwoom"] = "namuh"
    trade_code: str = Field(default="", pattern=r"^(\d{6})?$")
    realized_code: str = Field(default="", pattern=r"^(\d{6})?$")
    dimension: DimensionScreenState = Field(default_factory=DimensionScreenState)
    chart_start: dict[str, str] = Field(default_factory=dict, max_length=100)
    chart_end: dict[str, str] = Field(default_factory=dict, max_length=100)
    report: ReportScreenState = Field(default_factory=ReportScreenState)


def user_brokers_router(current_user, db_session, config):
    router = APIRouter(prefix="/api/user/brokers")

    @router.get("/{broker}/keys")
    def status(broker: Literal["namuh", "kiwoom"], user=Depends(current_user), db=Depends(db_session)):
        row = db.scalar(
            select(UserBrokerKey).where(
                UserBrokerKey.owner_id == user.id,
                UserBrokerKey.tenant_id == user.tenant_id,
                UserBrokerKey.broker == broker,
            )
        )
        legacy = user.email.lower() == getattr(config, f"{broker}_owner_email").lower()
        return {
            "broker": broker,
            "version": row.version if row else 0,
            "configured": bool(row),
            "legacy_owner_only": legacy and not row,
            "updated_at": row.updated_at.isoformat() if row else None,
        }

    @router.put("/{broker}/keys")
    def save(
        broker: Literal["namuh", "kiwoom"], payload: Keys, user=Depends(current_user), db=Depends(db_session)
    ):
        if user.role == "viewer":
            raise HTTPException(403, "조회 전용 사용자는 증권사 키를 등록할 수 없습니다.")
        db.scalar(select(User).where(User.id == user.id).with_for_update())
        row = db.scalar(
            select(UserBrokerKey).where(
                UserBrokerKey.owner_id == user.id,
                UserBrokerKey.tenant_id == user.tenant_id,
                UserBrokerKey.broker == broker,
            )
        )
        if payload.version != (row.version if row else 0):
            raise HTTPException(409, "키 설정이 변경되었습니다. 새로 조회하세요.")
        crypto = cipher(config)
        if not row:
            row = UserBrokerKey(owner_id=user.id, tenant_id=user.tenant_id, broker=broker, version=0)
            db.add(row)
        row.key_ciphertext = crypto.encrypt(payload.app_key.get_secret_value().encode()).decode()
        row.secret_ciphertext = crypto.encrypt(payload.app_secret.get_secret_value().encode()).decode()
        row.version += 1
        row.updated_at = now()
        connection = db.get(BrokerConnection if broker == "namuh" else KiwoomConnection, user.id)
        if connection:
            connection.token_ciphertext = connection.token_scope = connection.token_expires_at = None
            connection.enabled = False
            connection.version += 1
        audit(db, user, "broker.personal_keys_saved", broker, {"version": row.version})
        db.commit()
        return status(broker, user, db)

    return router


def screen_state_router(current_user, db_session):
    from .models import UserSettings

    router = APIRouter()

    @router.put("/api/settings/screen-state")
    def save(payload: ScreenState, user=Depends(current_user), db=Depends(db_session)):
        row = db.scalar(
            select(UserSettings)
            .where(UserSettings.user_id == user.id, UserSettings.tenant_id == user.tenant_id)
            .with_for_update()
        )
        if not row:
            raise HTTPException(409, "사용자 설정이 없습니다.")
        for mapping in (payload.chart_start, payload.chart_end):
            import re

            if any(
                not re.fullmatch(r"\d{6}", code) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date)
                for code, date in mapping.items()
            ):
                raise HTTPException(422, "차트 기간을 확인하세요.")
        row.value = {
            **row.value,
            "screen_state": {**row.value.get("screen_state", {}), **payload.model_dump(exclude_unset=True)},
        }
        row.updated_at = now()
        db.commit()
        return {"status": "saved"}

    return router
