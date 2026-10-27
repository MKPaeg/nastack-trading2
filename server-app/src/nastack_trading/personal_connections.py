"""Encrypted, owner-scoped external-service settings, with fixed provider endpoints."""

import json
import re
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import select

from .db import aware, now
from .models import UserSettings
from .security import audit
from .user_brokers import cipher

SMTP_HOSTS = {"smtp.gmail.com", "smtp.office365.com", "smtp.naver.com", "smtp.daum.net"}
SECRET_FIELDS = {"api_key", "bot_token", "password"}


def records(db, user):
    row = db.get(UserSettings, user.id)
    return (row.value if row and row.tenant_id == user.tenant_id else {}).get("personal_connections", {})


def values(db, user, config, service):
    record = records(db, user).get(service)
    if not record:
        return None
    try:
        return json.loads(cipher(config).decrypt(record["ciphertext"].encode()))
    except Exception:
        raise HTTPException(
            409, "개인 연결 설정을 복호화하지 못했습니다. 서버 암호화 키를 확인하세요."
        ) from None


def external_config(db, user, config):
    # Server SMTP is for authentication mail only, never personal notifications.
    updates = {
        "telegram_bot_token": SecretStr(""),
        "telegram_chat_id": "",
        "telegram_owner_email": user.email,
        "smtp_host": "",
        "smtp_username": "",
        "smtp_password": SecretStr(""),
        "smtp_from_email": "",
    }
    for service in records(db, user):
        value = values(db, user, config, service)
        enabled = value.get("enabled", False)
        if service == "openai":
            updates["openai_api_key"] = SecretStr(value.get("api_key", "") if enabled else "")
        elif service == "telegram":
            updates.update(
                telegram_bot_token=SecretStr(value.get("bot_token", "") if enabled else ""),
                telegram_chat_id=value.get("chat_id", "") if enabled else "",
                telegram_owner_email=user.email,
            )
        elif service == "smtp":
            updates.update(
                smtp_host=value.get("host", "") if enabled else "",
                smtp_port=value.get("port", 587),
                smtp_username=value.get("username", ""),
                smtp_password=SecretStr(value.get("password", "") if enabled else ""),
                smtp_from_email=value.get("from_email", ""),
                smtp_use_ssl=value.get("port") == 465,
                smtp_use_tls=value.get("port") != 465,
            )
    return config.model_copy(update=updates) if updates else config


class Connection(BaseModel):
    version: int = Field(ge=0)
    enabled: bool
    fields: dict[str, str | int] = Field(default_factory=dict, max_length=6)


