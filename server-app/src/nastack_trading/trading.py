"""Explicitly confirmed order intents; no inference code can submit orders."""

import math
from datetime import timedelta
from typing import Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select, update

from .brokers import NamuhAuth
from .db import aware, local_time, now
from .email_lists import deliver, saved_lists
from .market import market_session
from .models import (
    AuditEvent,
    BrokerConnection,
    BrokerSnapshot,
    Instrument,
    KiwoomConnection,
    TradeOrder,
    TradePlan,
    User,
    UserBrokerKey,
)
from .namuh_reads import critical_reads
from .security import audit
from .telegram_settings import preferences, telegram_request
from .trade_audit import clean, order_detail, plan_detail
from .trade_notifications import (
    Channels,
    channels,
    completion_message,
    notify_automatic_control,
    save_channels,
)
from .trade_safety import (
    RuntimeUpdate,
    SafetyUpdate,
    ServerSafetyUpdate,
    automatic_runtime,
    ensure_new_order_allowed,
    safety,
    save_automatic_runtime,
    save_safety,
    save_server_safety,
    server_safety,
)


class PlanInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(pattern=r"^\d{6}$")
    broker: Literal["namuh", "kiwoom"]
    account_no: str = Field(default="", max_length=20, pattern=r"^\d*$")
    version: int = Field(default=0, ge=0)
    mode: Literal["automatic"] = "automatic"
    fraction: Literal[100, 50, 20, 10] = 10
    auto_max_quantity: int | None = Field(default=None, ge=1, le=10000000)
    repeat_trading: Literal[False] = False  # Reject retired repeat execution, including old clients.
    entry_policy: Literal["buy_then_sell_all", "legacy"] = "buy_then_sell_all"
    buy_rule: Literal["price"] = "price"
    buy_condition: Literal["at_or_below", "at_or_above"] = "at_or_below"
    buy_price: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    take_profit: Literal[3, 5, 7, 10, 15] = 5
    take_profit_enabled: bool = True
    buy_enabled: bool = True
    stop_loss_enabled: bool = False
    stop_loss_rate: Literal[5, 6, 7, 8, 9, 10, 15, 20] = 5
    stop_loss_price: int | None = Field(default=None, gt=0, le=1000000000)
    trade_time: str = Field(default="09:00", pattern=r"^(09|1[0-4]):[0-5][0-9]$|^15:[01][0-9]$")
    test_buy_price: int | None = Field(default=None, gt=0, le=1000000000)
    test_sell_price: int | None = Field(default=None, gt=0, le=1000000000)
    active: bool = False
    confirm_auto: bool = False

    @model_validator(mode="after")
    def valid_rule(self):
        if self.active and self.buy_enabled and self.buy_rule == "price" and self.buy_price is None:
            raise ValueError("가격 기반 매수에는 기준가격이 필요합니다.")
        if self.active and (self.mode != "automatic" or not self.confirm_auto):
            raise ValueError("자동 주문 활성화를 직접 확인해야 합니다.")
        return self


class OrderPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_id: str
    side: Literal["buy", "sell"]
    quantity: int | None = Field(default=None, ge=1, le=10000000)
    request_key: str = Field(default_factory=lambda: str(uuid4()), min_length=10, max_length=100)
    purpose: Literal["normal", "small_live_test", "position_adjustment"] = "normal"
    limit_price: int | None = Field(default=None, gt=0, le=1000000000)

    @model_validator(mode="after")
    def small_test(self):
        if self.purpose == "small_live_test" and (self.quantity != 1 or self.limit_price is None):
            raise ValueError("소액 실전 테스트는 가격을 입력한 1주 지정가 주문만 가능합니다.")
        return self


class PlanControl(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)
    action: Literal["start", "stop", "schedule"]
    time: str | None = Field(default=None, pattern=r"^(09|1[0-4]):[0-5][0-9]$|^15:[01][0-9]$")
    confirmation: Literal["실전 자동매매 시작 확인"] | None = None
    settings: PlanInput | None = None


def adjustment_preview_pending(order):
    if order.status != "preview" or order.value.get("purpose") not in {"normal", "position_adjustment"}:
        return False
    from datetime import datetime

    try:
        return aware(datetime.fromisoformat(order.value["expires_at"])) > now()
    except (KeyError, TypeError, ValueError):
        return True  # Malformed intent must not grant automatic execution permission.


class PlanListChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)


