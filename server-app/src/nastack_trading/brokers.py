"""Namuh authentication only: fixed official endpoint, no account/quote/order calls."""

import hashlib
import json
import logging
import time
from datetime import timedelta
from typing import Literal

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from .db import aware, local_time, now
from .models import BrokerConnection, BrokerProbe, TradePlan, User
from .security import audit

AUTH_URL = "https://api.nhplug.com:8443/oauth2/token"
API_URLS = {"mock": "https://moapi.nhplug.com:8443", "live": "https://api.nhplug.com:8443"}
ERRORS = {
    "NOT_CONFIGURED": "서버의 소유자 이메일·앱키·암호화 키 설정을 확인하세요.",
    "OWNER_MISMATCH": "이 사용자는 서버에 지정된 나무 API 키 소유자가 아닙니다.",
    "AUTH_TIMEOUT": "나무 인증 응답 시간이 초과되었습니다. 잠시 후 다시 확인하세요.",
    "AUTH_NETWORK": "나무 인증 서버에 연결하지 못했습니다.",
    "AUTH_REJECTED": "나무 인증이 거부되었습니다. 앱키·사용 신청 상태를 확인하세요.",
    "RATE_LIMITED": "나무 호출 제한에 도달했습니다. 자동 재시도하지 않습니다.",
    "INVALID_RESPONSE": "나무 인증 응답 형식을 확인하지 못했습니다.",
    "CACHE_INVALID": "토큰 암호화 키가 변경되었거나 캐시가 손상되었습니다. 설정 저장 후 다시 확인하세요.",
    "POLICY_CHANGED": "인증 중 설정이 변경되어 결과를 적용하지 않았습니다.",
}


class HideAuthURL(logging.Filter):
    def filter(self, record):
        # httpx INFO logs URLs, including this provider's credential query parameters.
        return "api.nhplug.com" not in record.getMessage()


logging.getLogger("httpx").addFilter(HideAuthURL())


class BrokerFailure(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(ERRORS[code])


class BrokerPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=0)
    enabled: bool
    environment: Literal["mock", "live"] = "mock"


class ProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)


