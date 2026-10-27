"""Gross app-confirmed sell P/L. Never guesses missing costs or charges."""

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from .db import aware, now
from .models import RealizedSale, TradeOrder
from .security import audit


def decimal(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def save_sale(db, user, order):
    v = order.value
    if order.owner_id != user.id or order.tenant_id != user.tenant_id:
        raise HTTPException(404, "본인의 주문만 반영할 수 있습니다.")
    if v.get("side") != "sell" or v.get("environment") != "live":
        return
    qty, price = decimal(v.get("filled_quantity")), decimal(v.get("fill_price"))
    if not qty or qty <= 0:
        return
    if qty != int(qty) or price is None or price <= 0:
        raise HTTPException(409, "매도 체결 수량·가격 확인 필요")
    row = db.scalar(select(RealizedSale).where(RealizedSale.order_id == order.id).with_for_update())
    if row and (row.owner_id != user.id or row.tenant_id != user.tenant_id):
        raise HTTPException(404, "본인의 손익 기록만 반영할 수 있습니다.")
    if row and row.value.get("excluded_at"):
        return None  # Explicitly removed historical entries must not reappear on sync.
    if row and qty < row.quantity:
        raise HTTPException(409, "체결 누적 수량 감소 · 손익 대조 필요")
    basis = decimal(v.get("sale_cost_basis"))
    scale = Decimal("0.000001")
    cost = (qty * basis).quantize(scale) if basis is not None and basis > 0 else None
    proceeds = (qty * price).quantize(scale)
    profit = proceeds - cost if cost is not None else None
    reason = v.get("auto_exit_reason") if v.get("purpose") == "automatic" else "manual"
    reason = reason if reason in {"manual", "take_profit", "stop_loss"} else "automatic"
    detail = {
        "name": v.get("name", v.get("code")),
        "reason": reason,
        "purchase_price": float(basis) if cost is not None else None,
        "fill_price": float(price),
        "cost_status": "confirmed" if cost is not None else "missing",
        "date_basis": "broker_order_day",
        "fees_tax": "excluded",
    }
    order.value = {
        **v,
        "realized_profit": float(profit) if profit is not None else None,
        "realized_return_rate": float(profit / cost * 100) if cost else None,
        "realized_cost_status": detail["cost_status"],
    }
    if row and (row.quantity, row.proceeds, row.cost, row.profit, row.value) == (
        int(qty),
        proceeds,
        cost,
        profit,
        detail,
    ):
        return row
    if not row:
        row = RealizedSale(
            owner_id=user.id,
            tenant_id=user.tenant_id,
            order_id=order.id,
            plan_id=order.plan_id,
            broker=v["broker"],
            code=v["code"],
            trade_day=aware(order.created_at).astimezone(ZoneInfo("Asia/Seoul")).date(),
        )
        db.add(row)
    row.quantity, row.proceeds, row.cost, row.profit = int(qty), proceeds, cost, profit
    row.value, row.updated_at = detail, now()
    audit(
        db,
        user,
        "trading.realized_updated",
        order.id,
        {
            "code": row.code,
            "broker": row.broker,
            "quantity": row.quantity,
            "proceeds": float(proceeds),
            "cost": float(cost) if cost is not None else None,
            "profit": float(profit) if profit is not None else None,
            **detail,
        },
    )
    return row


def exclude_missing_sale(db, user, sale_id):
    row = db.scalar(
        select(RealizedSale)
        .where(
            RealizedSale.id == sale_id,
            RealizedSale.owner_id == user.id,
            RealizedSale.tenant_id == user.tenant_id,
        )
        .with_for_update()
    )
    if not row:
        raise HTTPException(404, "본인의 손익 기록을 찾을 수 없습니다.")
    order = db.get(TradeOrder, row.order_id)
    if (
        row.cost is not None
        or row.profit is not None
        or not order
        or order.status not in {"filled", "canceled"}
    ):
        raise HTTPException(409, "완료된 원가 미확인 과거 매도만 제외할 수 있습니다.")
    if not row.value.get("excluded_at"):
        row.value = {
            **row.value,
            "excluded_at": now().isoformat(),
            "excluded_reason": "사용자 요청 · 과거 매입단가 미확인",
        }
        audit(db, user, "trading.realized_excluded", row.order_id, {"sale_id": row.id, "code": row.code})


def realized_router(current_user, db_session):
    router = APIRouter()

    @router.post("/api/trading/realized/sync")
    def sync(user=Depends(current_user), db=Depends(db_session)):
        if not user.active or user.role == "viewer":
            raise HTTPException(403, "활성 매매 사용자만 과거 체결을 반영할 수 있습니다.")
        orders = db.scalars(
            select(TradeOrder)
            .where(
                TradeOrder.owner_id == user.id,
                TradeOrder.tenant_id == user.tenant_id,
                TradeOrder.status.in_(["filled", "partial", "canceled", "cancel_pending", "accepted"]),
            )
            .with_for_update()
        ).all()
        count = 0
        for order in orders:
            if save_sale(db, user, order):
                count += 1
        db.commit()
        return {"sales": count}

    @router.get("/api/trading/realized")
    def report(
        start: date,
        end: date,
        period: Literal["day", "week", "month", "year"] = "day",
        broker: Literal["all", "namuh", "kiwoom"] = "all",
        code: str | None = None,
        user=Depends(current_user),
        db=Depends(db_session),
    ):
        if start > end or (end - start).days > 3660:
            raise HTTPException(422, "조회 기간은 최대 10년입니다.")
        query = select(RealizedSale).where(
            RealizedSale.owner_id == user.id,
            RealizedSale.tenant_id == user.tenant_id,
            RealizedSale.trade_day.between(start, end),
        )
        if broker != "all":
            query = query.where(RealizedSale.broker == broker)
        if code:
            query = query.where(RealizedSale.code == code)
        rows = db.scalars(query.order_by(RealizedSale.trade_day, RealizedSale.id)).all()
        groups, items, stocks = {}, [], {}
        total_profit, total_cost, missing = Decimal(0), Decimal(0), 0
        for row in rows:
            if row.value.get("excluded_at"):
                continue
            day = row.trade_day
            key = (
                str(day)
                if period == "day"
                else str(day - timedelta(days=day.weekday()))
                if period == "week"
                else day.strftime("%Y-%m")
                if period == "month"
                else str(day.year)
            )
            group = groups.setdefault(
                key, {"period": key, "profit": Decimal(0), "cost": Decimal(0), "missing": 0, "sales": 0}
            )
            group["sales"] += 1
            stock = stocks.setdefault(
                row.code,
                {
                    "code": row.code,
                    "profit": Decimal(0),
                    "cost": Decimal(0),
                    "proceeds": Decimal(0),
                    "quantity": 0,
                    "missing": 0,
                    "sales": 0,
                },
            )
            stock["name"] = row.value.get("name") or row.code
            stock["sales"] += 1
            stock["quantity"] += row.quantity
            stock["proceeds"] += row.proceeds
            if row.profit is None or row.cost is None:
                group["missing"] += 1
                stock["missing"] += 1
                missing += 1
            else:
                group["profit"] += row.profit
                group["cost"] += row.cost
                stock["profit"] += row.profit
                stock["cost"] += row.cost
                total_profit += row.profit
                total_cost += row.cost
            items.append(
                {
                    "id": row.id,
                    "order_id": row.order_id,
                    "day": str(day),
                    "code": row.code,
                    "broker": row.broker,
                    "quantity": row.quantity,
                    "proceeds": float(row.proceeds),
                    "cost": float(row.cost) if row.cost is not None else None,
                    "profit": float(row.profit) if row.profit is not None else None,
                    "return_rate": float(row.profit / row.cost * 100) if row.cost else None,
                    **row.value,
                }
            )

        def summary(profit, cost, unknown):
            return {
                "profit": None if unknown else float(profit),
                "known_profit": float(profit),
                "return_rate": float(profit / cost * 100) if cost and not unknown else None,
                "missing": unknown,
            }

        return {
            "summary": summary(total_profit, total_cost, missing),
            "groups": [
                {"period": g["period"], "sales": g["sales"], **summary(g["profit"], g["cost"], g["missing"])}
                for g in groups.values()
            ],
            "items": list(reversed(items)),
            "stocks": [
                {
                    "code": s["code"],
                    "name": s["name"],
                    "sales": s["sales"],
                    "quantity": s["quantity"],
                    "proceeds": float(s["proceeds"]),
                    "cost": None if s["missing"] else float(s["cost"]),
                    "known_cost": float(s["cost"]),
                    **summary(s["profit"], s["cost"], s["missing"]),
                }
                for s in sorted(stocks.values(), key=lambda s: s["code"])
            ],
            "note": "앱 확인 매도 체결 · 증권사 평균매입가 기준 · 수수료·세금 제외 · 주문일(KST) 귀속, 실제 체결시각 미제공 · 미확인 원가는 합계 확정 제외",
        }

    return router
