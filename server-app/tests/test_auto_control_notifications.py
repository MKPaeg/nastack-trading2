from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from nastack_trading.models import AuditEvent, TradePlan, User
from nastack_trading.trade_notifications import notify_automatic_control


@pytest.mark.parametrize("event", ["start", "schedule", "stop"])
@pytest.mark.parametrize("email,telegram", [(False, True), (True, False), (False, False)])
def test_control_messages_respect_owner_channels(setup, monkeypatch, event, email, telegram):
    import nastack_trading.email_lists as mail
    import nastack_trading.personal_connections as personal
    import nastack_trading.telegram_settings as tg
    import nastack_trading.trade_notifications as notification

    config, factory = setup
    sent = []
    monkeypatch.setattr(notification, "channels", lambda db, u: {"email": email, "telegram": telegram})
    monkeypatch.setattr(personal, "external_config", lambda db, u, cfg: SimpleNamespace(owner=u.id))
    monkeypatch.setattr(mail, "saved_lists", lambda db, u: [{"trade_notifications": True}])
    monkeypatch.setattr(
        mail, "deliver", lambda db, u, cfg, item, title, text: sent.append(("email", u.id, cfg.owner, text))
    )
    monkeypatch.setattr(
        tg,
        "preferences",
        lambda db, u, cfg: {
            "enabled": True,
            "token_configured": True,
            "chat_id": "synthetic",
            "owner_matches": True,
        },
    )
    monkeypatch.setattr(
        tg,
        "telegram_request",
        lambda cfg, action, body: sent.append(("telegram", cfg.owner, cfg.owner, body["text"])),
    )
    with factory() as db:
        user = db.scalar(select(User))
        plan = TradePlan(
            owner_id=user.id,
            tenant_id=user.tenant_id,
            broker="namuh",
            code="034220",
            version=2,
            value={"name": "LG디스플레이", "account_last4": "1234", "trade_time": "10:30"},
        )
        db.add(plan)
        db.commit()
        notify_automatic_control(db, user, config, plan, event)
        assert len(sent) == int(email) + int(telegram)
        assert all(owner == user.id == configured for _, owner, configured, _ in sent)
        assert all("증권사: 나무증권\n종목: LG디스플레이 (034220)" in text for *_, text in sent)
        assert all("기존 미체결 주문은 자동 취소되지 않습니다." in text for *_, text in sent)
        events = db.scalars(
            select(AuditEvent).where(AuditEvent.action.like("trading.auto_notification_%"))
        ).all()
        assert len(events) == 2
        assert all(e.actor_id == user.id and e.tenant_id == user.tenant_id for e in events)
        stranger = SimpleNamespace(id="foreign", tenant_id=user.tenant_id)
        with pytest.raises(HTTPException):
            notify_automatic_control(db, stranger, config, plan, event)
