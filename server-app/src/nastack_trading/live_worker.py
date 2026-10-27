"""Single-leader live runner. Persistent plans + unique order slots survive restarts.

No Paper wallet, synthetic fills, paid analysis or automatic activation is used here.
The leader is database-wide; pending orders additionally serialize each user's broker.
"""

import hashlib
import math
import os
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import select, text

from .db import aware, now
from .market import market_session
from .models import TradeOrder, TradePlan, User
from .security import audit
from .trading import (
    OrderPreview,
    PlanControl,
    TradingService,
    adjustment_preview_pending,
    automatic_signal,
    control_plan,
    held_entry_authorized,
    stop_loss_hit,
)

SEOUL = ZoneInfo("Asia/Seoul")
PENDING = {"accepted", "partial", "cancel_pending", "cancel_unknown"}
UNRESOLVED = PENDING | {"submitting", "unknown", "cancel_submitting"}


@contextmanager
def leader(engine, key=2026102208):
    """Connection-owned PostgreSQL advisory lock; OS lock for local SQLite installs."""
    if engine.dialect.name == "postgresql":
        with engine.connect() as connection:
            acquired = connection.scalar(text(f"SELECT pg_try_advisory_lock({int(key)})"))
            try:
                yield bool(acquired)
            finally:
                if acquired:
                    connection.execute(text(f"SELECT pg_advisory_unlock({int(key)})"))
                    connection.commit()
        return
    identity = str(engine.url) if key == 2026102208 else f"{engine.url}:{key}"
    digest = hashlib.sha256(identity.encode()).hexdigest()[:24]
    with (Path(tempfile.gettempdir()) / f"nastack-live-{digest}.lock").open("a+b") as handle:
        acquired = False
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                handle.write(b"0")
                handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            pass
        try:
            yield acquired
        finally:
            if acquired:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def entry_window(value, at):
    """Price-only watch begins at the approved start/schedule; no forecast entry."""
    control = value.get("auto_control", {})
    if control.get("action") == "schedule":
        time = value.get("trade_time", "09:00")
        begin = datetime.fromisoformat(at.date().isoformat() + "T" + time).replace(tzinfo=SEOUL)
    else:
        try:
            requested = datetime.fromisoformat(control["requested_at"]).astimezone(SEOUL)
        except (KeyError, ValueError, TypeError):
            return False
        begin = (
            requested
            if requested.date() == at.date()
            else at.replace(hour=9, minute=0, second=0, microsecond=0)
        )
    return at >= begin and value.get("buy_rule") == "price"


