"""Paper execution only: no capacity, submit, notification or paid inference calls."""

import argparse
import copy
import math
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .config import Config
from .db import aware, connect, now
from .market import market_session
from .models import PaperEvent, PaperWallet, TradePlan, User
from .trading import automatic_signal

SEOUL = ZoneInfo("Asia/Seoul")


def initial():
    return {
        "enabled": False,
        "cash": 10000000.0,
        "initial_cash": 10000000.0,
        "fee_bps": 2,
        "slippage_bps": 5,
        "positions": {},
        "marks": {},
        "executed": {},
        "checkpoints": {},
    }


def evaluate(wallet, plan_id, rule, at, is_day, price, predicted=None, execute=False):
    """Mutates only a supplied paper copy. Shared cash, stable plan ordering, once/day/side."""
    day, clock = at.date().isoformat(), at.strftime("%H:%M:%S")
    if not is_day:
        return {"action": "skip", "reason": "휴장일"}
    if not "09:00:00" <= clock < "15:20:00":
        return {"action": "skip", "reason": "매매 감시 시간 외"}
    if price is None or not math.isfinite(price) or price <= 0:
        return {"action": "skip", "reason": "유효 시세 없음"}
    position = wallet["positions"].get(plan_id, {"quantity": 0, "cost": 0})
    profit = (price / position["cost"] - 1) * 100 if position["quantity"] else None
    value = {**rule, "active": True}
    side = automatic_signal(value, at, True, price=price, profit_rate=profit)
    if side == "buy" and rule.get("start_day") and rule["start_day"] != day:
        return {"action": "skip", "reason": "시작일 경과 · Paper 시작을 다시 누르세요."}
    if not side:
        return {"action": "skip", "reason": "설정 시각 외 또는 가격 조건 미충족·제거된 예측 조건"}
    key = f"{day}:{plan_id}:{side}"
    if key in wallet["executed"] or (side == "buy" and position["quantity"]):
        return {"action": "skip", "reason": "중복 매매 또는 이미 보유"}
    fill = price * (1 + wallet["slippage_bps"] / 10000 * (1 if side == "buy" else -1))
    fee = wallet["fee_bps"] / 10000
    fraction = rule.get("fraction", 10) / 100
    quantity = (
        math.floor(wallet["cash"] * fraction / (fill * (1 + fee)))
        if side == "buy"
        else math.floor(position["quantity"] * fraction)
    )
    if quantity < 1:
        return {"action": "skip", "reason": "가상 현금/매도 수량 부족"}
    result = {
        "action": side,
        "quantity": quantity,
        "price": round(fill, 4),
        "fee": round(fill * quantity * fee, 4),
        "realized_pnl": round((fill * (1 - fee) - position["cost"]) * quantity, 4)
        if side == "sell"
        else None,
        "reason": "가상 체결 (실제 체결 아님)",
        "executed": execute,
    }
    if execute:
        wallet["cash"] += fill * quantity * ((1 - fee) if side == "sell" else -(1 + fee))
        wallet["positions"][plan_id] = {
            "quantity": quantity if side == "buy" else position["quantity"] - quantity,
            "cost": fill * (1 + fee) if side == "buy" else position["cost"],
        }
        wallet["executed"][key] = True
    return result


def locked_wallet(db, user):
    # PostgreSQL FOR NO KEY UPDATE still serializes this owner's wallet work,
    # but permits the FK KEY SHARE lock taken by the separate token-refresh
    # session when it inserts an audit event referencing this user. FOR UPDATE
    # here deadlocks the caller waiting for that refresh session to commit.
    db.scalar(
        select(User)
        .where(User.id == user.id, User.tenant_id == user.tenant_id)
        .with_for_update(key_share=True)
    )
    row = db.scalar(
        select(PaperWallet).where(PaperWallet.owner_id == user.id, PaperWallet.tenant_id == user.tenant_id)
    )
    if row is None:
        row = PaperWallet(owner_id=user.id, tenant_id=user.tenant_id, value=initial())
        db.add(row)
        db.flush()
    return row


