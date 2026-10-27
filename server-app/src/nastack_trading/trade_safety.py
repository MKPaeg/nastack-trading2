"""Per-user new-order block, independent of deployment-wide safety gates."""

from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from .models import AdminSettings, User, UserSettings
from .security import audit


class SafetyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=0)
    new_orders_locked: bool
    confirmation: Literal["신규 주문 잠금 해제 확인"] | None = None


class RuntimeUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=0)
    paused: bool


class ServerSafetyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=0)
    field: Literal["server_locked", "automatic_locked"]
    locked: bool
    confirmation: Literal["전체 사용자 주문 차단 해제 확인"] | None = None


def server_safety(db, config, *, lock=False):
    query = select(AdminSettings).where(AdminSettings.id == 1).execution_options(populate_existing=True)
    row = db.scalar(query.with_for_update() if lock else query)
    return {
        "server_safety_version": row.trading_safety_version if row else 0,
        "server_locked": not config.live_orders_enabled or not row or row.server_orders_locked,
        "automatic_locked": not config.automatic_orders_enabled or not row or row.server_automatic_locked,
        "deployment_orders_locked": not config.live_orders_enabled,
        "deployment_automatic_locked": not config.automatic_orders_enabled,
    }


def save_server_safety(db, user, config, payload):
    current = db.scalar(
        select(User)
        .where(User.id == user.id, User.tenant_id == user.tenant_id)
        .execution_options(populate_existing=True)
    )
    if not current or not current.active or current.role != "admin":
        raise HTTPException(403, "서버 전체 주문 차단은 관리자만 변경할 수 있습니다.")
    row = db.scalar(
        select(AdminSettings)
        .where(AdminSettings.id == 1)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if not row or row.trading_safety_version != payload.version:
        raise HTTPException(409, "서버 차단 상태가 변경되었습니다. 다시 조회하세요.")
    if not payload.locked:
        if payload.confirmation != "전체 사용자 주문 차단 해제 확인":
            raise HTTPException(422, "전체 사용자 주문 차단 해제를 확인하세요.")
        allowed = (
            config.live_orders_enabled
            if payload.field == "server_locked"
            else config.automatic_orders_enabled
        )
        if not allowed:
            raise HTTPException(
                409, "배포 환경의 안전 잠금은 화면에서 해제할 수 없습니다. 서버 실행 설정을 확인하세요."
            )
    attr = "server_orders_locked" if payload.field == "server_locked" else "server_automatic_locked"
    before = getattr(row, attr)
    setattr(row, attr, payload.locked)
    row.trading_safety_version += 1
    audit(
        db,
        current,
        "trading.server_safety_saved",
        "server",
        {
            "field": payload.field,
            "before": before,
            "locked": payload.locked,
            "scope": "all_users_all_tenants",
        },
    )
    db.commit()
    return safety(db, current, config)


def automatic_runtime(db, user):
    row = db.scalar(
        select(UserSettings)
        .where(UserSettings.user_id == user.id, UserSettings.tenant_id == user.tenant_id)
        .execution_options(populate_existing=True)
    )
    saved = row.value.get("automatic_runtime", {}) if row else {}
    return {"version": saved.get("version", 0), "paused": saved.get("paused", False)}


def save_automatic_runtime(db, user, payload):
    current = db.scalar(
        select(User)
        .where(User.id == user.id, User.tenant_id == user.tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if not current or not current.active or current.role == "viewer":
        raise HTTPException(403, "자동매매 일시정지 변경 권한이 없습니다.")
    row = db.scalar(
        select(UserSettings)
        .where(UserSettings.user_id == user.id, UserSettings.tenant_id == user.tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    saved = row.value.get("automatic_runtime", {}) if row else {}
    if saved.get("version", 0) != payload.version:
        raise HTTPException(409, "자동매매 상태가 변경되었습니다. 다시 조회하세요.")
    if row is None:
        row = UserSettings(user_id=user.id, tenant_id=user.tenant_id, value={})
        db.add(row)
    row.value = {**row.value, "automatic_runtime": {"version": payload.version + 1, "paused": payload.paused}}
    row.version = (row.version or 0) + 1
    audit(
        db,
        user,
        "trading.automatic_paused" if payload.paused else "trading.automatic_resumed",
        user.id,
        {"paused": payload.paused},
    )
    db.commit()
    return automatic_runtime(db, user)


def safety(db, user, config):
    row = db.scalar(
        select(UserSettings)
        .where(UserSettings.user_id == user.id, UserSettings.tenant_id == user.tenant_id)
        .execution_options(populate_existing=True)
    )
    value = row.value if row and row.tenant_id == user.tenant_id else {}
    saved = value.get("trade_safety", {})
    return {
        "version": saved.get("version", 0),
        "new_orders_locked": saved.get("new_orders_locked", True),
        **server_safety(db, config),
        "automatic_paused": value.get("automatic_runtime", {}).get("paused", False),
    }


def ensure_new_order_allowed(db, user, config):
    current = db.scalar(
        select(User)
        .where(User.id == user.id, User.tenant_id == user.tenant_id)
        .execution_options(populate_existing=True)
    )
    if not current or not current.active or current.role == "viewer":
        raise HTTPException(403, "활성 매매 사용자만 주문할 수 있습니다.")
    policy = safety(db, user, config)
    if policy["server_locked"]:
        raise HTTPException(409, "서버 실전 주문 잠금 상태입니다. 모든 사용자 신규 주문이 차단됩니다.")
    if policy["new_orders_locked"]:
        raise HTTPException(409, "내 신규 주문 차단이 켜져 있습니다. 상단 안전 옵션을 확인하세요.")


def save_safety(db, user, config, payload):
    if not user.active or user.role == "viewer":
        raise HTTPException(403, "안전 옵션 변경 권한이 없습니다.")
    db.scalar(select(User).where(User.id == user.id, User.tenant_id == user.tenant_id).with_for_update())
    row = db.scalar(
        select(UserSettings)
        .where(UserSettings.user_id == user.id, UserSettings.tenant_id == user.tenant_id)
        .with_for_update()
    )
    if not row or safety(db, user, config)["version"] != payload.version:
        raise HTTPException(409, "안전 옵션이 변경되었습니다. 다시 조회하세요.")
    if not payload.new_orders_locked and payload.confirmation != "신규 주문 잠금 해제 확인":
        raise HTTPException(422, "신규 주문 차단 해제를 직접 확인하세요.")
    row.value = {
        **row.value,
        "trade_safety": {"version": payload.version + 1, "new_orders_locked": payload.new_orders_locked},
    }
    audit(db, user, "trading.safety_saved", user.id, {"new_orders_locked": payload.new_orders_locked})
    db.commit()
    return safety(db, user, config)