def connections_router(current_user, db_session, config, transport=None):
    router = APIRouter(prefix="/api/user/connections")

    @router.post("/ecos/test")
    @router.post("/krx/test")
    def test_connection(request: Request, user=Depends(current_user), db=Depends(db_session)):
        from .dimension_catalog import BY_ID
        from .models import User
        from .official_daily import ecos_key, fetch_ecos

        service = "krx" if request.url.path.endswith("/krx/test") else "ecos"
        label = "KRX" if service == "krx" else "ECOS"
        from .models import DimensionExclusion
        prior_exclusion = db.get(DimensionExclusion, (user.id, "control-11")) if service == "krx" else None
        exclusion_at_start = prior_exclusion.created_at if prior_exclusion and prior_exclusion.tenant_id == user.tenant_id else None
        from .dimension_krx import credentials, fetch_vkospi, validate_owner
        get_key = ecos_key if service == "ecos" else lambda d, u, c: credentials(d, u, c)[0]
        owner_id, tenant_id = user.id, user.tenant_id
        if not user.active or user.role == "viewer":
            raise HTTPException(403, "조회 전용 사용자는 연결 테스트를 실행할 수 없습니다.")
        row = db.scalar(
            select(UserSettings)
            .where(UserSettings.user_id == user.id, UserSettings.tenant_id == user.tenant_id)
            .with_for_update()
        )
        if not row:
            raise HTTPException(409, "본인의 개인 연결을 먼저 저장하세요.")
        key = get_key(db, user, config)
        version = records(db, user)[service]["version"]
        previous = row.value.get("connection_checks", {}).get(service, {})
        if (
            previous.get("started_at")
            and aware(datetime.fromisoformat(previous["started_at"])) + timedelta(seconds=15) > now()
        ):
            raise HTTPException(
                429, f"{label} 연결 테스트는 15초 간격으로 실행하세요.", headers={"Retry-After": "15"}
            )
        started = now().isoformat()
        row.value = {
            **row.value,
            "connection_checks": {
                **row.value.get("connection_checks", {}),
                service: {"started_at": started, "status": "running"},
            },
        }
        audit(db, user, "connection.test_started", service, {"version": version})
        db.commit()
        result, failure = None, None
        try:
            today = now().astimezone(ZoneInfo("Asia/Seoul")).date()
            if service == "krx":
                data = fetch_vkospi(BY_ID["control-11"], today - timedelta(days=14), today, key, version, transport, latest=True,
                    validate=lambda: validate_owner(db, owner_id, tenant_id, config, key, version))
            else:
                data = fetch_ecos(BY_ID["control-01"], today - timedelta(days=14), today, key, transport)
            latest = max((p for p in data["points"] if p["value"] is not None), key=lambda p: p["date"])
            result = {
                "status": "connected",
                "latest_date": latest["date"],
                "latest_value": latest["value"],
                "count": len(data["points"]),
                "checked_at": now().isoformat(),
                "connection_version": version,
                "observations_saved": False,
                "llm_tokens": 0,
            }
        except HTTPException as error:
            failure = error
            result = {"status": "failed", "checked_at": now().isoformat(), "http_status": error.status_code}
        db.expire_all()
        current = db.get(User, owner_id, populate_existing=True)
        if not current or current.tenant_id != tenant_id or not current.active or current.role == "viewer":
            raise HTTPException(403, "사용자 권한이 변경되었습니다.")
        row = db.scalar(
            select(UserSettings)
            .where(UserSettings.user_id == current.id, UserSettings.tenant_id == current.tenant_id)
            .with_for_update()
        )
        if (
            not row
            or records(db, current).get(service, {}).get("version") != version
            or get_key(db, current, config) != key
        ):
            raise HTTPException(409, "개인 연결이 변경되었습니다. 저장된 키로 다시 테스트하세요.")
        row.value = {
            **row.value,
            "connection_checks": {
                **row.value.get("connection_checks", {}),
                service: {"started_at": started, **result},
            },
        }
        if service == "krx" and not failure and exclusion_at_start:
            exclusion = db.get(DimensionExclusion, (current.id, "control-11"), populate_existing=True)
            if exclusion and exclusion.tenant_id == tenant_id and exclusion.created_at == exclusion_at_start:
                audit(db, current, "dimension.restore", "control-11", {"previous_reason": exclusion.reason, "reason": "krx_connection_verified"})
                db.delete(exclusion)
        audit(
            db, current, "connection.test_completed", service, {"status": result["status"], "version": version}
        )
        db.commit()
        if failure:
            raise failure
        return result

    @router.get("/{service}")
    def read(
        service: Literal["openai", "telegram", "smtp", "ecos", "opendart", "krx"],
        user=Depends(current_user),
        db=Depends(db_session),
    ):
        record = records(db, user).get(service)
        value = values(db, user, config, service) or {}
        return {
            "version": record["version"] if record else 0,
            "configured": bool(record),
            "enabled": value.get("enabled", False),
            "fields": {k: v for k, v in value.items() if k not in SECRET_FIELDS and k != "enabled"},
            "secret_configured": any(bool(value.get(k)) for k in SECRET_FIELDS),
            "smtp_hosts": sorted(SMTP_HOSTS) if service == "smtp" else [],
            "checks": {
                kind: check for kind, check in (db.get(UserSettings, user.id).value.get("connection_checks", {}) if record else {}).items()
                if (kind == service or service == "opendart" and kind == "opendart_financial")
                and check.get("connection_version") == record["version"]
            },
        }

    @router.put("/{service}")
    def save(
        service: Literal["openai", "telegram", "smtp", "ecos", "opendart", "krx"],
        payload: Connection,
        user=Depends(current_user),
        db=Depends(db_session),
    ):
        if user.role == "viewer":
            raise HTTPException(403, "조회 전용 사용자입니다.")
        row = db.scalar(
            select(UserSettings)
            .where(UserSettings.user_id == user.id, UserSettings.tenant_id == user.tenant_id)
            .with_for_update()
        )
        if not row:
            raise HTTPException(409, "사용자 설정이 없습니다.")
        record = records(db, user).get(service)
        if payload.version != (record["version"] if record else 0):
            raise HTTPException(409, "연결 설정이 변경되었습니다. 새로 조회하세요.")
        allowed = {
            "krx": {"api_key"},
            "opendart": {"api_key"},
            "ecos": {"api_key"},
            "openai": {"api_key"},
            "telegram": {"bot_token", "chat_id"},
            "smtp": {"host", "port", "username", "password", "from_email"},
        }[service]
        if set(payload.fields) - allowed or any(
            len(str(v)) > 2000 or "\n" in str(v) or "\r" in str(v) for v in payload.fields.values()
        ):
            raise HTTPException(422, "허용된 연결 필드와 길이를 확인하세요.")
        value = values(db, user, config, service) or {}
        value.update({k: v for k, v in payload.fields.items() if k not in SECRET_FIELDS or v})
        value["enabled"] = payload.enabled
        if service == "krx" and value.get("api_key") and (
            not isinstance(value["api_key"], str)
            or not re.fullmatch(r"[!-~]{8,512}", value["api_key"])
        ):
            raise HTTPException(422, "KRX 인증키는 공백 없이 8~512자의 발급된 원문으로 입력하세요.")
        if payload.enabled:
            required = {
                "krx": ["api_key"],
                "opendart": ["api_key"],
                "ecos": ["api_key"],
                "openai": ["api_key"],
                "telegram": ["bot_token", "chat_id"],
                "smtp": ["host", "username", "password", "from_email"],
            }[service]
            if any(not value.get(k) for k in required):
                raise HTTPException(422, "필수 연결 정보를 입력하세요.")
            if service == "ecos" and not re.fullmatch(r"[A-Za-z0-9_-]{8,200}", str(value["api_key"])):
                raise HTTPException(
                    422, "ECOS 정식 인증키 형식을 확인하세요. sample 키는 운영 수집에 사용하지 않습니다."
                )
            if service == "opendart" and not re.fullmatch(r"[A-Fa-f0-9]{40}", str(value["api_key"])):
                raise HTTPException(422, "OpenDART에서 발급한 40자리 인증키를 입력하세요.")
            if service == "telegram" and not re.fullmatch(r"-?\d+", str(value["chat_id"])):
                raise HTTPException(422, "채팅 ID는 숫자로 입력하세요.")
            if service == "telegram" and not re.fullmatch(r"[A-Za-z0-9:_-]+", str(value["bot_token"])):
                raise HTTPException(422, "봇 토큰 형식을 확인하세요.")
            if service == "smtp":
                try:
                    value["port"] = int(value.get("port", 587))
                except (ValueError, TypeError):
                    raise HTTPException(422, "SMTP 포트를 확인하세요.") from None
                if value["host"] not in SMTP_HOSTS or value["port"] not in (465, 587):
                    raise HTTPException(422, "지원 SMTP 서버와 TLS 포트 465/587을 선택하세요.")
                if not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", str(value["from_email"])):
                    raise HTTPException(422, "발신 이메일을 확인하세요.")
        stored = {
            "version": payload.version + 1,
            "ciphertext": cipher(config).encrypt(json.dumps(value).encode()).decode(),
        }
        row.value = {**row.value, "personal_connections": {**records(db, user), service: stored}}
        audit(
            db, user, "connection.saved", service, {"version": stored["version"], "enabled": payload.enabled}
        )
        db.commit()
        return read(service, user, db)

    return router
