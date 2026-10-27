"""Owner-scoped recipient lists, stored locally without an Agent dependency."""

import re
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from .mail import send_email_message
from .models import UserSettings
from .security import audit


class RecipientList(BaseModel):
    version: int = Field(ge=0)
    name: str = Field(min_length=1, max_length=80)
    emails: list[str] = Field(min_length=1, max_length=100)
    trade_notifications: bool = False

    @field_validator("name")
    @classmethod
    def name_valid(cls, value):
        if not value.strip():
            raise ValueError("목록 이름을 입력하세요.")
        return value.strip()

    @field_validator("emails")
    @classmethod
    def emails_valid(cls, values):
        emails = list(dict.fromkeys(v.strip().lower() for v in values))
        if any(len(v) > 254 or not re.fullmatch(r"[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+", v) for v in emails):
            raise ValueError("유효한 이메일 주소를 입력하세요.")
        return emails


class Message(BaseModel):
    subject: str = Field(min_length=1, max_length=200, pattern=r"^[^\r\n]+$")
    body: str = Field(min_length=1, max_length=20000)


def saved_lists(db, user):
    row = db.get(UserSettings, user.id)
    return (row.value if row and row.tenant_id == user.tenant_id else {}).get("notification_email_lists", [])


def deliver(db, user, config, item, subject, body):
    from .personal_connections import external_config

    config = external_config(db, user, config)
    if not config.smtp_host or not config.smtp_password.get_secret_value() or not config.smtp_from_email:
        raise HTTPException(409, "사용자 설정에서 본인의 SMTP 연결을 등록하고 사용을 켜세요.")
    audit(db, user, "email.send_started", item["id"], {"recipient_count": len(item["emails"])})
    db.commit()
    try:
        count = send_email_message(config, item["emails"], subject, body)
    except Exception:
        audit(db, user, "email.send_failed", item["id"], {})
        db.commit()
        raise HTTPException(
            502, "이메일 발송 실패 · 일부 수신자에게 전송됐을 수 있습니다. 자동 재시도하지 않습니다."
        ) from None
    audit(db, user, "email.send_succeeded", item["id"], {"recipient_count": count})
    db.commit()
    return {"status": "sent", "recipient_count": count}


def email_lists_router(administrator, db_session, config):
    router = APIRouter(prefix="/api/admin/email-lists")

    @router.get("")
    def listing(user=Depends(administrator), db=Depends(db_session)):
        from .personal_connections import external_config

        scoped = external_config(db, user, config)
        return {
            "items": saved_lists(db, user),
            "smtp_configured": bool(scoped.smtp_host and scoped.smtp_from_email),
        }

    @router.put("/{list_id}")
    def save(list_id: str, payload: RecipientList, user=Depends(administrator), db=Depends(db_session)):
        if user.role == "viewer":
            raise HTTPException(403, "조회 전용 사용자입니다.")
        row = db.scalar(
            select(UserSettings)
            .where(UserSettings.user_id == user.id, UserSettings.tenant_id == user.tenant_id)
            .with_for_update()
        )
        if not row:
            raise HTTPException(409, "사용자 설정이 없습니다.")
        items = saved_lists(db, user)
        current = next((v for v in items if v["id"] == list_id), None)
        if list_id != "new" and not current:
            raise HTTPException(404, "목록을 찾을 수 없습니다.")
        if payload.version != (current["version"] if current else 0):
            raise HTTPException(409, "목록이 변경되었습니다. 새로 조회하세요.")
        if not current and len(items) >= 20:
            raise HTTPException(422, "목록은 최대 20개까지 저장할 수 있습니다.")
        item = {
            **payload.model_dump(),
            "id": current["id"] if current else str(uuid4()),
            "version": payload.version + 1,
        }
        row.value = {
            **row.value,
            "notification_email_lists": [v for v in items if v["id"] != item["id"]] + [item],
        }
        audit(db, user, "email.list_saved", item["id"], {"recipient_count": len(item["emails"])})
        db.commit()
        return item

    @router.delete("/{list_id}")
    def remove(list_id: str, version: int, user=Depends(administrator), db=Depends(db_session)):
        if user.role == "viewer":
            raise HTTPException(403, "조회 전용 사용자입니다.")
        row = db.scalar(
            select(UserSettings)
            .where(UserSettings.user_id == user.id, UserSettings.tenant_id == user.tenant_id)
            .with_for_update()
        )
        items = saved_lists(db, user)
        item = next((v for v in items if v["id"] == list_id), None)
        if not item:
            raise HTTPException(404, "목록을 찾을 수 없습니다.")
        if item["version"] != version:
            raise HTTPException(409, "목록이 변경되었습니다.")
        row.value = {**row.value, "notification_email_lists": [v for v in items if v["id"] != list_id]}
        audit(db, user, "email.list_deleted", list_id, {})
        db.commit()
        return {"status": "deleted"}

    @router.post("/{list_id}/send")
    def send(list_id: str, payload: Message, user=Depends(administrator), db=Depends(db_session)):
        if user.role == "viewer":
            raise HTTPException(403, "조회 전용 사용자입니다.")
        item = next((v for v in saved_lists(db, user) if v["id"] == list_id), None)
        if not item:
            raise HTTPException(404, "목록을 찾을 수 없습니다.")
        return deliver(db, user, config, item, payload.subject, payload.body)

    return router