class NamuhAuth:
    def __init__(self, config, transport=None):
        self.config, self.transport = config, transport

    def missing(self):
        fields = {
            "TRADING_NAMUH_OWNER_EMAIL": self.config.namuh_owner_email,
            "TRADING_NAMUH_APP_KEY": self.config.namuh_app_key.get_secret_value(),
            "TRADING_NAMUH_APP_SECRET": self.config.namuh_app_secret.get_secret_value(),
            "TRADING_BROKER_ENCRYPTION_KEY": self.config.broker_encryption_key.get_secret_value(),
        }
        result = [name for name, value in fields.items() if not value]
        if not result:
            try:
                self.cipher()
            except BrokerFailure:
                result.append("TRADING_BROKER_ENCRYPTION_KEY (잘못된 형식)")
        return result

    def cipher(self):
        try:
            return Fernet(self.config.broker_encryption_key.get_secret_value().encode())
        except (ValueError, TypeError):
            raise BrokerFailure("NOT_CONFIGURED") from None

    def scope(self):
        values = [
            AUTH_URL,
            self.config.namuh_app_key.get_secret_value(),
            self.config.namuh_app_secret.get_secret_value(),
            self.config.broker_encryption_key.get_secret_value(),
        ]
        return hashlib.sha256("\0".join(values).encode()).hexdigest()

    def verify(self, connection, email, *, force=False):
        if self.missing():
            raise BrokerFailure("NOT_CONFIGURED")
        if email.lower() != self.config.namuh_owner_email.strip().lower():
            raise BrokerFailure("OWNER_MISMATCH")
        cipher, scope = self.cipher(), self.scope()
        if (
            not force
            and
            connection.token_scope == scope
            and connection.token_ciphertext
            and connection.token_expires_at
            and aware(connection.token_expires_at) > now() + timedelta(seconds=60)
        ):
            try:
                cipher.decrypt(connection.token_ciphertext.encode())
            except InvalidToken:
                raise BrokerFailure("CACHE_INVALID") from None
            return connection.token_ciphertext, connection.token_expires_at, scope, True
        # No retries, redirects, arbitrary endpoints, proxy environment, SDK plaintext file cache,
        # or exception/response-body logging (provider URLs contain secrets).
        try:
            started = time.monotonic()
            with httpx.Client(
                transport=self.transport, timeout=10, follow_redirects=False, trust_env=False
            ) as client:
                with client.stream(
                    "POST",
                    AUTH_URL,
                    params={
                        "appkey": self.config.namuh_app_key.get_secret_value(),
                        "appsecretkey": self.config.namuh_app_secret.get_secret_value(),
                        "grant_type": "client_credentials",
                        "scope": "oob",
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                ) as response:
                    if response.status_code == 429:
                        raise BrokerFailure("RATE_LIMITED")
                    if response.status_code != 200:
                        raise BrokerFailure("AUTH_REJECTED")
                    size, parts = 0, []
                    for part in response.iter_bytes():
                        if time.monotonic() - started > 20:
                            raise BrokerFailure("AUTH_TIMEOUT")
                        size += len(part)
                        if size > 65536:
                            raise BrokerFailure("INVALID_RESPONSE")
                        parts.append(part)
                    data = json.loads(b"".join(parts))
        except httpx.TimeoutException:
            raise BrokerFailure("AUTH_TIMEOUT") from None
        except httpx.RequestError:
            raise BrokerFailure("AUTH_NETWORK") from None
        except (ValueError, UnicodeError):
            raise BrokerFailure("INVALID_RESPONSE") from None
        if not isinstance(data, dict):
            raise BrokerFailure("INVALID_RESPONSE")
        token, ttl = data.get("access_token"), data.get("expires_in")
        if (
            not isinstance(token, str)
            or not 1 <= len(token) <= 4096
            or not token.isascii()
            or any(not 33 <= ord(char) <= 126 for char in token)
            or isinstance(ttl, bool)
            or len(str(ttl)) > 6
            or not str(ttl).isdigit()
            or not 61 <= int(ttl) <= 86400
            or data.get("error_code")
        ):
            raise BrokerFailure("INVALID_RESPONSE")
        return cipher.encrypt(token.encode()).decode(), now() + timedelta(seconds=int(ttl)), scope, False


def refresh_automatic_namuh(db, user, config, transport=None):
    """Renew only an approved user's active live connection, without placing orders."""
    from .user_brokers import personal_config

    if not user.active or user.role == "viewer":
        return False
    plans = db.scalars(
        select(TradePlan).where(
            TradePlan.owner_id == user.id,
            TradePlan.tenant_id == user.tenant_id,
            TradePlan.broker == "namuh",
        )
    )
    if not any(p.value.get("active") and p.value.get("mode") == "automatic" and not p.value.get("archived") for p in plans):
        return False
    row = db.scalar(select(BrokerConnection).where(
        BrokerConnection.owner_id == user.id, BrokerConnection.tenant_id == user.tenant_id
    ))
    if not row or not row.enabled or row.environment != "live":
        return False
    provider = NamuhAuth(personal_config(db, user, config, "namuh"), transport)
    if provider.missing() or user.email.lower() != provider.config.namuh_owner_email.lower():
        return False
    if (row.token_expires_at and aware(row.token_expires_at) > now() + timedelta(minutes=10)
            and row.token_scope == provider.scope()):
        return False
    claimed = db.execute(
        update(BrokerConnection).where(
            BrokerConnection.owner_id == user.id,
            BrokerConnection.tenant_id == user.tenant_id,
            BrokerConnection.enabled.is_(True),
            BrokerConnection.environment == "live",
            BrokerConnection.version == row.version,
            BrokerConnection.next_probe_at <= now(),
        ).values(next_probe_at=now() + timedelta(minutes=5)).execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        db.rollback()
        return False
    version, scope = row.version, provider.scope()
    db.commit()  # Release the claim transaction before provider I/O.
    try:
        result = provider.verify(row, user.email, force=True)
        failure = None
    except BrokerFailure as error:
        result, failure = None, error.code
    db.expire_all()
    current = db.scalar(
        select(BrokerConnection).where(
            BrokerConnection.owner_id == user.id, BrokerConnection.tenant_id == user.tenant_id
        ).with_for_update().execution_options(populate_existing=True)
    )
    owner = db.scalar(select(User).where(
        User.id == user.id, User.tenant_id == user.tenant_id
    ).execution_options(populate_existing=True))
    current_plans = db.scalars(select(TradePlan).where(
        TradePlan.owner_id == user.id, TradePlan.tenant_id == user.tenant_id,
        TradePlan.broker == "namuh",
    ))
    still_active = any(p.value.get("active") and p.value.get("mode") == "automatic"
                       and not p.value.get("archived") for p in current_plans)
    if (not current or not owner or not owner.active or owner.role == "viewer"
            or not still_active or not current.enabled or current.environment != "live"
            or current.version != version
            or NamuhAuth(personal_config(db, owner, config, "namuh")).scope() != scope):
        db.rollback()
        return False
    if not failure:
        current.token_ciphertext, current.token_expires_at, current.token_scope, reused = result
    else:
        reused = False
    probe = BrokerProbe(
        owner_id=user.id, tenant_id=user.tenant_id, policy_version=version,
        environment="live", status="failed" if failure else "completed",
        error_code=failure, reused_token=reused, finished_at=now(),
    )
    db.add(probe)
    db.flush()
    audit(db, owner, "broker.auth_auto_refreshed", probe.id,
          {"status": probe.status, "error_code": failure})
    db.commit()
    return failure is None


def brokers_router(administrator, db_session, config, transport=None):
    router = APIRouter()

    def connection(db, user):
        return db.scalar(
            select(BrokerConnection).where(
                BrokerConnection.owner_id == user.id, BrokerConnection.tenant_id == user.tenant_id
            )
        )

    def view(db, user):
        from .user_brokers import personal_config

        provider = NamuhAuth(personal_config(db, user, config, "namuh"), transport)
        item = connection(db, user)
        last = db.scalar(
            select(BrokerProbe)
            .where(BrokerProbe.owner_id == user.id, BrokerProbe.tenant_id == user.tenant_id)
            .order_by(BrokerProbe.created_at.desc(), BrokerProbe.id.desc())
            .limit(1)
        )
        return {
            "provider": "namuh",
            "enabled": item.enabled if item else False,
            "environment": item.environment if item else "mock",
            "version": item.version if item else 0,
            "missing_settings": provider.missing(),
            "key_owner_matches": owner_matches(db, user),
            "auth_url": AUTH_URL,
            "api_url": API_URLS[item.environment if item else "mock"],
            "orders_supported": False,
            "token_expires_at": local_time(item.token_expires_at, config.timezone)
            if item and item.token_expires_at and item.token_scope == provider.scope()
            else None,
            "last_probe": {
                "id": last.id,
                "status": last.status,
                "environment": last.environment,
                "policy_version": last.policy_version,
                "reused_token": last.reused_token,
                "message": ERRORS.get(last.error_code, "인증 토큰 확인 완료 · 계좌/시세 연결은 미검증")
                if last.status != "running"
                else "인증 확인 중 · 오래 지속되면 프로세스 중단 가능성을 확인하세요.",
                "created_at": local_time(last.created_at, config.timezone),
            }
            if last
            else None,
        }

    def owner_matches(db, user):
        from .user_brokers import personal_config

        scoped = personal_config(db, user, config, "namuh")
        email = scoped.namuh_owner_email.strip().lower()
        # Email alone cannot authorize sharing a server credential across tenants.
        count = db.scalar(select(func.count()).select_from(User).where(User.email == email))
        return user.email.lower() == email and (scoped is not config or count == 1)

    @router.get("/api/admin/brokers/namuh")
    @router.get("/api/user/brokers/namuh")
    def read(user=Depends(administrator), db=Depends(db_session)):
        return view(db, user)

    @router.put("/api/admin/brokers/namuh")
    @router.put("/api/user/brokers/namuh")
    def save(payload: BrokerPolicy, user=Depends(administrator), db=Depends(db_session)):
        values = {"enabled": payload.enabled, "environment": payload.environment}
        try:
            if payload.version == 0:
                db.add(BrokerConnection(owner_id=user.id, tenant_id=user.tenant_id, **values))
                db.flush()
            else:
                changed = db.execute(
                    update(BrokerConnection)
                    .where(
                        BrokerConnection.owner_id == user.id,
                        BrokerConnection.tenant_id == user.tenant_id,
                        BrokerConnection.version == payload.version,
                    )
                    .values(
                        **values,
                        version=payload.version + 1,
                        updated_at=now(),
                        token_ciphertext=None,
                        token_scope=None,
                        token_expires_at=None,
                    )
                )
                if changed.rowcount != 1:
                    raise HTTPException(409, "설정이 변경되었습니다. 화면을 다시 불러오세요.")
            audit(db, user, "broker.policy_updated", user.id, {**values, "version": payload.version + 1})
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "설정이 변경되었습니다. 화면을 다시 불러오세요.") from None
        return view(db, user)

    @router.post("/api/admin/brokers/namuh/verify")
    @router.post("/api/user/brokers/namuh/verify")
    def verify(payload: ProbeRequest, user=Depends(administrator), db=Depends(db_session)):
        from .user_brokers import personal_config

        provider = NamuhAuth(personal_config(db, user, config, "namuh"), transport)
        item = connection(db, user)
        if not item or not item.enabled or item.version != payload.version:
            raise HTTPException(409, "현재 설정을 저장하고 인증 확인을 활성화하세요.")
        changed = db.execute(
            update(BrokerConnection)
            .where(
                BrokerConnection.owner_id == user.id,
                BrokerConnection.version == payload.version,
                BrokerConnection.enabled.is_(True),
                BrokerConnection.next_probe_at <= now(),
            )
            .values(next_probe_at=now() + timedelta(seconds=60))
            .execution_options(synchronize_session=False)
        )
        if changed.rowcount != 1:
            raise HTTPException(429, "중복 요청을 막기 위해 60초 후 다시 확인하세요.")
        probe = BrokerProbe(
            owner_id=user.id,
            tenant_id=user.tenant_id,
            policy_version=item.version,
            environment=item.environment,
        )
        db.add(probe)
        owns_key = owner_matches(db, user)
        db.commit()  # release DB locks before network I/O
        try:
            if not owns_key:
                raise BrokerFailure("OWNER_MISMATCH")
            result = provider.verify(item, user.email)
            error = None
        except BrokerFailure as failure:
            result, error = None, failure.code
        # Serialize finalization with policy changes; do not apply stale credentials/results.
        current = db.scalar(
            select(BrokerConnection)
            .where(BrokerConnection.owner_id == user.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if not current.enabled or current.version != payload.version:
            error = "POLICY_CHANGED"
        if not error:
            current.token_ciphertext, current.token_expires_at, current.token_scope, reused = result
            probe.reused_token = reused
        probe.status, probe.error_code, probe.finished_at = "failed" if error else "completed", error, now()
        audit(db, user, "broker.auth_checked", probe.id, {"status": probe.status, "error_code": error})
        db.commit()
        return view(db, user)

    return router