def scenario_suite():
    """Finite synthetic rule checks, independent of time, quotes and operational wallets."""
    base = {"buy_rule": "price", "buy_price": 1000, "trade_time": "09:00", "fraction": 100, "take_profit": 3}
    # (clock, open day, synthetic quote, hypothetical return, expected action)
    checkpoint = ("08:50:00", True, None, None, "skip")
    buy = ("09:00:00", True, 1000, None, "buy")
    sell = ("10:00:00", True, 1100, None, "sell")
    cases = [
        ("positive", "가격 조건 → 매수 → 목표수익 매도", {}, [checkpoint, buy, sell]),
        (
            "negative",
            "제거된 예측 매수 차단",
            {"buy_rule": "forecast"},
            [("08:50:00", True, None, -0.02, "skip"), (*buy[:4], "skip")],
        ),
        (
            "zero",
            "매수가격 누락 차단",
            {"buy_price": None},
            [("08:50:00", True, None, 0, "skip"), (*buy[:4], "skip")],
        ),
        (
            "missing",
            "매수 사용 꺼짐 → 매수 안 함",
            {"buy_enabled": False},
            [("08:50:00", True, None, None, "skip"), (*buy[:4], "skip")],
        ),
        ("checkpoint_missing", "예측 확인 없이 가격 조건만 사용", {}, [buy]),
        ("holiday", "휴장 → 매매 차단", {}, [("09:00:00", False, 1000, 0.02, "skip")]),
        ("before_open", "장 시작 전 → 대기", {}, [("08:49:00", True, 1000, 0.02, "skip")]),
        ("after_close", "장 마감 후 → 대기", {}, [("16:00:00", True, 1000, 0.02, "skip")]),
        (
            "missed_window",
            "매수 시간 경과 → 매수 차단",
            {},
            [checkpoint, ("09:01:00", True, 1000, None, "skip")],
        ),
        (
            "duplicate",
            "매수·매도 중복 실행 차단",
            {},
            [checkpoint, buy, (*buy[:4], "skip"), sell, (*sell[:4], "skip")],
        ),
        (
            "invalid_quote",
            "시세 누락·0원 → 주문 차단",
            {},
            [checkpoint, ("09:00:00", True, None, None, "skip"), ("09:00:00", True, 0, None, "skip")],
        ),
        ("cash", "가상 현금 부족 → 매수 차단", {}, [checkpoint, ("09:00:00", True, 20000000, None, "skip")]),
        (
            "below_profit",
            "목표수익 미달·손실 → 보유",
            {},
            [checkpoint, buy, ("10:00:00", True, 1010, None, "skip"), ("10:01:00", True, 900, None, "skip")],
        ),
        (
            "price",
            "설정가격 초과 대기·이하 매수",
            {"buy_rule": "price", "buy_price": 1000},
            [("09:00:00", True, 1001, None, "skip"), buy, sell],
        ),
    ]
    for fraction in (10, 20, 50):
        cases.append(
            (
                f"fraction_{fraction}",
                f"가상 현금·보유수량 {fraction}% 매매",
                {"fraction": fraction},
                [checkpoint, buy, sell],
            )
        )
    for trade_time in ("09:00", "10:00", "11:00"):
        cases.append(
            (
                f"time_{trade_time}",
                f"단일 매매시점 {trade_time} 가격 확인·중복 차단",
                {"trade_time": trade_time},
                [
                    (trade_time + ":00", True, 1001, None, "skip"),
                    (trade_time + ":10", True, 1000, 0.02, "buy"),
                    (trade_time + ":20", True, 1000, 0.02, "skip"),
                ],
            )
        )
    results = []
    started = time.monotonic()
    for identity, title, overrides, steps in cases:
        wallet, checks = initial(), []
        try:
            for clock, is_day, price, predicted, expected in steps:
                at = datetime.fromisoformat("2026-10-22T" + clock).replace(tzinfo=SEOUL)
                result = evaluate(
                    wallet, "synthetic:373220", {**base, **overrides}, at, is_day, price, predicted, True
                )
                passed = result["action"] == expected and wallet["cash"] >= 0
                if expected in ("buy", "sell"):
                    passed = passed and result.get("quantity", 0) > 0
                checks.append({"clock": clock, "expected": expected, "passed": passed, **result})
            passed = all(step["passed"] for step in checks)
        except Exception:
            passed = False
            checks.append({"passed": False, "reason": "시나리오 처리 오류 · 나머지 검사는 계속 실행"})
        results.append({"id": identity, "title": title, "passed": passed, "steps": checks})
    passed = sum(case["passed"] for case in results)
    return {
        "version": 1,
        "total": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "duration_ms": round((time.monotonic() - started) * 1000, 2),
        "cases": results,
        "orders_sent": False,
        "operational_wallet_changed": False,
        "assumptions": "고정 가상 거래일·시각, 가상 시세 1,000원 등 입력값, 가상 수익률 ±2%, 목표 3%. 실제 LG 시세·예측·증권사 API 검증 아님.",
    }


