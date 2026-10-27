"""Owner-scoped channel switches; separate from service credentials and recipient lists."""

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from .models import UserSettings
from .security import audit


def completion_message(order):
    """Plain text shared by Telegram/email; one fact per line for mobile readers."""
    if order.status not in {"filled", "canceled"}:
        raise ValueError("Completion notifications only")
    v = order.value
    side = "매수" if v["side"] == "buy" else "매도"
    broker = {"namuh": "나무증권 (NH투자증권)", "kiwoom": "영웅문 (키움증권)"}.get(
        v["broker"], "증권사 미확인"
    )
    title = f"{side} 체결 완료" if order.status == "filled" else f"{side} 주문 취소 완료"
    lines = [f"[NaStack {title}]", "", f"증권사: {broker}", f"종목: {v['name']} ({v['code']})", ""]
    if v.get("limit_price") is not None:
        lines.append(f"주문 지정가: {v['limit_price']:,.0f}원")
    lines.append(f"체결수량: {v.get('filled_quantity', 0):,}주")
    if v.get("filled_quantity") and v.get("fill_price") is not None:
        lines.append(f"평균 체결가: {v['fill_price']:,.0f}원")
    if order.status == "canceled":
        lines.append(f"취소수량: {v.get('canceled_quantity', 0):,}주")
    lines.append(f"미체결 잔량: {v['remaining_quantity']:,}주")
    if order.status == "filled" and v["side"] == "sell":
        from .trade_performance import average_sale

        result = average_sale(v)
        if result:
            lines.extend(
                [
                    "",
                    f"평균매입가: {v['sale_cost_basis']:,.2f}원",
                    f"실현 매매차익: {result['profit']:+,.0f}원",
                    f"실현 수익률: {result['return_rate']:+.3f}%",
                    "수수료·세금 제외",
                ]
            )
        else:
            lines.extend(["", "실현 수익률: 미확정", "매입원가·수수료·세금 대사 필요"])
    return "\n".join(lines)


def notify_automatic_control(db, user, config, plan, event, reason=""):
    """A committed control transition, not an order or a worker heartbeat.

    Claim durably before I/O: never resend an ambiguous notification automatically.
    Delivery failure must not undo a committed start/stop decision.
    """
    from .email_lists import deliver, saved_lists
    from .personal_connections import external_config
    from .telegram_settings import preferences, telegram_request
    from .trade_audit import plan_detail

    titles = {"start": "시작", "schedule": "예약", "stop": "종료·정지"}
    if event not in titles:
        raise ValueError("Unknown control event")
    if plan.owner_id != user.id or plan.tenant_id != user.tenant_id:
        raise HTTPException(404, "본인의 매매 설정을 찾을 수 없습니다.")
    detail = {**plan_detail(plan), "event": event}
    audit(db, user, "trading.auto_notification_requested", plan.id, detail)
    db.commit()
    selected = channels(db, user)
    broker = {"namuh": "나무증권", "kiwoom": "영웅문 (키움증권)"}.get(plan.broker, plan.broker)
    text = "\n".join(
        [
            f"[NaStack 자동매매 {titles[event]}]",
            "",
            f"증권사: {broker}",
            f"종목: {plan.value.get('name', plan.code)} ({plan.code})",
            "매매 방식: 전량 매도 완료 후 자동 정지 · 자동 재매수 없음",
            f"계좌: 끝자리 {plan.value.get('account_last4', '미확인')}",
            f"상태: {'예약 대기' if event == 'schedule' else '조건 감시 중' if event == 'start' else '신규 자동 주문 중지'}",
            *([f"예약 시각: {plan.value.get('trade_time')} (한국시간)"] if event == "schedule" else []),
            *([f"사유: {reason}"] if reason else []),
            "",
            "주문 접수·체결 알림과 별도입니다.",
            "기존 미체결 주문은 자동 취소되지 않습니다.",
        ]
    )
    result = {"email": "disabled", "telegram": "disabled"}
    try:
        cfg = external_config(db, user, config)
        if selected["email"]:
            results = []
            for item in saved_lists(db, user):
                if not item.get("trade_notifications"):
                    continue
                try:
                    deliver(db, user, cfg, item, "NaStack 자동매매 상태 알림", text)
                    results.append("sent")
                except Exception:
                    results.append("failed")
            result["email"] = ",".join(results) or "not_configured"
        if selected["telegram"]:
            policy = preferences(db, user, cfg)
            result["telegram"] = "not_configured"
            if all(policy[k] for k in ("enabled", "token_configured", "chat_id", "owner_matches")):
                try:
                    telegram_request(cfg, "sendMessage", {"chat_id": policy["chat_id"], "text": text})
                    result["telegram"] = "sent"
                except Exception:
                    result["telegram"] = "failed"
    except Exception:
        db.rollback()
        result = {k: "failed" if selected[k] else "disabled" for k in result}
    audit(
        db,
        user,
        "trading.auto_notification_completed",
        plan.id,
        {**detail, "notification": result["telegram"], "email_notification": result["email"]},
    )
    db.commit()


class Channels(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=0)
    email: bool
    telegram: bool


def channels(db, user):
    row = db.get(UserSettings, user.id)
    value = row.value if row and row.tenant_id == user.tenant_id else {}
    return {"version": 0, "email": True, "telegram": True, **value.get("trade_notification_channels", {})}


def save_channels(db, user, payload):
    if not user.active or user.role == "viewer":
        raise HTTPException(403, "알림 설정 변경 권한이 없습니다.")
    row = db.scalar(
        select(UserSettings)
        .where(UserSettings.user_id == user.id, UserSettings.tenant_id == user.tenant_id)
        .with_for_update()
    )
    if not row or channels(db, user)["version"] != payload.version:
        raise HTTPException(409, "알림 설정을 새로 조회하세요.")
    value = {**payload.model_dump(), "version": payload.version + 1}
    row.value = {**row.value, "trade_notification_channels": value}
    audit(db, user, "trading.notification_channels_saved", user.id, value)
    db.commit()
    return value
