"""Trading-local Telegram configuration. No reads from nastack-agent at runtime."""

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from .models import UserSettings
from .security import audit


class TelegramPolicy(BaseModel):
    version: int = Field(ge=0)
    enabled: bool
    chat_id: str = Field(max_length=30, pattern=r"^-?\d*$")


def preferences(db, user, config):
    from .personal_connections import external_config, records

    personal = bool(records(db, user).get("telegram"))
    config = external_config(db, user, config)
    row = db.get(UserSettings, user.id)
    value = (row.value if row and row.tenant_id == user.tenant_id else {}).get("telegram_trading", {})
    return {
        "version": value.get("version", 0),
        "enabled": personal
        and bool(config.telegram_bot_token.get_secret_value())
        and value.get("enabled", True),
        "personal": personal,
        "chat_id": config.telegram_chat_id if personal else "",
        "chat_id_source": "personal" if personal else "not_configured",
        "token_configured": bool(config.telegram_bot_token.get_secret_value()),
        "owner_matches": config.telegram_owner_email.lower() == user.email.lower(),
    }


def telegram_request(config, method, body):
    try:
        with httpx.Client(timeout=8, trust_env=False, follow_redirects=False) as client:
            response = client.post(
                f"https://api.telegram.org/bot{config.telegram_bot_token.get_secret_value()}/{method}",
                json=body,
            )
        if response.status_code != 200 or len(response.content) > 100000:
            raise ValueError
        data = response.json()
        if data.get("ok") is not True:
            raise ValueError
        return data["result"]
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        raise HTTPException(502, "텔레그램 연결 실패 · 토큰·수신 채팅·봇 권한을 확인하세요.") from None


def telegram_router(administrator, db_session, config):
    base_config = config
    router = APIRouter()

    def owner(user, db):
        from .personal_connections import external_config

        scoped = external_config(db, user, config)
        if user.role == "viewer" or user.email.lower() != scoped.telegram_owner_email.lower():
            raise HTTPException(403, "본인의 텔레그램 연결만 설정할 수 있습니다.")

    @router.get("/api/admin/trading/notifications")
    def status(user=Depends(administrator), db=Depends(db_session)):
        value = preferences(db, user, config)
        if not value["owner_matches"]:
            value["chat_id"] = ""
        return {
            **value,
            "live_orders_enabled": config.live_orders_enabled,
            "automatic_orders_enabled": False,
            "token_source": "개인 DB 암호화 연결" if value["personal"] else "개인 연결 미등록",
        }

    @router.put("/api/admin/trading/notifications")
    def save(payload: TelegramPolicy, user=Depends(administrator), db=Depends(db_session)):
        owner(user, db)
        row = db.scalar(
            select(UserSettings)
            .where(UserSettings.user_id == user.id, UserSettings.tenant_id == user.tenant_id)
            .with_for_update()
        )
        if not row:
            raise HTTPException(409, "사용자 설정을 먼저 생성하세요.")
        current = preferences(db, user, config)
        if current["version"] != payload.version:
            raise HTTPException(409, "알림 설정이 변경되었습니다. 새로 조회하세요.")
        if payload.enabled and (not payload.chat_id or not current["token_configured"]):
            raise HTTPException(422, "알림 사용에는 봇 토큰과 채팅 ID가 필요합니다.")
        row.value = {
            **row.value,
            "telegram_trading": {
                "version": payload.version + 1,
                "enabled": payload.enabled,
                "chat_id": payload.chat_id,
            },
        }
        audit(db, user, "trading.telegram_saved", user.id, {"enabled": payload.enabled})
        db.commit()
        return status(user, db)

    @router.post("/api/admin/trading/notifications/verify")
    def verify(user=Depends(administrator), db=Depends(db_session)):
        from .personal_connections import external_config

        config = external_config(db, user, base_config)
        owner(user, db)
        if not config.telegram_bot_token.get_secret_value():
            raise HTTPException(409, "봇 토큰이 설정되지 않았습니다.")
        result = telegram_request(config, "getMe", {})
        return {"status": "verified", "bot_username": result.get("username"), "message_sent": False}

    @router.post("/api/admin/trading/notifications/test")
    def test(user=Depends(administrator), db=Depends(db_session)):
        from .personal_connections import external_config

        config = external_config(db, user, base_config)
        owner(user, db)
        value = preferences(db, user, config)
        if not value["enabled"] or not value["chat_id"] or not value["token_configured"]:
            raise HTTPException(409, "알림 설정을 저장하고 사용을 켜세요.")
        telegram_request(
            config,
            "sendMessage",
            {
                "chat_id": value["chat_id"],
                "text": "[NaStack Trading] 매매 알림 연결 테스트입니다. 실제 주문은 실행하지 않았습니다.",
            },
        )
        audit(db, user, "trading.telegram_test_sent", user.id, {})
        db.commit()
        return {"status": "sent"}

    return router