def tick(db, user, row, at, quote, execute):
    cycle_started = time.monotonic()
    wallet = copy.deepcopy(row.value)
    # Calendar gate via noon, independent of current time-of-day.
    is_day = market_session(at.replace(hour=12, minute=0, second=0))["is_open"]
    plans = db.scalars(
        select(TradePlan)
        .where(TradePlan.owner_id == user.id, TradePlan.tenant_id == user.tenant_id)
        .order_by(TradePlan.id)
    ).all()
    if wallet.get("pilot"):
        plans = [
            SimpleNamespace(
                id="pilot:373220",
                code="373220",
                broker="kiwoom",
                version=1,
                value={
                    "mode": "automatic",
                    "buy_rule": "forecast",
                    "forecast_source": "daily",
                    "fraction": 10,
                    "take_profit": 3,
                    "trade_time": wallet.get("trade_time", "09:00"),
                },
            )
        ]
    results = []
    for plan in plans:
        if plan.value.get("archived") or plan.value.get("mode") != "automatic":
            continue
        try:
            price = (
                read_quote(db, user, plan, quote)
                if is_day and "09:00" <= at.strftime("%H:%M") < "15:20"
                else None
            )
            decision_at = at + timedelta(seconds=time.monotonic() - cycle_started)
            rule = {"trade_time": "09:00", **plan.value}
            if wallet.get("start_on_click"):
                rule.update(trade_time=wallet["trade_time"], start_day=wallet["start_day"])
            result = evaluate(
                wallet,
                plan.id,
                rule,
                decision_at,
                is_day,
                price,
                None,
                execute,
            )
            if price is not None:
                wallet.setdefault("marks", {})[plan.id] = price
        except Exception:
            result = {"action": "skip", "reason": "조회 실패 (인증/연결 확인 필요)"}
        result.update(code=plan.code, plan_id=plan.id, broker=plan.broker, at=at.isoformat(), paper=True)
        result["plan_version"] = plan.version
        result["rule"] = {
            key: plan.value.get(key)
            for key in ("buy_rule", "buy_price", "fraction", "take_profit", "forecast_source", "trade_time")
        }
        results.append(result)
        if execute:
            db.add(PaperEvent(owner_id=user.id, tenant_id=user.tenant_id, value=result))
    if execute:
        wallet.update(last_tick=now().isoformat(), next_tick=(now() + timedelta(seconds=30)).isoformat())
        row.value = wallet
    return results


def read_quote(db, user, plan, quote):
    # Token refresh may commit; isolate it from the wallet transaction/owner lock.
    with Session(db.bind) as external_db:
        if db.bind.dialect.name == "postgresql":
            # Fail closed instead of hanging a Paper request on another DB lock.
            # LOCAL is scoped to this auth transaction; it cannot leak via pool reuse.
            external_db.execute(text("SET LOCAL lock_timeout = '5s'"))
        return quote(external_db, user, plan)


class Control(BaseModel):
    enabled: bool


class Schedule(BaseModel):
    trade_time: str = Field(pattern=r"^(09|1[0-4]):[0-5][0-9]$|^15:[01][0-9]$")
    version: int = Field(default=0, ge=0)


class Scenario(BaseModel):
    plan_id: str
    at: datetime
    price: float = Field(gt=0, allow_inf_nan=False)
    predicted_return: float | None = Field(default=None, allow_inf_nan=False)