def change_plan_listing(db, user, plan_id, version, *, archived):
    """Soft removal only. Keep the stable plan ID and every financial ledger row."""
    if not user.active or user.role == "viewer":
        raise HTTPException(403, "매매 목록 변경 권한이 없습니다.")
    # Same owner gate as final transmission: removal cannot race past authorization.
    db.execute(
        update(User).where(User.id == user.id, User.tenant_id == user.tenant_id).values(active=User.active)
    )
    current = db.scalar(
        select(User)
        .where(User.id == user.id, User.tenant_id == user.tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if not current or not current.active or current.role == "viewer":
        raise HTTPException(403, "매매 목록 변경 권한이 없습니다.")
    row = db.scalar(
        select(TradePlan)
        .where(
            TradePlan.id == plan_id,
            TradePlan.owner_id == current.id,
            TradePlan.tenant_id == current.tenant_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if not row:
        raise HTTPException(404, "본인의 매매 설정을 찾을 수 없습니다.")
    if row.version != version:
        raise HTTPException(409, "설정이 변경되었습니다. 목록을 새로 조회하세요.")
    if bool(row.value.get("archived")) == archived:
        return plan_view(row)
    stamp = now()
    message = (
        "매매 목록에서 제거 · 기존 주문·체결·실현손익 보존"
        if archived
        else "매매 목록 재추가 · 자동매매는 별도 시작 승인 필요"
    )
    previous = plan_detail(row)
    row.value = {
        **row.value,
        "archived": archived,
        "active": False,
        "archived_at" if archived else "restored_at": stamp.isoformat(),
        "worker_status": {"state": "stopped", "message": message, "checked_at": stamp.isoformat()},
        "auto_control": {
            "action": "stop",
            "state": "stopped",
            "generation": str(uuid4()),
            "requested_at": stamp.isoformat(),
        },
    }
    row.value = {k: v for k, v in row.value.items() if k != "auto_authorization"}
    row.version += 1
    row.updated_at = stamp
    audit(
        db,
        current,
        "trading.plan_archived" if archived else "trading.plan_restored",
        row.id,
        {**plan_detail(row), "before": previous, "after": plan_detail(row)},
    )
    db.commit()
    return plan_view(row)


def automatic_connection(db, user, broker):
    key = db.scalar(
        select(UserBrokerKey)
        .where(
            UserBrokerKey.owner_id == user.id,
            UserBrokerKey.tenant_id == user.tenant_id,
            UserBrokerKey.broker == broker,
        )
        .execution_options(populate_existing=True)
    )
    model = BrokerConnection if broker == "namuh" else KiwoomConnection
    connection = db.scalar(
        select(model)
        .where(model.owner_id == user.id, model.tenant_id == user.tenant_id)
        .execution_options(populate_existing=True)
    )
    if (
        not connection
        or not connection.enabled
        or connection.environment != "live"
        or not connection.token_scope
    ):
        raise HTTPException(409, "본인의 실전 증권사 연결을 먼저 검증하세요.")
    return {
        "key_version": key.version if key else 0,
        "connection_version": connection.version,
        "environment": connection.environment,
        "token_scope": connection.token_scope,
    }


def control_plan(db, user, plan_id, payload, config=None, *, stop_reason=""):
    if not user.active or user.role == "viewer":
        raise HTTPException(403, "자동매매 제어 권한이 없습니다.")
    db.scalar(select(User).where(User.id == user.id, User.tenant_id == user.tenant_id).with_for_update())
    row = db.scalar(
        select(TradePlan)
        .where(TradePlan.id == plan_id, TradePlan.owner_id == user.id, TradePlan.tenant_id == user.tenant_id)
        .with_for_update()
    )
    if not row:
        raise HTTPException(404, "본인의 매매 설정을 찾을 수 없습니다.")
    if row.version != payload.version:
        raise HTTPException(409, "조건이 변경되었습니다. 목록을 새로 조회하세요.")
    if row.value.get("archived") and payload.action != "stop":
        raise HTTPException(409, "제거된 종목입니다. 매매 목록에 재추가한 뒤 시작하세요.")
    was_active = bool(row.value.get("active"))
    if payload.settings is not None:
        settings = payload.settings
        if payload.action == "stop" or settings.code != row.code or settings.broker != row.broker:
            raise HTTPException(422, "본인 종목의 시작·예약 조건만 변경할 수 있습니다.")
        if settings.version != row.version or settings.account_no:
            raise HTTPException(409, "최신 설정을 확인하세요. 시작 시 계좌 변경은 허용하지 않습니다.")
        row.value = {
            **row.value,
            **settings.model_dump(
                exclude={"code", "broker", "account_no", "version", "confirm_auto", "active"}
            ),
            "mode": "automatic",
            "quantity_limit_explicit": True,
        }
    running = payload.action in {"start", "schedule"} and config is not None
    if running:
        if (
            not config.live_worker_enabled
            or not config.live_orders_enabled
            or not config.automatic_orders_enabled
        ):
            raise HTTPException(409, "서버 실전 주문·자동매매 실행이 잠겨 있습니다. 서버 설정을 확인하세요.")
        if config.live_order_scope:
            raise HTTPException(
                409,
                "이전 소액 주문의 한정 승인 범위가 남아 있습니다. 자동 실행 전에 서버 승인 범위를 정리하세요.",
            )
        if payload.confirmation != "실전 자동매매 시작 확인":
            raise HTTPException(422, "종목·계좌·매매 비중·최대 수량을 확인하고 자동매매를 승인하세요.")
        authorization = automatic_connection(db, user, row.broker)
        if row.value.get("buy_enabled", True) and not row.value.get("buy_price"):
            raise HTTPException(422, "기준 매수가격을 입력하세요.")
    elif payload.action == "start":
        raise HTTPException(409, "실전 자동매매 실행 설정이 없습니다.")
    if payload.action == "schedule" and not payload.time:
        raise HTTPException(422, "예약 시간을 지정하세요.")
    runtime = automatic_runtime(db, user) if running else None
    value = {
        **row.value,
        "mode": "automatic",
        "repeat_trading": False,
        "buy_rule": "price",
        "active": running,
        "worker_status": {
            "state": "waiting" if running else "stopped",
            "message": "사용자 실행 요청 저장",
            "checked_at": now().isoformat(),
        },
        "auto_control": {
            "action": payload.action,
            "state": ("scheduled" if payload.action == "schedule" else "running")
            if running
            else "scheduled_blocked"
            if payload.action == "schedule"
            else "stopped",
            "requested_at": now().isoformat(),
            "generation": row.value.get("auto_control", {}).get("generation", str(uuid4()))
            if was_active and running
            else str(uuid4()),
            # Only an explicit stopped -> start approval can add to an existing position.
            # A subsequent global pause/resume changes the runtime version and revokes it.
            "held_entry_runtime_version": runtime["version"]
            if running and payload.action == "start" and not was_active and not runtime["paused"]
            else None,
        },
    }
    if payload.action == "schedule":
        value["trade_time"] = payload.time
        value["auto_control"]["time"] = payload.time
    if running:
        value["auto_authorization"] = authorization
        value["auto_market_buy_above"] = value.get("buy_condition", "at_or_below") == "at_or_above"
    row.value = value
    row.version += 1
    row.updated_at = now()
    audit(
        db,
        user,
        "trading.auto_" + payload.action,
        row.id,
        {**plan_detail(row), "time": payload.time, "active": running, "reason": stop_reason},
    )
    db.commit()
    if config is not None and (running or was_active):
        notify_automatic_control(db, user, config, row, payload.action, stop_reason)
    return plan_view(row)


def held_entry_authorized(db, user, plan):
    """Whether this explicit start still permits its one held-position buy slot."""
    control = plan.value.get("auto_control", {})
    approved_version = control.get("held_entry_runtime_version")
    if not plan.value.get("active") or control.get("action") != "start" or approved_version is None:
        return False
    runtime = automatic_runtime(db, user)
    return not runtime["paused"] and runtime["version"] == approved_version


class OrderConfirm(BaseModel):
    intent_id: str
    confirmation: Literal["실전 주문 확인"]


class CancelConfirm(BaseModel):
    confirmation: Literal["실전 취소 확인"]


class SelectionLog(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: Literal[
        "fraction",
        "auto_max_quantity",
        "buy_price",
        "buy_condition",
        "take_profit",
        "take_profit_enabled",
        "buy_enabled",
        "stop_loss_enabled",
        "stop_loss_rate",
        "stop_loss_price",
        "manual_sell_price",
        "manual_buy_price",
    ]
    value: int | str | None

    @model_validator(mode="after")
    def valid_selection(self):
        choices = {
            "buy_condition": {"at_or_below", "at_or_above"},
            "fraction": {10, 20, 50, 100},
            "take_profit": {3, 5, 7, 10, 15},
            "take_profit_enabled": {0, 1},
            "buy_enabled": {0, 1},
            "stop_loss_enabled": {0, 1},
            "stop_loss_rate": {5, 6, 7, 8, 9, 10, 15, 20},
        }
        if self.field in choices:
            if self.value not in choices[self.field]:
                raise ValueError("허용된 선택값이 아닙니다.")
        elif self.value is not None:
            maximum = 10000000 if self.field in {"auto_max_quantity", "manual_quantity"} else 1000000000
            if not isinstance(self.value, int) or not 1 <= self.value <= maximum:
                raise ValueError("가격·수량 범위가 잘못되었습니다.")
        return self


class PlanSettingUpdate(SelectionLog):
    version: int = Field(ge=1)


def update_plan_setting(db, user, plan_id, payload, config=None):
    """Patch one owner-scoped setting without resetting a running trading round."""
    if not user.active or user.role == "viewer":
        raise HTTPException(403, "매매 설정 변경 권한이 없습니다.")
    db.scalar(select(User).where(User.id == user.id, User.tenant_id == user.tenant_id).with_for_update())
    row = db.scalar(
        select(TradePlan)
        .where(
            TradePlan.id == plan_id,
            TradePlan.owner_id == user.id,
            TradePlan.tenant_id == user.tenant_id,
        )
        .with_for_update()
    )
    if not row:
        raise HTTPException(404, "본인의 매매 설정을 찾을 수 없습니다.")
    if row.version != payload.version:
        raise HTTPException(409, "다른 화면에서 설정이 변경되었습니다. 목록을 새로 조회하세요.")
    if row.value.get("archived"):
        raise HTTPException(409, "제거된 종목입니다. 매매 목록에 재추가한 뒤 변경하세요.")
    field, value = payload.field, payload.value
    if field in {"stop_loss_enabled", "take_profit_enabled", "buy_enabled"}:
        value = bool(value)
    if (
        row.value.get("active")
        and row.value.get("buy_enabled", True)
        and field == "buy_price"
        and value is None
        and row.value.get("buy_rule") == "price"
    ):
        raise HTTPException(422, "실행 중에는 기준 매수가격을 비울 수 없습니다.")
    if (
        row.value.get("active")
        and field == "buy_enabled"
        and value
        and row.value.get("buy_rule") == "price"
        and not row.value.get("buy_price")
    ):
        raise HTTPException(422, "매수 사용 전에 기준 매수가격을 입력하세요.")
    before = row.value.get(field)
    updated = {**row.value, field: value}
    if field == "buy_condition" and before != value:
        # Changing direction can introduce an unbounded market buy. Require a new
        # start/reservation approval instead of activating it through autosave.
        updated.update(active=False, auto_market_buy_above=False,
                       auto_control={**updated.get("auto_control", {}), "state": "stopped", "action": "stop"},
                       worker_status={"state": "stopped", "message": "매수 조건 변경 · 시작·예약 재승인 필요", "checked_at": now().isoformat()})
    if field == "auto_max_quantity":
        updated["quantity_limit_explicit"] = True
    row.value, row.version, row.updated_at = updated, row.version + 1, now()
    audit(
        db,
        user,
        "trading.setting_saved",
        row.id,
        {
            "plan_id": row.id,
            "code": row.code,
            "broker": row.broker,
            "field": field,
            "before": before,
            "value": value,
            "version": row.version,
        },
    )
    db.commit()
    return plan_view(row)


def stop_loss_hit(value, price=None, profit_rate=None):
    if not value.get("stop_loss_enabled"):
        return False
    limit = value.get("stop_loss_price")
    return bool(
        (limit is not None and price is not None and math.isfinite(price) and 0 < price <= limit)
        or (
            profit_rate is not None
            and math.isfinite(profit_rate)
            and profit_rate <= -value.get("stop_loss_rate", 5)
        )
    )


def buy_price_hit(value, price):
    target = value.get("buy_price")
    if type(price) not in (int, float) or not math.isfinite(price) or price <= 0 or type(target) not in (int, float) or not math.isfinite(target) or target <= 0:
        return False
    condition = value.get("buy_condition", "at_or_below")
    return price >= target if condition == "at_or_above" else price <= target if condition == "at_or_below" else False


def automatic_signal(
    value, at, is_session_day, price=None, profit_rate=None, forecast=None, *, entry_allowed=None
):
    """Pure decision function. A signal is NOT an executed order."""
    if not value.get("active") or not is_session_day:
        return None
    clock = at.astimezone(ZoneInfo("Asia/Seoul")).strftime("%H:%M:%S")
    if not "09:00:00" <= clock < "15:20:00":
        return None
    if stop_loss_hit(value, price, profit_rate):
        return "sell"
    if (
        value.get("take_profit_enabled", True)
        and profit_rate is not None
        and math.isfinite(profit_rate)
        and profit_rate >= value["take_profit"]
    ):
        return "sell"
    if not value.get("buy_enabled", True):
        return None
    if entry_allowed is False or value.get("buy_rule") != "price":
        return None
    if entry_allowed is None and value.get("trade_time") and clock[:5] != value["trade_time"]:
        return None
    return "buy" if buy_price_hit(value, price) else None


def plan_view(row):
    return {
        "id": row.id,
        "code": row.code,
        "broker": row.broker,
        "version": row.version,
        **{k: v for k, v in row.value.items() if k not in {"account_ciphertext", "auto_authorization"}},
    }


def order_view(row, timezone):
    return {
        "id": row.id,
        "plan_id": row.plan_id,
        "status": row.status,
        "created_at": local_time(row.created_at, timezone),
        **{k: v for k, v in row.value.items() if k not in {"account_ciphertext", "credential_scope"}},
    }


class TradingService:
    def __init__(self, config, gateway):
        self.config, self.gateway = config, gateway
        self.automatic_runtime_allowed = lambda: True

    def account(self, plan):
        raw = plan.value.get("account_ciphertext")
        return NamuhAuth(self.config).cipher().decrypt(raw.encode()).decode() if raw else ""

    def quote_metadata(self, db, user, plan, price):
        row = db.scalar(
            select(BrokerSnapshot)
            .where(
                BrokerSnapshot.owner_id == user.id,
                BrokerSnapshot.tenant_id == user.tenant_id,
                BrokerSnapshot.kind == "trade_quote",
                BrokerSnapshot.symbol == plan.code,
                BrokerSnapshot.value["plan_id"].as_string() == plan.id,
                BrokerSnapshot.value["broker"].as_string() == plan.broker,
                BrokerSnapshot.value["price"].as_float() == price,
            )
            .order_by(BrokerSnapshot.created_at.desc())
            .limit(1)
        )
        return {
            "reference_quote_id": row.id if row else None,
            "reference_quote_at": row.created_at.isoformat() if row else None,
        }

    def locked_user(self, db, user):
        return db.scalar(
            select(User)
            .where(User.id == user.id, User.tenant_id == user.tenant_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    @critical_reads
    def preview(self, db, user, payload, *, automatic=False):
        current = self.locked_user(db, user)
        if not current:
            raise HTTPException(404, "본인의 사용자 영역을 찾을 수 없습니다.")
        if not current.active or current.role == "viewer":
            raise HTTPException(403, "활성 매매 사용자만 주문을 준비할 수 있습니다.")
        existing = db.scalar(
            select(TradeOrder).where(
                TradeOrder.owner_id == user.id,
                TradeOrder.tenant_id == user.tenant_id,
                TradeOrder.request_key == payload.request_key,
            )
        )
        if existing:
            rebuilding = (
                automatic
                and existing.status in {"preview", "blocked"}
                and existing.plan_id == payload.plan_id
                and existing.value["side"] == payload.side
            )
            if not rebuilding and (
                existing.plan_id != payload.plan_id
                or existing.value["side"] != payload.side
                or existing.value.get("purpose", "normal") != ("automatic" if automatic else payload.purpose)
                or existing.value.get("limit_price") != payload.limit_price
            ):
                raise HTTPException(409, "이미 다른 주문에 사용된 요청키입니다.")
            if not automatic or existing.status not in {"preview", "blocked"}:
                return existing
        plan = db.scalar(
            select(TradePlan).where(
                TradePlan.id == payload.plan_id,
                TradePlan.owner_id == user.id,
                TradePlan.tenant_id == user.tenant_id,
            )
        )
        if not plan:
            raise HTTPException(404, "매매 종목을 찾을 수 없습니다.")
        if plan.value.get("archived"):
            raise HTTPException(409, "제거된 종목에는 새 주문을 만들 수 없습니다. 먼저 재추가하세요.")
        if automatic:
            self.check_automatic(db, user, plan)
        if not market_session()["is_open"]:
            raise HTTPException(409, "KRX 정규장이 아닙니다. 예약 주문으로 전환하지 않습니다.")
        capacity_args = (payload.limit_price,) if payload.limit_price is not None else ()
        maximum, cash, environment = self.gateway.capacity(
            db, user, plan, self.account(plan), payload.side, *capacity_args
        )
        if payload.purpose == "small_live_test" and environment != "live":
            raise HTTPException(409, "소액 실전 테스트는 본인의 실전 계좌만 사용할 수 있습니다.")
        # A market buy has no upper execution price, so its final amount cannot be
        # guaranteed. The only exception is the explicitly reapproved above-price
        # automatic market strategy. Manual/other automatic buys retain ceilings.
        if (
            self.config.live_orders_enabled
            and environment == "live"
            and payload.side == "buy"
            and payload.limit_price is None
            and not (automatic and plan.value.get("buy_condition") == "at_or_above" and plan.value.get("auto_market_buy_above") is True)
        ):
            raise HTTPException(422, "실전 매수는 금액 상한을 보장하는 지정가를 입력하세요.")
        price = self.gateway.quote(db, user, plan)
        if not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
            raise HTTPException(409, "유효한 최신 시세가 없어 주문을 차단했습니다.")
        quote_metadata = self.quote_metadata(db, user, plan, price)
        if maximum is None or maximum < 1:
            raise HTTPException(409, "주문가능 수량이 없습니다.")
        if payload.side == "buy":
            if cash is None or cash <= 0:
                raise HTTPException(409, "미수 없는 주문가능 현금이 없습니다.")
            maximum = min(maximum, math.floor(cash / (payload.limit_price or price)))
        quantity = payload.quantity or (
            plan.value["auto_max_quantity"]
            if automatic and payload.side == "buy" and plan.value.get("auto_max_quantity") is not None
            else math.floor(maximum * plan.value["fraction"] / 100)
        )
        if automatic and plan.value.get("auto_max_quantity") is not None and payload.side == "buy":
            quantity = min(quantity, plan.value["auto_max_quantity"])
        if quantity < 1 or quantity > maximum:
            raise HTTPException(422, "주문가능 범위 안의 정수 수량을 선택하세요.")
        row = (
            existing
            if existing is not None
            else TradeOrder(
                owner_id=user.id,
                tenant_id=user.tenant_id,
                plan_id=plan.id,
                request_key=payload.request_key,
                status="preview",
                value={
                    "code": plan.code,
                    "broker": plan.broker,
                    "environment": environment,
                    "side": payload.side,
                    "quantity": quantity,
                    "reference_price": price,
                    **quote_metadata,
                    "cash": cash,
                    "account_last4": plan.value.get("account_last4"),
                    "plan_version": plan.version,
                    "expires_at": (now() + timedelta(seconds=20)).isoformat(),
                    "name": plan.value["name"],
                    "notification": "not_sent",
                    "purpose": payload.purpose,
                    "adjustment_generation": plan.value.get("auto_control", {}).get("generation")
                    if not automatic and payload.purpose in {"normal", "position_adjustment"}
                    else None,
                    "limit_price": payload.limit_price,
                    "order_type": "limit" if payload.limit_price is not None else "market",
                },
            )
        )
        if existing is not None:
            # Rebuild an untransmitted automatic intent; never reuse an expired authorization.
            row.status = "preview"
            row.value = {
                **row.value,
                "quantity": quantity,
                "reference_price": price,
                **quote_metadata,
                "cash": cash,
                "environment": environment,
                "limit_price": payload.limit_price,
                "order_type": "limit" if payload.limit_price is not None else "market",
                "plan_version": plan.version,
                "expires_at": (now() + timedelta(seconds=20)).isoformat(),
            }
        if automatic:
            row.value = {
                **row.value,
                "purpose": "automatic",
                "auto_generation": plan.value.get("auto_control", {}).get("generation"),
            }
        db.add(row)
        if plan.broker == "namuh":
            from .user_brokers import personal_config

            row.value = {
                **row.value,
                "account_ciphertext": plan.value["account_ciphertext"],
                "credential_scope": NamuhAuth(personal_config(db, user, self.config, "namuh")).scope(),
            }
        else:
            from .kiwoom import KiwoomReader
            from .user_brokers import personal_config

            row.value = {
                **row.value,
                "credential_scope": KiwoomReader(personal_config(db, user, self.config, "kiwoom")).scope(
                    environment
                ),
            }
        db.flush()
        audit(db, user, "trading.order_preview", row.id, order_detail(row))
        db.commit()
        return row

    def check_automatic(self, db, user, plan):
        ensure_new_order_allowed(db, user, self.config)
        if server_safety(db, self.config)["automatic_locked"]:
            raise HTTPException(409, "서버 자동매매 차단 상태입니다. 모든 사용자 자동 주문이 차단됩니다.")
        if automatic_runtime(db, user)["paused"]:
            raise HTTPException(409, "내 자동매매 전체 일시정지 중 · 매수·익절·손절 대기")
        if (
            not self.config.live_worker_enabled
            or not self.config.automatic_orders_enabled
            or not self.config.live_orders_enabled
        ):
            raise HTTPException(409, "실전 자동매매 서버 잠금 상태입니다.")
        if self.config.live_order_scope:
            raise HTTPException(409, "한정 테스트 승인 범위에서는 자동 주문을 실행하지 않습니다.")
        if not self.automatic_runtime_allowed():
            raise HTTPException(409, "워커 종료 중이므로 자동 주문을 차단했습니다.")
        if (
            not plan
            or plan.owner_id != user.id
            or plan.tenant_id != user.tenant_id
            or not plan.value.get("active")
            or plan.value.get("archived")
            or plan.value.get("mode") != "automatic"
        ):
            raise HTTPException(409, "자동매매가 중지되었거나 설정이 변경되었습니다.")
        if plan.value.get("auto_authorization") != automatic_connection(db, user, plan.broker):
            raise HTTPException(409, "증권사 키·연결 설정이 변경되어 자동매매 재승인이 필요합니다.")

    def final_gate(self, db, user, row):
        # Serializes with safety/save/stop: the winner of this DB gate defines the boundary.
        if db.get_bind().dialect.name == "sqlite":
            # SQLite has no SELECT FOR UPDATE. Reserve its writer gate before the final reads.
            db.execute(
                update(User)
                .where(User.id == user.id, User.tenant_id == user.tenant_id)
                .values(active=User.active)
            )
        self.locked_user(db, user)
        db.expire_all()
        server_safety(db, self.config, lock=True)
        ensure_new_order_allowed(db, user, self.config)
        plan = db.scalar(
            select(TradePlan)
            .where(
                TradePlan.id == row.plan_id,
                TradePlan.owner_id == user.id,
                TradePlan.tenant_id == user.tenant_id,
            )
            .with_for_update()
        )
        if not plan or plan.value.get("archived") or plan.version != row.value["plan_version"]:
            raise HTTPException(409, "전송 직전 설정 변경을 확인하여 주문을 차단했습니다.")
        if row.value.get("credential_scope"):
            from .kiwoom import KiwoomReader
            from .user_brokers import personal_config

            scoped = personal_config(db, user, self.config, plan.broker)
            current_scope = (
                NamuhAuth(scoped).scope()
                if plan.broker == "namuh"
                else KiwoomReader(scoped).scope(row.value["environment"])
            )
            if row.value["credential_scope"] != current_scope:
                raise HTTPException(409, "주문 준비 이후 증권사 키가 변경되어 전송을 차단했습니다.")
        pending = db.scalars(
            select(TradeOrder).where(
                TradeOrder.owner_id == user.id,
                TradeOrder.tenant_id == user.tenant_id,
                TradeOrder.id != row.id,
                TradeOrder.status.in_(
                    [
                        "submitting",
                        "accepted",
                        "partial",
                        "unknown",
                        "cancel_submitting",
                        "cancel_pending",
                        "cancel_unknown",
                    ]
                ),
            )
        ).all()
        if any(other.value.get("broker") == plan.broker for other in pending):
            raise HTTPException(
                409, "전송 직전 동일 증권사의 미확정 주문을 확인하여 추가 주문을 차단했습니다."
            )
        if row.value.get("purpose") == "automatic":
            previews = db.scalars(
                select(TradeOrder).where(
                    TradeOrder.owner_id == user.id,
                    TradeOrder.tenant_id == user.tenant_id,
                    TradeOrder.status == "preview",
                    TradeOrder.id != row.id,
                )
            ).all()
            if any(o.value.get("broker") == plan.broker and adjustment_preview_pending(o) for o in previews):
                raise HTTPException(409, "추가매수·일부매도 최종 확인 중 · 자동 주문 대기")
        if not self.config.live_orders_enabled or not market_session()["is_open"]:
            raise HTTPException(409, "서버 잠금 또는 장 마감으로 주문을 차단했습니다.")
        from datetime import datetime

        if now() > aware(datetime.fromisoformat(row.value["expires_at"])):
            raise HTTPException(409, "주문 유효시간이 지나 전송을 차단했습니다.")
        if row.value.get("purpose") == "automatic":
            self.check_automatic(db, user, plan)
            if plan.value.get("auto_control", {}).get("generation") != row.value.get("auto_generation"):
                raise HTTPException(409, "이전 자동매매 실행 세대의 주문을 차단했습니다.")
            if row.value["side"] == "buy":
                if not plan.value.get("buy_enabled", True):
                    raise HTTPException(409, "매수 사용이 해제되어 자동매수를 차단했습니다.")
                from .live_worker import entry_window

                if not entry_window(plan.value, now().astimezone(ZoneInfo("Asia/Seoul"))):
                    raise HTTPException(409, "자동매수 실행 시간이 지나 주문을 차단했습니다.")
                if plan.value.get("buy_rule") != "price":
                    raise HTTPException(
                        409, "예측 기반 자동매수는 제거되었습니다. 가격 조건을 확인하고 다시 승인하세요."
                    )
        return plan

    @critical_reads
    def execute(self, db, user, intent_id):
        row = db.scalar(
            select(TradeOrder).where(
                TradeOrder.id == intent_id,
                TradeOrder.owner_id == user.id,
                TradeOrder.tenant_id == user.tenant_id,
            )
        )
        if not row:
            raise HTTPException(404, "본인의 주문을 찾을 수 없습니다.")
        audit(db, user, "trading.order_execution_requested", row.id, order_detail(row))
        db.commit()
        try:
            return self._execute(db, user, intent_id)
        except HTTPException as error:
            db.rollback()
            row = db.scalar(
                select(TradeOrder).where(
                    TradeOrder.id == intent_id,
                    TradeOrder.owner_id == user.id,
                    TradeOrder.tenant_id == user.tenant_id,
                )
            )
            if row:
                audit(
                    db,
                    user,
                    "trading.order_blocked",
                    row.id,
                    {
                        **order_detail(row),
                        "http_status": error.status_code,
                        "reason": str(error.detail)[:200],
                    },
                )
                db.commit()
            raise

    def _execute(self, db, user, intent_id):
        self.locked_user(db, user)
        row = db.scalar(
            select(TradeOrder)
            .where(
                TradeOrder.id == intent_id,
                TradeOrder.owner_id == user.id,
                TradeOrder.tenant_id == user.tenant_id,
            )
            .with_for_update()
        )
        if not row:
            raise HTTPException(404, "본인의 주문을 찾을 수 없습니다.")
        if row.status != "preview":
            return row
        ensure_new_order_allowed(db, user, self.config)
        if not self.config.live_orders_enabled:
            raise HTTPException(409, "서버의 실전 주문 안전잠금이 켜져 있습니다.")
        scope = self.config.live_order_scope
        if scope:
            actual = {
                "owner_email": user.email.lower(),
                "broker": row.value["broker"],
                "code": row.value["code"],
                "side": row.value["side"],
                "quantity": row.value["quantity"],
                "limit_price": row.value.get("limit_price"),
            }
            if set(scope) != set(actual) or any(actual[k] != v for k, v in scope.items()):
                raise HTTPException(403, "승인된 소액 주문 범위를 벗어났습니다.")
            completed = db.scalars(
                select(TradeOrder).where(
                    TradeOrder.owner_id == user.id,
                    TradeOrder.tenant_id == user.tenant_id,
                    TradeOrder.status.in_(["filled", "canceled"]),
                )
            ).all()
            if any(
                all(
                    r.value.get(k) == actual[k] for k in ("broker", "code", "side", "quantity", "limit_price")
                )
                for r in completed
                if not self.config.live_order_scope_started_at
                or aware(r.created_at) >= aware(self.config.live_order_scope_started_at)
            ):
                raise HTTPException(409, "승인된 1주 테스트가 완료되어 추가 주문은 잠겼습니다.")
        from datetime import datetime

        if now() > datetime.fromisoformat(row.value["expires_at"]):
            raise HTTPException(409, "주문 확인 시간이 만료되었습니다. 수량과 가격을 다시 확인하세요.")
        if not market_session()["is_open"]:
            raise HTTPException(409, "KRX 정규장이 아닙니다.")
        plan = db.get(TradePlan, row.plan_id)
        if plan.version != row.value["plan_version"]:
            raise HTTPException(409, "매매 설정이 변경되었습니다. 다시 확인하세요.")
        unresolved = db.scalars(
            select(TradeOrder).where(
                TradeOrder.owner_id == user.id,
                TradeOrder.tenant_id == user.tenant_id,
                TradeOrder.status.in_(
                    [
                        "submitting",
                        "accepted",
                        "partial",
                        "unknown",
                        "cancel_submitting",
                        "cancel_pending",
                        "cancel_unknown",
                    ]
                ),
            )
        ).all()
        if any(r.value.get("broker") == plan.broker for r in unresolved):
            raise HTTPException(
                409, "이 계좌에 체결·취소 확인 전인 주문이 있습니다. 증권사 원장을 확인하세요."
            )
        maximum, cash, environment = self.gateway.capacity(
            db,
            user,
            plan,
            self.account(plan),
            row.value["side"],
            *((row.value["limit_price"],) if row.value.get("limit_price") is not None else ()),
        )
        price = self.gateway.quote(db, user, plan)
        if row.value["side"] == "sell" and row.value.get("purpose") != "automatic":
            position = self.gateway.position(db, user, plan, self.account(plan))
            if position.get("environment") != row.value["environment"]:
                raise HTTPException(409, "매도 계좌 환경 확인 필요")
            row.value = {
                **row.value,
                "sale_cost_basis": position.get("purchase_price"),
                "sale_cost_basis_at": now().isoformat(),
                "pre_sale_quantity": position.get("quantity"),
            }
        if row.value.get("purpose") == "automatic":
            db.refresh(plan)
            self.check_automatic(db, user, plan)
            if row.value["environment"] != "live":
                raise HTTPException(409, "실전 계좌에서만 자동매매가 가능합니다.")
            if row.value["side"] == "buy":
                if not plan.value.get("buy_enabled", True):
                    raise HTTPException(409, "매수 사용이 해제되어 자동매수를 차단했습니다.")
                current_position = self.gateway.position(db, user, plan, self.account(plan))
                if current_position.get("environment") != "live":
                    raise HTTPException(409, "실전 계좌의 최신 잔고를 확인할 수 없어 자동매수를 차단했습니다.")
                if current_position.get("quantity", 0) > 0:
                    if not held_entry_authorized(db, user, plan):
                        raise HTTPException(409, "새 시작 승인 없이 보유종목 자동 추가매수를 차단했습니다.")
                    basis = current_position.get("purchase_price")
                    rate = (price / basis - 1) * 100 if basis and basis > 0 else None
                    if stop_loss_hit(plan.value, price, rate) or (
                        plan.value.get("take_profit_enabled", True)
                        and rate is not None
                        and rate >= plan.value["take_profit"]
                    ):
                        raise HTTPException(409, "보유종목의 익절·손절 조건이 우선하여 자동매수를 차단했습니다.")
                if not buy_price_hit(plan.value, price):
                    raise HTTPException(409, "최신 시세가 매수 가격 조건을 충족하지 않아 주문을 차단했습니다.")
                if plan.value.get("buy_condition", "at_or_below") == "at_or_above":
                    if plan.value.get("auto_market_buy_above") is not True or row.value.get("limit_price") is not None:
                        raise HTTPException(409, "가격 이상 시장가 매수의 실행 재승인이 필요합니다.")
                elif row.value.get("limit_price") is None or row.value["limit_price"] > plan.value["buy_price"]:
                    raise HTTPException(409, "가격 이하 매수의 지정가 상한이 없어 주문을 차단했습니다.")
            else:
                latest_position = self.gateway.position(db, user, plan, self.account(plan))
                if latest_position.get("environment") != "live":
                    raise HTTPException(409, "실전 계좌의 평균매입가를 확인할 수 없습니다.")
                basis = latest_position.get("purchase_price")
                rate = (price / basis - 1) * 100 if basis and basis > 0 else None
                stopping = stop_loss_hit(plan.value, price, rate)
                if not stopping and (
                    not plan.value.get("take_profit_enabled", True)
                    or rate is None
                    or rate + 1e-9 < plan.value["take_profit"]
                ):
                    raise HTTPException(409, "목표수익 미달로 매도를 차단했습니다.")
                row.value = {
                    **row.value,
                    "sale_cost_basis": basis,
                    "sale_cost_basis_at": now().isoformat(),
                    "auto_exit_reason": "stop_loss" if stopping else "take_profit",
                }
                db.flush()  # final_gate expires ORM state; retain the pre-sale broker basis.
        if (
            environment != row.value["environment"]
            or maximum is None
            or row.value["quantity"] > maximum
            or abs(price / row.value["reference_price"] - 1) > 0.02
            or (
                row.value["side"] == "buy"
                and (cash is None or row.value["quantity"] * (row.value.get("limit_price") or price) > cash)
            )
        ):
            raise HTTPException(409, "환경·수량·현금·시세가 변경되었습니다. 다시 확인하세요.")
        quote_metadata = self.quote_metadata(db, user, plan, price)
        row.value = {
            **row.value,
            "transmission_reference_price": price,
            "transmission_quote_at": quote_metadata["reference_quote_at"],
            "transmission_quote_id": quote_metadata["reference_quote_id"],
        }
        db.flush()
        self.final_gate(db, user, row)
        row.status = "submitting"
        row.value = {**row.value, "submit_started_at": now().isoformat()}
        audit(db, user, "trading.order_submitting", row.id, order_detail(row))
        db.commit()  # Durable before the external side effect. Never retry ambiguous submissions.
        attempted = False

        def authorize_transmission():
            nonlocal attempted
            self.final_gate(db, user, row)
            attempted = True

        try:
            extra = (
                {"limit_price": row.value["limit_price"]} if row.value.get("limit_price") is not None else {}
            )
            from .order_boundary import transmission_guard

            with transmission_guard(authorize_transmission):
                # Also protects adapters/mocks which do not use TradeGateway.request.
                self.final_gate(db, user, row)
                number = self.gateway.submit(
                    db, user, plan, self.account(plan), row.value["side"], row.value["quantity"], **extra
                )
            row.status = "accepted"
            row.value = {**row.value, "broker_order_no": number, "note": "주문 접수 · 체결 아님"}
        except HTTPException as error:
            row.status = "unknown" if attempted else "blocked"
            row.value = {
                **row.value,
                "note": "주문 결과 불명 · 재전송 금지" if attempted else str(error.detail),
            }
        except Exception:
            row.status = "unknown"
            row.value = {**row.value, "note": "주문 결과 불명 · 재전송 차단 · 증권사 원장 확인 필요"}
        audit(db, user, "trading.order_" + row.status, row.id, order_detail(row))
        db.commit()
        self.notify(db, user, row)
        return row

    @critical_reads
    def reconcile(self, db, user, order_id):
        if not user.active or user.role == "viewer":
            raise HTTPException(403, "활성 사용자만 체결 확인이 가능합니다.")
        order = db.scalar(
            select(TradeOrder)
            .where(
                TradeOrder.id == order_id,
                TradeOrder.owner_id == user.id,
                TradeOrder.tenant_id == user.tenant_id,
            )
            .with_for_update()
        )
        if not order:
            raise HTTPException(404, "주문을 찾을 수 없습니다.")
        if order.status not in {"accepted", "partial", "cancel_pending", "cancel_unknown"}:
            return order
        previous = (
            order.status,
            order.value.get("filled_quantity"),
            order.value.get("fill_price"),
            order.value.get("remaining_quantity"),
        )
        result = self.gateway.fills(db, user, order)
        cancel_pending = order.status in {"cancel_pending", "cancel_unknown"}
        order.status = (
            "canceled"
            if result.get("cancel_confirmed")
            else (
                "filled"
                if result["remaining_quantity"] == 0
                else (
                    "cancel_pending"
                    if cancel_pending
                    else "partial"
                    if result["filled_quantity"]
                    else "accepted"
                )
            )
        )
        order.value = {
            **order.value,
            **result,
            "fill_checked_at": now().isoformat(),
            "note": "전량 체결 · 매수 완료"
            if order.status == "filled" and order.value["side"] == "buy"
            else "전량 체결 · 매도 완료"
            if order.status == "filled"
            else "취소 완료 · 증권사 확인"
            if order.status == "canceled"
            else "취소 확인 중"
            if order.status == "cancel_pending"
            else "미체결 또는 부분 체결 · 주문 유지",
        }
        current = (
            order.status,
            order.value.get("filled_quantity"),
            order.value.get("fill_price"),
            order.value.get("remaining_quantity"),
        )
        if previous != current:
            audit(db, user, "trading.fill_checked", order.id, order_detail(order))
        from .realized_sales import save_sale

        save_sale(db, user, order)
        db.commit()
        if order.status in {"filled", "canceled"}:
            self.notify(db, user, order)
        return order

    def cancel(self, db, user, order_id):
        # Re-read the broker ledger first; never submit cancellation for a completed order.
        order = self.reconcile(db, user, order_id)
        if order.status not in {"accepted", "partial"}:
            return order
        if not self.config.live_orders_enabled:
            raise HTTPException(409, "주문 안전잠금 상태입니다.")
        scope = self.config.live_order_scope
        v = order.value
        if scope and scope != {
            "owner_email": user.email.lower(),
            "broker": v["broker"],
            "code": v["code"],
            "side": v["side"],
            "quantity": v["quantity"],
            "limit_price": v.get("limit_price"),
        }:
            raise HTTPException(403, "승인된 테스트 주문의 잔량만 취소할 수 있습니다.")
        db.refresh(order, with_for_update=True)
        if order.status not in {"accepted", "partial"}:
            return order
        order.status = "cancel_submitting"
        order.value = {
            **order.value,
            "cancel_quantity": order.value["remaining_quantity"],
            "cancel_requested_at": now().isoformat(),
            "note": "취소 전송 중 · 재전송 금지",
        }
        audit(db, user, "trading.cancel_requested", order.id, order_detail(order))
        db.commit()
        try:
            number = self.gateway.cancel(db, user, order)
            order.status = "cancel_pending"
            order.value = {**order.value, "cancel_order_no": number, "note": "취소 접수 · 완료 확인 대기"}
        except Exception:
            order.status = "cancel_unknown"
            order.value = {**order.value, "note": "취소 응답 불명 · 재전송 금지 · 증권사 확인 필요"}
        audit(db, user, "trading." + order.status, order.id, order_detail(order))
        db.commit()
        return order

    def notify(self, db, user, order):
        # Acceptance/partial/unknown are not completion. Never notify those states.
        if order.status not in {"filled", "canceled"}:
            return
        from .personal_connections import external_config

        cfg = external_config(db, user, self.config)
        selected = channels(db, user)
        # Claim before network side effects. Concurrent/repeated checks cannot resend.
        db.refresh(order, with_for_update=True)
        event = order.status
        deliveries = dict(order.value.get("notification_events", {}))
        if event in deliveries:
            db.commit()
            return
        deliveries[event] = "sending"
        order.value = {**order.value, "notification_events": deliveries}
        db.commit()
        text = completion_message(order)
        email_status = []
        for item in saved_lists(db, user):
            if not selected["email"] or not item.get("trade_notifications"):
                continue
            try:
                deliver(db, user, cfg, item, "NaStack Trading 매매 알림", text)
                email_status.append("sent")
            except HTTPException:
                email_status.append("failed")
        order.value = {
            **order.value,
            "email_notification": email_status or ["not_configured" if selected["email"] else "disabled"],
        }
        policy = preferences(db, user, cfg)
        if (
            not selected["telegram"]
            or not policy["enabled"]
            or not policy["token_configured"]
            or not policy["chat_id"]
            or not policy["owner_matches"]
        ):
            order.value = {
                **order.value,
                "notification": "not_configured" if selected["telegram"] else "disabled",
            }
            order.value = {**order.value, "notification_events": {**deliveries, event: "attempted"}}
            audit(
                db, user, "trading.notification_completed", order.id, {**order_detail(order), "event": event}
            )
            db.commit()
            return
        try:
            telegram_request(cfg, "sendMessage", {"chat_id": policy["chat_id"], "text": text})
            status = "sent"
        except Exception:
            status = "failed"
        order.value = {**order.value, "notification": status}
        order.value = {**order.value, "notification_events": {**deliveries, event: "attempted"}}
        audit(db, user, "trading.notification_completed", order.id, {**order_detail(order), "event": event})
        db.commit()


def trading_router(current_user, db_session, config, gateway, worker=None):
    router, service = APIRouter(), TradingService(config, gateway)

    def writable(user):
        if user.role == "viewer":
            raise HTTPException(403, "조회 전용 사용자는 매매 설정·주문을 변경할 수 없습니다.")

    @router.get("/api/trading/events")
    def events(
        page: int = Query(1, ge=1),
        code: str | None = Query(None, pattern=r"^\d{6}$"),
        user=Depends(current_user),
        db=Depends(db_session),
    ):
        scope = [
            AuditEvent.actor_id == user.id,
            AuditEvent.tenant_id == user.tenant_id,
            AuditEvent.action.like("trading.%"),
        ]
        if code:
            scope.append(AuditEvent.detail["code"].as_string() == code)
        total = db.scalar(select(func.count()).select_from(AuditEvent).where(*scope))
        rows = db.scalars(
            select(AuditEvent)
            .where(*scope)
            .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
            .offset((page - 1) * 10)
            .limit(10)
        )
        return {
            "items": [
                {
                    "id": r.id,
                    "action": r.action,
                    "target_id": r.target_id,
                    "detail": clean(r.detail),
                    "created_at": local_time(r.created_at, config.timezone),
                }
                for r in rows
            ],
            "total": total,
            "page": page,
            "page_size": 10,
        }

    @router.post("/api/trading/plans/{plan_id}/selection")
    def selection(plan_id: str, payload: SelectionLog, user=Depends(current_user), db=Depends(db_session)):
        writable(user)
        row = db.scalar(
            select(TradePlan).where(
                TradePlan.id == plan_id, TradePlan.owner_id == user.id, TradePlan.tenant_id == user.tenant_id
            )
        )
        if not row:
            raise HTTPException(404, "본인의 매매 설정을 찾을 수 없습니다.")
        audit(
            db,
            user,
            "trading.selection_changed",
            row.id,
            {
                "plan_id": row.id,
                "code": row.code,
                "broker": row.broker,
                "account_last4": row.value.get("account_last4"),
                "version": row.version,
                "field": payload.field,
                "value": payload.value,
            },
        )
        db.commit()
        return {"recorded": True}

    @router.patch("/api/trading/plans/{plan_id}/settings")
    def patch_setting(
        plan_id: str, payload: PlanSettingUpdate, user=Depends(current_user), db=Depends(db_session)
    ):
        return update_plan_setting(db, user, plan_id, payload, config)

    @router.get("/api/trading/worker")
    def worker_status(user=Depends(current_user), db=Depends(db_session)):
        plans = db.scalars(
            select(TradePlan).where(TradePlan.owner_id == user.id, TradePlan.tenant_id == user.tenant_id)
        ).all()
        return {
            "worker": worker.status() if worker else {"running": False},
            "plans": [
                {
                    "id": p.id,
                    "active": bool(p.value.get("active")),
                    "status": p.value.get("worker_status", {}),
                }
                for p in plans
            ],
        }

    @router.get("/api/trading/automatic-runtime")
    def read_automatic_runtime(user=Depends(current_user), db=Depends(db_session)):
        return automatic_runtime(db, user)

    @router.put("/api/trading/automatic-runtime")
    def write_automatic_runtime(payload: RuntimeUpdate, user=Depends(current_user), db=Depends(db_session)):
        return save_automatic_runtime(db, user, payload)

    @router.get("/api/trading/safety")
    def read_safety(user=Depends(current_user), db=Depends(db_session)):
        return safety(db, user, config)

    @router.put("/api/trading/safety")
    def write_safety(payload: SafetyUpdate, user=Depends(current_user), db=Depends(db_session)):
        writable(user)
        return save_safety(db, user, config, payload)

    @router.put("/api/trading/server-safety")
    def write_server_safety(payload: ServerSafetyUpdate, user=Depends(current_user), db=Depends(db_session)):
        return save_server_safety(db, user, config, payload)

    @router.get("/api/trading/notification-channels")
    def read_channels(user=Depends(current_user), db=Depends(db_session)):
        return channels(db, user)

    @router.put("/api/trading/notification-channels")
    def write_channels(payload: Channels, user=Depends(current_user), db=Depends(db_session)):
        writable(user)
        return save_channels(db, user, payload)

    @router.get("/api/trading/plans")
    def plans(user=Depends(current_user), db=Depends(db_session)):
        rows = db.scalars(
            select(TradePlan)
            .where(TradePlan.owner_id == user.id, TradePlan.tenant_id == user.tenant_id)
            .order_by(TradePlan.updated_at)
        ).all()
        orders = db.scalars(
            select(TradeOrder)
            .where(TradeOrder.owner_id == user.id, TradeOrder.tenant_id == user.tenant_id)
            .order_by(TradeOrder.created_at.desc())
            .limit(50)
        ).all()
        policy = preferences(db, user, config)
        safety_policy = safety(db, user, config)
        return {
            "items": [plan_view(r) for r in rows if not r.value.get("archived")],
            "buy_condition_version": 1,
            "archived_items": [plan_view(r) for r in rows if r.value.get("archived")],
            "orders": [order_view(r, config.timezone) for r in orders],
            "live_enabled": not safety_policy["server_locked"] and not safety_policy["new_orders_locked"],
            "safety": safety_policy,
            "live_scope": {k: v for k, v in config.live_order_scope.items() if k != "owner_email"}
            if config.live_order_scope.get("owner_email") == user.email.lower()
            else {},
            "automatic_enabled": config.live_worker_enabled
            and not safety_policy["automatic_locked"]
            and not safety_policy["server_locked"]
            and not config.live_order_scope,
            "automatic_note": "워커 실행 허용"
            if not safety_policy["automatic_locked"]
            and not safety_policy["server_locked"]
            and not config.live_order_scope
            else "서버 실행 잠금 · 자동매매 시작 전 서버 설정 확인",
            "worker": worker.status() if worker else {"running": False},
            "telegram_configured": bool(
                policy["enabled"]
                and policy["token_configured"]
                and policy["chat_id"]
                and policy["owner_matches"]
            ),
        }

    @router.put("/api/trading/plans")
    def save(payload: PlanInput, user=Depends(current_user), db=Depends(db_session)):
        writable(user)
        service.locked_user(db, user)
        instrument = db.scalar(
            select(Instrument).where(Instrument.tenant_id == user.tenant_id, Instrument.code == payload.code)
        )
        if not instrument or instrument.value.get("status") != "active":
            raise HTTPException(422, "등록된 활성 종목을 선택하세요.")
        if payload.active:
            raise HTTPException(
                409, "체결·취소 원장 대사 연결 전 자동 실전 활성화는 차단됩니다. 조건은 저장할 수 있습니다."
            )
        row = db.scalar(
            select(TradePlan).where(
                TradePlan.owner_id == user.id,
                TradePlan.tenant_id == user.tenant_id,
                TradePlan.broker == payload.broker,
                TradePlan.code == payload.code,
            )
        )
        if payload.version != (row.version if row else 0):
            raise HTTPException(409, "설정이 변경되었습니다. 목록을 새로 조회하세요.")
        if row and row.value.get("archived"):
            raise HTTPException(
                409, "제거된 종목은 보관 목록의 재추가 기능을 사용하세요. 기존 기록과 조건을 보존합니다."
            )
        value = payload.model_dump(exclude={"code", "broker", "account_no", "version", "confirm_auto"})
        value["quantity_limit_explicit"] = True
        value["name"] = instrument.value["name"]
        if payload.account_no:
            gateway.validate_account(db, user, payload.broker, payload.account_no)
            value["account_ciphertext"] = (
                NamuhAuth(config).cipher().encrypt(payload.account_no.encode()).decode()
            )
            value["account_last4"] = payload.account_no[-4:]
        elif row:
            value.update({k: row.value[k] for k in ("account_ciphertext", "account_last4") if k in row.value})
        if payload.broker == "namuh" and not value.get("account_ciphertext"):
            raise HTTPException(422, "나무증권 주문 계좌를 먼저 선택하세요.")
        if not row:
            row = TradePlan(
                owner_id=user.id,
                tenant_id=user.tenant_id,
                broker=payload.broker,
                code=payload.code,
                version=0,
            )
            db.add(row)
        previous = clean(row.value or {})
        row.value, row.version, row.updated_at = value, row.version + 1, now()
        db.flush()
        audit(
            db,
            user,
            "trading.plan_saved",
            payload.code,
            {
                "plan_id": row.id,
                "code": row.code,
                "broker": payload.broker,
                "version": row.version,
                "before": previous,
                "after": clean(value),
            },
        )
        db.commit()
        if previous.get("active"):
            notify_automatic_control(db, user, config, row, "stop", "조건 변경·저장으로 실행 승인 해제")
        return plan_view(row)

    @router.post("/api/trading/plans/{plan_id}/archive")
    def archive(plan_id: str, payload: PlanListChange, user=Depends(current_user), db=Depends(db_session)):
        return change_plan_listing(db, user, plan_id, payload.version, archived=True)

    @router.post("/api/trading/plans/{plan_id}/restore")
    def restore(plan_id: str, payload: PlanListChange, user=Depends(current_user), db=Depends(db_session)):
        return change_plan_listing(db, user, plan_id, payload.version, archived=False)

    @router.post("/api/trading/plans/{plan_id}/control")
    def control(plan_id: str, payload: PlanControl, user=Depends(current_user), db=Depends(db_session)):
        writable(user)
        plan = db.scalar(
            select(TradePlan).where(
                TradePlan.id == plan_id, TradePlan.owner_id == user.id, TradePlan.tenant_id == user.tenant_id
            )
        )
        if not plan:
            raise HTTPException(404, "본인의 매매 설정을 찾을 수 없습니다.")
        audit(
            db,
            user,
            "trading.auto_control_requested",
            plan.id,
            {**plan_detail(plan), "action": payload.action, "time": payload.time},
        )
        db.commit()
        try:
            return control_plan(db, user, plan_id, payload, config)
        except HTTPException as error:
            db.rollback()
            audit(
                db,
                user,
                "trading.auto_control_blocked",
                plan_id,
                {
                    "plan_id": plan_id,
                    "action": payload.action,
                    "reason": str(error.detail)[:200],
                    "http_status": error.status_code,
                },
            )
            db.commit()
            raise

    @router.get("/api/trading/plans/{plan_id}/quote")
    def trade_quote(plan_id: str, user=Depends(current_user), db=Depends(db_session)):
        plan = db.scalar(
            select(TradePlan).where(
                TradePlan.id == plan_id, TradePlan.owner_id == user.id, TradePlan.tenant_id == user.tenant_id
            )
        )
        if not plan:
            raise HTTPException(404, "본인의 매매 설정을 찾을 수 없습니다.")

        def latest():
            rows = db.scalars(
                select(BrokerSnapshot)
                .where(
                    BrokerSnapshot.owner_id == user.id,
                    BrokerSnapshot.tenant_id == user.tenant_id,
                    BrokerSnapshot.kind == "trade_quote",
                    BrokerSnapshot.symbol == plan.code,
                    BrokerSnapshot.value["plan_id"].as_string() == plan.id,
                    BrokerSnapshot.value["broker"].as_string() == plan.broker,
                )
                .order_by(BrokerSnapshot.created_at.desc())
                .limit(100)
            ).all()
            return next(
                (
                    r
                    for r in rows
                    if r.value.get("plan_id") == plan.id and r.value.get("broker") == plan.broker
                ),
                None,
            )

        snapshot = latest()
        deferred = None
        cadence = 10 if plan.broker == "namuh" else 1
        if (
            market_session()["is_open"]
            and user.role != "viewer"
            and (not snapshot or (now() - aware(snapshot.created_at)).total_seconds() >= cadence)
        ):
            from .namuh_reads import NamuhReadDeferred, read_lane

            try:
                with read_lane("display"):
                    service.gateway.quote(db, user, plan)
                db.commit()
                snapshot = latest()
            except NamuhReadDeferred as error:
                if not snapshot:
                    raise
                deferred = error
        if not snapshot:
            raise HTTPException(409, "계좌 증권사 시세 미조회 · 다른 증권사 가격으로 대체하지 않습니다.")
        value = {
            **snapshot.value,
            "quote_id": snapshot.id,
            "collected_at": local_time(snapshot.created_at, config.timezone),
            "stale": deferred is not None or (now() - aware(snapshot.created_at)).total_seconds() > 15,
            "refresh_after_ms": int(deferred.headers["Retry-After"]) * 1000 if deferred else cadence * 1000,
            "refresh_note": str(deferred.detail) if deferred else "",
        }
        history = db.scalar(
            select(BrokerSnapshot)
            .where(
                BrokerSnapshot.owner_id == user.id,
                BrokerSnapshot.tenant_id == user.tenant_id,
                BrokerSnapshot.kind == "market",
                BrokerSnapshot.symbol == plan.code,
            )
            .order_by(BrokerSnapshot.created_at.desc())
            .limit(1)
        )
        if history:
            value["chart"] = history.value.get("chart", [])
            if (
                aware(history.created_at).astimezone(ZoneInfo("Asia/Seoul")).date()
                == now().astimezone(ZoneInfo("Asia/Seoul")).date()
            ):
                value["reference_close"] = history.value.get("reference_close")
        return {"snapshot": value}

    @router.get("/api/trading/plans/{plan_id}/performance")
    def read_performance(plan_id: str, user=Depends(current_user), db=Depends(db_session)):
        from .trade_performance import performance

        return performance(db, user, plan_id, service)

    @router.post("/api/trading/plans/{plan_id}/performance/refresh")
    def refresh_performance(plan_id: str, user=Depends(current_user), db=Depends(db_session)):
        from .trade_performance import performance

        return performance(db, user, plan_id, service, refresh=True)

    @router.post("/api/trading/orders/preview")
    def preview(payload: OrderPreview, user=Depends(current_user), db=Depends(db_session)):
        writable(user)
        try:
            return order_view(service.preview(db, user, payload), config.timezone)
        except HTTPException as error:
            db.rollback()
            plan = db.scalar(
                select(TradePlan).where(
                    TradePlan.id == payload.plan_id,
                    TradePlan.owner_id == user.id,
                    TradePlan.tenant_id == user.tenant_id,
                )
            )
            audit(
                db,
                user,
                "trading.preview_blocked",
                plan.id if plan else user.id,
                {
                    **(plan_detail(plan) if plan else {}),
                    "side": payload.side,
                    "quantity": payload.quantity,
                    "limit_price": payload.limit_price,
                    "reason": str(error.detail)[:200],
                    "http_status": error.status_code,
                },
            )
            db.commit()
            raise

    @router.post("/api/trading/orders/confirm")
    def confirm(payload: OrderConfirm, user=Depends(current_user), db=Depends(db_session)):
        writable(user)
        return order_view(service.execute(db, user, payload.intent_id), config.timezone)

    @router.post("/api/trading/orders/{order_id}/dismiss")
    def dismiss(order_id: str, user=Depends(current_user), db=Depends(db_session)):
        writable(user)
        service.locked_user(db, user)
        order = db.scalar(
            select(TradeOrder)
            .where(
                TradeOrder.id == order_id,
                TradeOrder.owner_id == user.id,
                TradeOrder.tenant_id == user.tenant_id,
            )
            .with_for_update()
        )
        if not order:
            raise HTTPException(404, "본인의 주문을 찾을 수 없습니다.")
        if order.value.get("purpose") == "automatic":
            raise HTTPException(409, "자동 주문은 시작·정지 설정으로 관리하세요.")
        if order.status == "preview":
            order.status = "dismissed"
            order.value = {**order.value, "note": "사용자가 미리보기를 취소함 · 주문 전송 없음"}
            audit(db, user, "trading.order_preview_dismissed", order.id, order_detail(order))
            db.commit()
        return order_view(order, config.timezone)

    @router.post("/api/trading/orders/{order_id}/reconcile")
    def reconcile(order_id: str, user=Depends(current_user), db=Depends(db_session)):
        writable(user)
        return order_view(service.reconcile(db, user, order_id), config.timezone)

    @router.post("/api/trading/orders/{order_id}/cancel")
    def cancel(order_id: str, payload: CancelConfirm, user=Depends(current_user), db=Depends(db_session)):
        writable(user)
        try:
            return order_view(service.cancel(db, user, order_id), config.timezone)
        except HTTPException as error:
            db.rollback()
            row = db.scalar(
                select(TradeOrder).where(
                    TradeOrder.id == order_id,
                    TradeOrder.owner_id == user.id,
                    TradeOrder.tenant_id == user.tenant_id,
                )
            )
            if row:
                audit(
                    db,
                    user,
                    "trading.cancel_blocked",
                    row.id,
                    {
                        **order_detail(row),
                        "reason": str(error.detail)[:200],
                        "http_status": error.status_code,
                    },
                )
                db.commit()
            raise

    return router
