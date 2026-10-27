"""Owner-scoped positions and explicitly limited app-ledger realized P/L."""

from collections import deque
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import select

from .db import aware, now
from .models import BrokerSnapshot, TradeOrder, TradePlan


def number(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def average_sale(value):
    """Gross realized result from a saved broker cost basis and confirmed sell fills."""
    basis = number(value.get("sale_cost_basis"))
    price, quantity = number(value.get("fill_price")), number(value.get("filled_quantity"))
    if value.get("side") != "sell" or any(v is None or v <= 0 for v in (basis, price, quantity)):
        return None
    cost = basis * quantity
    profit = (price - basis) * quantity
    return {"profit": float(profit), "cost": float(cost), "return_rate": float(profit / cost * 100)}


def realized(orders, day):
    """FIFO matched fills only. No fabricated cost basis, fees, or external trades."""
    lots, profit, cost, sells, incomplete = deque(), Decimal(0), Decimal(0), 0, False
    for order in orders:
        v = order.value
        if order.status not in {"filled", "partial", "canceled", "cancel_pending", "accepted"}:
            continue
        quantity, price = number(v.get("filled_quantity")), number(v.get("fill_price"))
        if quantity is None or quantity <= 0:
            continue
        created_day = aware(order.created_at).astimezone(ZoneInfo("Asia/Seoul")).date().isoformat()
        try:
            checked_day = (
                datetime.fromisoformat(v["fill_checked_at"])
                .astimezone(ZoneInfo("Asia/Seoul"))
                .date()
                .isoformat()
            )
        except (KeyError, ValueError, TypeError):
            checked_day = None
        # Cumulative fills spanning dates cannot be attributed without execution-level records.
        if price is None or price <= 0 or checked_day != created_day:
            if created_day == day or checked_day == day:
                incomplete = True
            lots.clear()
            continue
        if v.get("side") == "buy":
            lots.append([quantity, price])
        elif v.get("side") == "sell":
            average = average_sale(v)
            if created_day == day:
                sells += 1
                if average:
                    cost += Decimal(str(average["cost"]))
                    profit += Decimal(str(average["profit"]))
            left = quantity
            while left > 0 and lots:
                take = min(left, lots[0][0])
                if created_day == day and not average:
                    cost += take * lots[0][1]
                    profit += take * (price - lots[0][1])
                left -= take
                lots[0][0] -= take
                if lots[0][0] == 0:
                    lots.popleft()
            if left > 0 and created_day == day and not average:
                incomplete = True
    return {
        "day": day,
        "profit": None if incomplete else float(profit),
        "return_rate": float(profit / cost * 100) if cost and not incomplete else None,
        "closed_orders": sells,
        "incomplete": incomplete,
        "note": "앱에서 확인한 매도 체결 및 저장된 증권사 평균매입가 기준(미확인 과거 주문은 FIFO) · 수수료·세금 제외 · 증권사 전체 일일 손익 아님",
    }


def performance(db, user, plan_id, service, refresh=False):
    plan = db.scalar(
        select(TradePlan).where(
            TradePlan.id == plan_id, TradePlan.owner_id == user.id, TradePlan.tenant_id == user.tenant_id
        )
    )
    if not plan:
        raise HTTPException(404, "본인의 매매 설정을 찾을 수 없습니다.")
    orders = db.scalars(
        select(TradeOrder)
        .where(
            TradeOrder.owner_id == user.id,
            TradeOrder.tenant_id == user.tenant_id,
            TradeOrder.plan_id == plan.id,
        )
        .order_by(TradeOrder.created_at, TradeOrder.id)
    ).all()
    fill_times = []
    for order in orders:
        if order.value.get("filled_quantity", 0) > 0 and order.value.get("fill_checked_at"):
            try:
                fill_times.append(aware(datetime.fromisoformat(order.value["fill_checked_at"])))
            except (TypeError, ValueError):
                pass
    snapshots = db.scalars(
        select(BrokerSnapshot)
        .where(
            BrokerSnapshot.owner_id == user.id,
            BrokerSnapshot.tenant_id == user.tenant_id,
            BrokerSnapshot.kind == "trade_position",
            BrokerSnapshot.symbol == plan.code,
        )
        .order_by(BrokerSnapshot.created_at.desc())
    ).all()
    cached = next(
        (
            r
            for r in snapshots
            if r.value.get("plan_id") == plan.id
            and r.value.get("account_identity") == plan.value.get("account_ciphertext")
            and (plan.broker != "namuh" or r.value.get("quantity_basis") == "broker_residual_v3")
        ),
        None,
    )
    if refresh and (not user.active or user.role == "viewer"):
        raise HTTPException(403, "활성 매매 사용자만 잔고를 갱신할 수 있습니다.")
    needs_fill_refresh = bool(cached and fill_times and max(fill_times) > aware(cached.created_at))
    if refresh and (
        not cached or needs_fill_refresh or (now() - aware(cached.created_at)).total_seconds() >= 15
    ):
        from .namuh_reads import read_lane

        with read_lane("critical" if needs_fill_refresh else "display"):
            position = service.gateway.position(db, user, plan, service.account(plan))
        cached = BrokerSnapshot(
            owner_id=user.id,
            tenant_id=user.tenant_id,
            kind="trade_position",
            symbol=plan.code,
            environment=position["environment"],
            value={**position, "plan_id": plan.id, "account_identity": plan.value.get("account_ciphertext")},
            created_at=aware(datetime.fromisoformat(position["collected_at"]))
            if plan.broker == "namuh" and position.get("collected_at")
            else now(),
        )
        db.add(cached)
        db.commit()
    filtered = [
        r
        for r in orders
        if r.value.get("environment") == "live"
        and (
            plan.broker != "namuh"
            or r.value.get("account_ciphertext") == plan.value.get("account_ciphertext")
        )
    ]
    position = (
        {k: v for k, v in cached.value.items() if k not in {"account_identity", "plan_id"}}
        if cached
        else None
    )
    if position and fill_times and max(fill_times) > aware(cached.created_at):
        position = {**position, "stale": True, "note": "체결 후 잔고 갱신 대기 · 이전 수량·평균매입가"}
    return {
        "position": position,
        "daily": realized(filtered, now().astimezone(ZoneInfo("Asia/Seoul")).date().isoformat()),
    }