def paper_router(current_user, db_session, gateway):
    router = APIRouter()

    def writable(user):
        if user.role == "viewer":
            raise HTTPException(403, "조회 전용 사용자입니다.")

    @router.get("/api/trading/paper")
    def status(user=Depends(current_user), db=Depends(db_session)):
        row = db.scalar(
            select(PaperWallet).where(
                PaperWallet.owner_id == user.id, PaperWallet.tenant_id == user.tenant_id
            )
        )
        events = db.scalars(
            select(PaperEvent)
            .where(PaperEvent.owner_id == user.id, PaperEvent.tenant_id == user.tenant_id)
            .order_by(PaperEvent.created_at.desc())
            .limit(50)
        ).all()
        value = row.value if row else initial()
        last = value.get("worker_seen")
        equity = value["cash"] + sum(
            p["quantity"] * value.get("marks", {}).get(key, p["cost"])
            for key, p in value["positions"].items()
        )
        return {
            "wallet": value,
            "equity": equity,
            "pnl": equity - value["initial_cash"],
            "worker_alive": bool(
                last and (now() - aware(datetime.fromisoformat(last))).total_seconds() < 120
            ),
            "events": [e.value for e in events],
            "live_orders": False,
            "latest_suite": (
                db.scalar(
                    select(PaperEvent)
                    .where(
                        PaperEvent.owner_id == user.id,
                        PaperEvent.tenant_id == user.tenant_id,
                        PaperEvent.value["action"].as_string() == "scenario_suite",
                    )
                    .order_by(PaperEvent.created_at.desc())
                    .limit(1)
                )
                or SimpleNamespace(value=None)
            ).value,
        }

    @router.post("/api/trading/paper/scenario-suite")
    def run_suite(user=Depends(current_user), db=Depends(db_session)):
        writable(user)
        result = scenario_suite()
        value = {
            "action": "scenario_suite",
            "paper": True,
            "code": "373220",
            "at": now().isoformat(),
            "suite": result,
            "reason": f"일괄 검사 완료 · {result['passed']}/{result['total']} 통과",
        }
        db.add(PaperEvent(owner_id=user.id, tenant_id=user.tenant_id, value=value))
        db.commit()
        return value

    @router.put("/api/trading/paper")
    def control(payload: Control, user=Depends(current_user), db=Depends(db_session)):
        writable(user)
        row = locked_wallet(db, user)
        row.value = {**row.value, "enabled": payload.enabled}
        result = []
        if payload.enabled:
            at = now().astimezone(SEOUL)
            row.value = {
                **row.value,
                "start_on_click": True,
                "trade_time": at.strftime("%H:%M"),
                "start_day": at.date().isoformat(),
                "started_at": at.isoformat(),
            }
            suite = scenario_suite()
            db.add(
                PaperEvent(
                    owner_id=user.id,
                    tenant_id=user.tenant_id,
                    value={
                        "action": "scenario_suite",
                        "paper": True,
                        "code": "373220",
                        "at": now().isoformat(),
                        "suite": suite,
                        "reason": f"시작 즉시 일괄 검사 · {suite['passed']}/{suite['total']} 통과",
                    },
                )
            )
            result = tick(db, user, row, at, gateway.quote, True)
        db.add(
            PaperEvent(
                owner_id=user.id,
                tenant_id=user.tenant_id,
                value={
                    "action": "start" if payload.enabled else "pause",
                    "paper": True,
                    "at": now().isoformat(),
                },
            )
        )
        db.commit()
        return {
            "enabled": payload.enabled,
            "results": result,
            "note": "Paper 시작 즉시 일괄 시나리오 및 현재 조건 검사 완료 · 실제 주문 없음"
            if payload.enabled
            else "Paper 중지",
        }

    @router.put("/api/trading/paper/schedule")
    def schedule(payload: Schedule, user=Depends(current_user), db=Depends(db_session)):
        writable(user)
        row = locked_wallet(db, user)
        if row.value.get("enabled"):
            raise HTTPException(409, "Paper를 일시중지한 뒤 매매시점을 변경하세요.")
        if row.value.get("schedule_version", 0) != payload.version:
            raise HTTPException(409, "매매시점이 변경되었습니다. 새로 조회하세요.")
        row.value = {**row.value, "trade_time": payload.trade_time, "schedule_version": payload.version + 1}
        db.add(
            PaperEvent(
                owner_id=user.id,
                tenant_id=user.tenant_id,
                value={
                    "action": "schedule_saved",
                    "paper": True,
                    "at": now().isoformat(),
                    "reason": f"Paper 파일럿 매매시점 {payload.trade_time} · Asia/Seoul",
                },
            )
        )
        db.commit()
        return {"trade_time": payload.trade_time, "version": payload.version + 1}

    @router.put("/api/trading/paper/pilot")
    def pilot(user=Depends(current_user), db=Depends(db_session)):
        writable(user)
        row = locked_wallet(db, user)
        if row.value.get("enabled"):
            raise HTTPException(409, "먼저 Paper를 중지한 뒤 대상을 바꾸세요.")
        if any(
            p.get("quantity", 0) > 0 and key != "pilot:373220"
            for key, p in row.value.get("positions", {}).items()
        ):
            raise HTTPException(409, "다른 종목의 가상 보유분이 있어 대상 변경을 차단합니다.")
        row.value = {**row.value, "pilot": True}
        db.add(
            PaperEvent(
                owner_id=user.id,
                tenant_id=user.tenant_id,
                value={
                    "action": "pilot_selected",
                    "code": "373220",
                    "reason": "Paper 대상 LG에너지솔루션으로 제한·일봉예측/가상 현금 10%/목표 3%",
                    "at": now().isoformat(),
                    "paper": True,
                },
            )
        )
        db.commit()
        return {"code": "373220", "note": "Paper 대상 LG에너지솔루션·기존 매매 목록 미변경"}

    @router.post("/api/trading/paper/check")
    def check(user=Depends(current_user), db=Depends(db_session)):
        writable(user)
        row = locked_wallet(db, user)
        result = tick(db, user, row, now().astimezone(SEOUL), gateway.quote, False)
        db.rollback()
        db.add(
            PaperEvent(
                owner_id=user.id,
                tenant_id=user.tenant_id,
                value={
                    "action": "check",
                    "reason": "주문 없는 조건 검사",
                    "results": result,
                    "paper": True,
                    "at": now().isoformat(),
                },
            )
        )
        db.commit()
        return {"results": result, "orders_sent": False}

    @router.post("/api/trading/paper/scenario")
    def scenario(payload: Scenario, user=Depends(current_user), db=Depends(db_session)):
        writable(user)
        plan = db.scalar(
            select(TradePlan).where(
                TradePlan.id == payload.plan_id,
                TradePlan.owner_id == user.id,
                TradePlan.tenant_id == user.tenant_id,
            )
        )
        if payload.plan_id == "pilot:373220":
            plan = SimpleNamespace(
                id="pilot:373220",
                code="373220",
                value={
                    "mode": "automatic",
                    "buy_rule": "forecast",
                    "forecast_source": "daily",
                    "fraction": 10,
                    "take_profit": 3,
                },
            )
        if not plan:
            raise HTTPException(404, "본인의 매매종목이 없습니다.")
        if payload.at.tzinfo is None:
            raise HTTPException(422, "시간대가 포함된 시각을 입력하세요.")
        at = payload.at.astimezone(SEOUL)
        if abs((at.date() - now().astimezone(SEOUL).date()).days) > 366:
            raise HTTPException(422, "시나리오 시각은 현재 기준 1년 이내로 입력하세요.")
        wallet = initial()
        is_day = market_session(at.replace(hour=12, minute=0, second=0))["is_open"]
        # Explicit hypothetical checkpoint, not a replay of historically available information.
        evaluate(
            wallet,
            plan.id,
            plan.value,
            at.replace(hour=8, minute=50, second=0),
            is_day,
            None,
            payload.predicted_return,
            True,
        )
        result = evaluate(
            wallet, plan.id, plan.value, at, is_day, payload.price, payload.predicted_return, execute=True
        )
        db.add(
            PaperEvent(
                owner_id=user.id,
                tenant_id=user.tenant_id,
                value={
                    **result,
                    "action": "scenario",
                    "simulated_action": result["action"],
                    "code": plan.code,
                    "paper": True,
                    "at": now().isoformat(),
                    "scenario_at": at.isoformat(),
                    "assumptions": {
                        "price": payload.price,
                        "predicted_return": payload.predicted_return,
                        "source": "user_input",
                    },
                },
            )
        )
        db.commit()
        return {
            "result": result,
            "wallet": wallet,
            "orders_sent": False,
            "note": "가정 입력 기반 단발 시나리오·운영 지갑 미변경",
        }

    return router


def work_once(factory, quote):
    with factory() as db:
        identities = list(db.execute(select(PaperWallet.owner_id, PaperWallet.tenant_id)))
    for owner, tenant in identities:
        with factory.begin() as db:
            user = db.scalar(
                select(User).where(User.id == owner, User.tenant_id == tenant, User.active.is_(True))
            )
            if not user or user.role == "viewer":
                continue
            row = locked_wallet(db, user)
            row.value = {**row.value, "worker_seen": now().isoformat()}
            if not row.value.get("enabled"):
                continue
            last = row.value.get("last_tick")
            if last and (now() - aware(datetime.fromisoformat(last))).total_seconds() < 25:
                continue
            tick(db, user, row, now().astimezone(SEOUL), quote, True)


def main():
    from .trade_gateway import TradeGateway

    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    config = Config()
    engine, factory = connect(config.database_url)
    try:
        while True:
            try:
                work_once(factory, TradeGateway(config).quote)
            except Exception:
                print("Paper worker cycle failed; no broker orders sent.", flush=True)
            if args.once:
                break
            time.sleep(30)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