class LiveWorker:
    def __init__(self, config, engine, factory, gateway):
        self.config, self.engine, self.factory = config, engine, factory
        self.service = TradingService(config, gateway)
        self.stop_event = threading.Event()
        self.service.automatic_runtime_allowed = lambda: not self.stop_event.is_set()
        self.thread = None
        self.last_tick = None
        self.is_leader = False
        self.last_error = None
        self.cursor = 0

    def start(self):
        if not self.config.live_worker_enabled:
            return
        self.thread = threading.Thread(target=self.run, name="nastack-live-trading", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=20)

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.tick()
                self.last_error = None
            except Exception:
                # Do not leak broker credentials, account bodies or tracebacks into the UI.
                self.last_error = "워커 점검 오류 · 주문 재전송 없이 다음 주기 확인"
            self.stop_event.wait(self.config.live_worker_poll_seconds)

    def status(self):
        return {
            "running": bool(self.thread and self.thread.is_alive()),
            "leader": self.is_leader,
            "last_tick": self.last_tick,
            "poll_seconds": self.config.live_worker_poll_seconds,
            "error": self.last_error,
        }

    def record(self, db, plan_id, owner_id, tenant_id, state, message):
        db.rollback()
        owner = db.scalar(
            select(User).where(User.id == owner_id, User.tenant_id == tenant_id).with_for_update()
        )
        if not owner:
            return
        plan = db.scalar(
            select(TradePlan)
            .where(TradePlan.id == plan_id, TradePlan.owner_id == owner_id, TradePlan.tenant_id == tenant_id)
            .with_for_update()
        )
        if plan:
            previous = plan.value.get("worker_status", {})
            if previous.get("state") != state or previous.get("message") != message:
                audit(db, owner, "trading.worker_state", plan_id, {"state": state, "message": message})
            plan.value = {
                **plan.value,
                "worker_status": {"state": state, "message": message, "checked_at": now().isoformat()},
            }
            db.commit()

    def tick(self):
        self.last_tick = now().isoformat()
        if not self.config.live_worker_enabled:
            return
        with leader(self.engine) as acquired:
            self.is_leader = acquired
            if not acquired:
                return
            with self.factory() as db:
                # Reconciliation is independent of new-order blocks and plan stop/manual state.
                pending = db.scalars(
                    select(TradeOrder)
                    .where(TradeOrder.status.in_(UNRESOLVED))
                    .order_by(TradeOrder.created_at)
                    .limit(200)
                ).all()
                pending_ids = [(r.id, r.owner_id, r.tenant_id) for r in pending]
            for order_id, owner, tenant in pending_ids:
                if self.stop_event.is_set():
                    return
                with self.factory() as db:
                    user = db.scalar(select(User).where(User.id == owner, User.tenant_id == tenant))
                    row = db.get(TradeOrder, order_id)
                    if not user or not user.active or user.role == "viewer" or not row:
                        continue
                    if row.status in {"submitting", "cancel_submitting"}:
                        # Manual and worker submissions may be in flight: never modify a recent claim.
                        age = row.value.get("submit_started_at") or row.value.get("cancel_requested_at")
                        if age and (now() - aware(datetime.fromisoformat(age))).total_seconds() < 120:
                            continue
                        row.status = "unknown" if row.status == "submitting" else "cancel_unknown"
                        row.value = {**row.value, "note": "재시작 후 전송 결과 확인 필요 · 재전송 차단"}
                        db.commit()
                    if row.status in PENDING:
                        try:
                            self.service.reconcile(db, user, row.id)
                        except Exception:
                            db.rollback()
            with self.factory() as db:
                ids = [
                    (p.id, p.owner_id, p.tenant_id)
                    for p in db.scalars(select(TradePlan).order_by(TradePlan.id))
                    if p.value.get("active")
                    and not p.value.get("archived")
                    and p.value.get("mode") == "automatic"
                ]
            # Rotate fairness across cycles instead of permanently favoring the first symbol.
            if ids:
                offset = self.cursor % len(ids)
                ids = ids[offset:] + ids[:offset]
                self.cursor += 1
            from .brokers import refresh_automatic_namuh
            from .trade_gateway import TradeGateway

            if isinstance(self.service.gateway, TradeGateway):
                refreshed = set()
                for _, owner, tenant in ids:
                    if (owner, tenant) in refreshed or self.stop_event.is_set():
                        continue
                    refreshed.add((owner, tenant))
                    with self.factory() as db:
                        user = db.scalar(select(User).where(User.id == owner, User.tenant_id == tenant))
                        if user:
                            try:
                                refresh_automatic_namuh(
                                    db, user, self.config, self.service.gateway.transports["namuh"]
                                )
                                from .kiwoom import KiwoomReader
                                from .models import KiwoomConnection

                                connection = db.scalar(select(KiwoomConnection).where(
                                    KiwoomConnection.owner_id == owner,
                                    KiwoomConnection.tenant_id == tenant,
                                ))
                                kiwoom_plans = db.scalars(select(TradePlan).where(
                                    TradePlan.owner_id == owner,
                                    TradePlan.tenant_id == tenant,
                                    TradePlan.broker == "kiwoom",
                                ))
                                active_kiwoom = any(
                                    p.value.get("active") and p.value.get("mode") == "automatic"
                                    and not p.value.get("archived") for p in kiwoom_plans
                                )
                                if (active_kiwoom and user.active and user.role != "viewer" and connection
                                        and connection.enabled and connection.environment == "live"
                                        and (not connection.token_expires_at or
                                             aware(connection.token_expires_at) <= now() + timedelta(minutes=10))):
                                    KiwoomReader(
                                        self.config, self.service.gateway.transports["kiwoom"]
                                    ).token(db, user, force=True)
                            except Exception:
                                db.rollback()  # Existing order gates still block an expired credential.
            for identity, owner, tenant in ids:
                if self.stop_event.is_set():
                    return
                with self.factory() as db:
                    try:
                        self.evaluate(db, identity, owner, tenant)
                    except HTTPException as error:
                        from .namuh_reads import NamuhReadDeferred

                        self.record(
                            db,
                            identity,
                            owner,
                            tenant,
                            "waiting"
                            if isinstance(error, NamuhReadDeferred)
                            else "error"
                            if error.status_code >= 500 or error.status_code == 429
                            else "blocked",
                            str(error.detail),
                        )
                    except Exception:
                        self.record(
                            db,
                            identity,
                            owner,
                            tenant,
                            "error",
                            "조회·처리 오류 · 주문 재전송 없이 다음 주기 확인",
                        )

    def evaluate(self, db, identity, owner, tenant):
        user = db.scalar(select(User).where(User.id == owner, User.tenant_id == tenant))
        plan = db.scalar(
            select(TradePlan).where(
                TradePlan.id == identity, TradePlan.owner_id == owner, TradePlan.tenant_id == tenant
            )
        )
        if not user or not user.active or user.role == "viewer" or not plan:
            return
        generation = plan.value.get("auto_control", {}).get("generation")
        orders = [
            r
            for r in db.scalars(
                select(TradeOrder)
                .where(
                    TradeOrder.plan_id == identity,
                    TradeOrder.owner_id == owner,
                    TradeOrder.tenant_id == tenant,
                )
                .order_by(TradeOrder.created_at, TradeOrder.id)
            )
            if (r.value.get("purpose") == "automatic" and r.value.get("auto_generation") == generation)
            or (
                r.value.get("purpose") in {"normal", "position_adjustment"}
                and r.value.get("adjustment_generation") == generation
            )
        ]
        completed_sells = [
            r
            for r in orders
            if r.status == "filled"
            and r.value.get("side") == "sell"
            and r.value.get("purpose") == "automatic"
        ]
        liquidated = any(
            r.value.get("side") == "sell"
            and r.value.get("purpose") in {"normal", "position_adjustment"}
            and r.status in {"filled", "canceled"}
            and r.value.get("pre_sale_quantity", 0) > 0
            and r.value.get("filled_quantity", 0) >= r.value["pre_sale_quantity"]
            for r in orders
        )
        if completed_sells or liquidated:
            control_plan(
                db,
                user,
                identity,
                PlanControl(version=plan.version, action="stop"),
                self.config,
                stop_reason="전량 매도 체결 확인 · 자동 종료 · 재매수 없음",
            )
            return self.record(db, identity, owner, tenant, "completed", "1회 매매 완료 · 자동 정지")
        self.service.check_automatic(db, user, plan)
        previous = plan.value.get("worker_status", {})
        if (
            previous.get("state") == "error"
            and previous.get("checked_at")
            and (now() - aware(datetime.fromisoformat(previous["checked_at"]))).total_seconds() < 30
        ):
            return
        if not market_session()["is_open"]:
            return self.record(db, identity, owner, tenant, "waiting", "정규장·거래일 대기")
        at = now().astimezone(SEOUL)
        unresolved = db.scalars(
            select(TradeOrder).where(
                TradeOrder.owner_id == owner,
                TradeOrder.tenant_id == tenant,
                TradeOrder.status.in_(UNRESOLVED | {"preview"}),
            )
        ).all()
        if any(
            r.value.get("broker") == plan.broker and (r.status in UNRESOLVED or adjustment_preview_pending(r))
            for r in unresolved
        ):
            return self.record(
                db, identity, owner, tenant, "waiting", "동일 증권사의 미확정 주문 대사 대기 · 신규 주문 차단"
            )
        if plan.value.get("auto_control", {}).get("action") == "schedule" and at.strftime(
            "%H:%M"
        ) < plan.value.get("trade_time", "09:00"):
            return self.record(db, identity, owner, tenant, "waiting", "예약 시간 대기")
        from .namuh_reads import read_lane

        with read_lane("monitor"):
            position = self.service.gateway.position(db, user, plan, self.service.account(plan))
        if position.get("environment") != "live":
            raise HTTPException(409, "실전 계좌가 아니므로 자동 주문 차단")
        with read_lane("monitor"):
            price = self.service.gateway.quote(db, user, plan)
        # Read adapters can hold credential-row locks. Release them before taking the user order gate.
        db.commit()
        if not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
            raise HTTPException(409, "유효한 최신 시세가 없어 주문 차단")
        held, basis = position["quantity"], position.get("purchase_price")
        if completed_sells and orders[-1].id == completed_sells[-1].id and held > 0:
            return self.record(
                db,
                identity,
                owner,
                tenant,
                "holding",
                "매도 후 계좌 잔량 확인 · 잔량 0주 확인 전 다음 회차 차단",
            )
        profit = round((price / basis - 1) * 100, 10) if held > 0 and basis and basis > 0 else None
        stopping = held > 0 and stop_loss_hit(plan.value, price, profit)
        round_number = len(completed_sells)
        buy_key = f"auto:{identity}:{generation}:{round_number}:buy"
        round_buy = next((order for order in orders if order.request_key == buy_key), None)
        buy_first = plan.value.get("buy_enabled", True)
        held_entry = held > 0 and held_entry_authorized(db, user, plan)
        entry_pending = (
            buy_first
            and (not held or held_entry)
            and (round_buy is None or round_buy.status in {"preview", "blocked"})
        )
        if (
            buy_first
            and not held
            and not entry_pending
            and not (round_buy.value.get("filled_quantity", 0) > 0)
            and not stopping
        ):
            return self.record(
                db, identity, owner, tenant, "waiting", "현재 회차 매수 체결 확인 필요 · 추가 주문 없음"
            )
        entry_allowed = (entry_pending or not held) and entry_window(plan.value, at)
        limit = None
        side = automatic_signal(
            plan.value,
            at,
            True,
            price=price,
            profit_rate=profit,
            entry_allowed=entry_allowed,
        )
        if stopping:
            side = "sell"
        elif side == "sell" and not held:
            side = None
        if side != "sell" and held > 0 and not entry_pending:
            watching = (
                ("목표수익·손절 감시" if plan.value.get("stop_loss_enabled") else "목표 수익률 감시")
                if plan.value.get("take_profit_enabled", True)
                else (
                    "손절만 감시 · 익절 꺼짐"
                    if plan.value.get("stop_loss_enabled")
                    else "자동 매도 조건 없음 · 익절·손절 꺼짐"
                )
            )
            return self.record(
                db,
                identity,
                owner,
                tenant,
                "holding",
                watching if basis else "매입단가 미확인 · 손절가격만 감시",
            )
        if side == "buy" and plan.value["buy_rule"] == "price":
            limit = None if plan.value.get("buy_condition") == "at_or_above" else int(plan.value["buy_price"])
        if not side:
            return self.record(
                db,
                identity,
                owner,
                tenant,
                "waiting",
                f"가격 매수 조건 미충족 또는 제거된 예측 조건 · {plan.broker} 현재가 {price:,.0f}원 / 기준 {plan.value.get('buy_price') or 0:,.0f}원",
            )
        # Durable round identity survives restart/day rollover; only a fully filled sell
        # plus a flat broker position permits the next buy. Uncertain/canceled slots
        # are not recycled automatically.
        round_number = len(completed_sells)
        key = f"auto:{identity}:{generation}:{round_number}:{side}"
        existing = db.scalar(
            select(TradeOrder).where(
                TradeOrder.owner_id == owner, TradeOrder.tenant_id == tenant, TradeOrder.request_key == key
            )
        )
        if existing and existing.status not in {"preview", "blocked"}:
            return self.record(
                db, identity, owner, tenant, "waiting", "현재 회차의 해당 주문 처리됨 · 중복 전송 안 함"
            )
        quantity = int(held) if side == "sell" else None
        intent = self.service.preview(
            db,
            user,
            OrderPreview(plan_id=identity, side=side, quantity=quantity, request_key=key, limit_price=limit),
            automatic=True,
        )
        intent.value = {**intent.value, "auto_purchase_price": basis, "auto_signal_price": price}
        db.commit()
        result = self.service.execute(db, user, intent.id)
        self.record(db, identity, owner, tenant, result.status, result.value.get("note", "체결 확인 대기"))
