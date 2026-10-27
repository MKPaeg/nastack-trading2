"""Kiwoom authentication and allowlisted read-only industry data. No order API."""

import hashlib
import json
import re
import time
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from .db import aware, local_time, now
from .models import KiwoomConnection, User
from .security import audit

HOSTS = {"live": "https://api.kiwoom.com", "mock": "https://mockapi.kiwoom.com"}
READ_PATHS = {
    "ka10027": "/api/dostk/rkinfo",
    "kt00018": "/api/dostk/acnt",
    "kt00016": "/api/dostk/acnt",
    "ka10081": "/api/dostk/chart",
    "ka10101": "/api/dostk/stkinfo",
    "ka20006": "/api/dostk/chart",
    "usa10100": "/api/us/stkinfo",
    "usa06012": "/api/us/chart",
}


class KiwoomPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=0)
    enabled: bool
    environment: Literal["live", "mock"]


class KiwoomVerify(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)


class KiwoomReader:
    def __init__(self, config, transport=None):
        self.config, self.transport = config, transport

    def missing(self):
        return [
            label
            for label, value in (
                ("소유자 이메일", self.config.kiwoom_owner_email),
                ("App Key", self.config.kiwoom_app_key.get_secret_value()),
                ("App Secret", self.config.kiwoom_app_secret.get_secret_value()),
                ("토큰 암호화 키", self.config.broker_encryption_key.get_secret_value()),
            )
            if not value
        ]

    def owner(self, db, user):
        from .models import UserBrokerKey

        personal = db.scalar(
            select(UserBrokerKey).where(
                UserBrokerKey.owner_id == user.id,
                UserBrokerKey.tenant_id == user.tenant_id,
                UserBrokerKey.broker == "kiwoom",
            )
        )
        email = self.config.kiwoom_owner_email.strip().lower()
        count = db.scalar(select(func.count()).select_from(User).where(User.email == email))
        if user.email.lower() != email or (not personal and count != 1):
            raise HTTPException(403, "서버에 지정된 키움 API 키 소유자만 사용할 수 있습니다.")

    def cipher(self):
        try:
            return Fernet(self.config.broker_encryption_key.get_secret_value().encode())
        except (ValueError, TypeError):
            raise HTTPException(409, "서버의 토큰 암호화 키 설정을 확인하세요.") from None

    def scope(self, environment):
        config = self.config
        return hashlib.sha256(
            "\0".join(
                (
                    environment,
                    config.kiwoom_owner_email,
                    config.kiwoom_app_key.get_secret_value(),
                    config.kiwoom_app_secret.get_secret_value(),
                    config.broker_encryption_key.get_secret_value(),
                )
            ).encode()
        ).hexdigest()

    def post(self, environment, path, payload, headers=None):
        if environment not in HOSTS or path not in {"/oauth2/token", *READ_PATHS.values()}:
            raise HTTPException(403, "허용된 키움 읽기 전용 경로가 아닙니다.")
        try:
            with httpx.Client(
                timeout=20, trust_env=False, follow_redirects=False, transport=self.transport
            ) as client:
                with client.stream(
                    "POST",
                    HOSTS[environment] + path,
                    json=payload,
                    headers={"Content-Type": "application/json;charset=UTF-8", **(headers or {})},
                ) as response:
                    if response.status_code == 429:
                        raise HTTPException(429, "키움 호출 제한입니다. 자동 재시도하지 않습니다.")
                    if response.status_code != 200:
                        raise HTTPException(
                            502, "키움 서버가 요청을 거부했습니다. 환경·인증·등록 IP를 확인하세요."
                        )
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 2_000_000:
                            raise HTTPException(502, "키움 응답 크기 제한을 초과했습니다.")
                    data = json.loads(raw)
                    response_headers = dict(response.headers)
            if not isinstance(data, dict) or str(data.get("return_code")) != "0":
                if isinstance(data, dict) and "8050" in str(data.get("return_msg", "")):
                    raise HTTPException(
                        403,
                        "키움 조회 IP 미등록 · 키움 REST API 홈페이지의 API 사용신청에서 현재 서버 접속 IP를 등록하세요.",
                    )
                # Never return arbitrary provider messages containing credentials or account data.
                raise HTTPException(
                    502, "키움 응답이 정상 처리되지 않았습니다. 키·환경·등록 IP·조회 권한을 확인하세요."
                )
            return data, response_headers
        except HTTPException:
            raise
        except (httpx.HTTPError, ValueError, TypeError, UnicodeError):
            raise HTTPException(
                502, "키움 연결 또는 응답 검증에 실패했습니다. 기존 자료는 유지합니다."
            ) from None

    def token(self, db, user):
        from .user_brokers import personal_config

        scoped = personal_config(db, user, self.config, "kiwoom")
        if scoped is not self.config:
            reader = KiwoomReader(scoped, self.transport)
            return reader._token(db, user)
        return self._token(db, user)

    def _token(self, db, user):
        self.owner(db, user)
        connection = db.scalar(
            select(KiwoomConnection)
            .where(KiwoomConnection.owner_id == user.id, KiwoomConnection.tenant_id == user.tenant_id)
            .with_for_update()
        )
        if not connection or not connection.enabled:
            raise HTTPException(409, "관리자 설정의 영웅문 탭에서 키움 조회 연결을 켜세요.")
        if self.missing():
            raise HTTPException(409, "서버의 키움 키·소유자·암호화 설정이 누락되었습니다.")
        scope = self.scope(connection.environment)
        if (
            connection.token_ciphertext
            and connection.token_scope == scope
            and connection.token_expires_at
            and aware(connection.token_expires_at) > now() + timedelta(seconds=60)
        ):
            try:
                return self.cipher().decrypt(
                    connection.token_ciphertext.encode()
                ).decode(), connection.environment
            except (InvalidToken, UnicodeError):
                raise HTTPException(
                    409, "키움 인증 캐시를 읽을 수 없습니다. 연결 설정을 다시 저장하세요."
                ) from None
        if connection.last_attempt_at and aware(connection.last_attempt_at) > now() - timedelta(seconds=60):
            raise HTTPException(429, "키움 인증 재시도는 60초 간격으로 실행하세요.")
        # Hold the owner connection lock through token issuance so parallel workers reuse one token.
        connection.last_attempt_at = now()
        try:
            data, _ = self.post(
                connection.environment,
                "/oauth2/token",
                {
                    "grant_type": "client_credentials",
                    "appkey": self.config.kiwoom_app_key.get_secret_value(),
                    "secretkey": self.config.kiwoom_app_secret.get_secret_value(),
                },
            )
            token = data.get("token")
            expiry = datetime.strptime(str(data.get("expires_dt")), "%Y%m%d%H%M%S").replace(
                tzinfo=ZoneInfo("Asia/Seoul")
            )
            if (
                not isinstance(token, str)
                or not token
                or len(token) > 4096
                or expiry <= now() + timedelta(seconds=60)
            ):
                raise ValueError
            connection.token_ciphertext = self.cipher().encrypt(token.encode()).decode()
            connection.token_scope, connection.token_expires_at = scope, expiry
            connection.last_verified_at, connection.last_error = now(), None
            audit(db, user, "kiwoom.auth_verified", user.id, {"environment": connection.environment})
            db.commit()
            return token, connection.environment
        except (HTTPException, ValueError, TypeError):
            connection.last_error = "AUTH_FAILED"
            connection.token_ciphertext = None
            connection.token_expires_at = None
            audit(db, user, "kiwoom.auth_failed", user.id, {"environment": connection.environment})
            db.commit()
            raise HTTPException(
                502, "키움 인증 실패: 키·환경·등록 IP·만료 응답을 확인하세요. 60초 후 재시도할 수 있습니다."
            ) from None

    def read(self, token, environment, api_id, payload, continuation=None):
        if api_id not in READ_PATHS:
            raise HTTPException(403, "키움 주문·계좌 API는 연결하지 않았습니다.")
        headers = {"authorization": f"Bearer {token}", "api-id": api_id}
        if continuation:
            headers.update({"cont-yn": "Y", "next-key": continuation})
        return self.post(environment, READ_PATHS[api_id], payload, headers)

    def industry_master(self, token, environment, market):
        data, headers = self.read(token, environment, "ka10101", {"mrkt_tp": market})
        items = data.get("list")
        if not isinstance(items, list) or headers.get("cont-yn") == "Y":
            raise HTTPException(502, "키움 업종 코드 목록이 불완전합니다. 수집을 중지합니다.")
        return items


def fetch_index(db, user, config, dimension, start, end, transport=None):
    from .user_brokers import personal_config

    reader = KiwoomReader(personal_config(db, user, config, "kiwoom"), transport)
    token, environment = reader.token(db, user)
    connection = db.get(KiwoomConnection, user.id)
    version = connection.version
    master = reader.industry_master(token, environment, dimension["market_code"])
    matches = [
        item for item in master if isinstance(item, dict) and item.get("code") == dimension["series_code"]
    ]
    if len(matches) != 1 or matches[0].get("name") != dimension["instrument_name"]:
        raise HTTPException(
            409, "키움 업종 코드·명칭이 정의와 다릅니다. 기존 자료를 유지하고 매핑 검토가 필요합니다."
        )
    points, hashes, seen_keys, continuation, previous_oldest = {}, [], set(), None, None
    try:
        for page in range(10):
            data, headers = reader.read(
                token,
                environment,
                "ka20006",
                {
                    "inds_cd": dimension["series_code"],
                    "base_dt": end.strftime("%Y%m%d"),
                },
                continuation,
            )
            if data.get("inds_cd") != dimension["series_code"]:
                raise ValueError
            bars = data.get("inds_dt_pole_qry")
            if not isinstance(bars, list) or not bars or len(bars) > 2000:
                raise ValueError
            hashes.append(hashlib.sha256(json.dumps(bars, sort_keys=True).encode()).hexdigest())
            page_dates = []
            for bar in bars:
                if not isinstance(bar, dict):
                    raise ValueError
                raw_date, raw_price = str(bar.get("dt", "")), str(bar.get("cur_prc", "")).strip()
                if not re.fullmatch(r"\d{8}", raw_date) or not re.fullmatch(r"[+-]?\d{1,16}", raw_price):
                    raise ValueError
                day = datetime.strptime(raw_date, "%Y%m%d").date()
                value = abs(int(raw_price)) / 100
                if value <= 0 or day > end:
                    raise ValueError
                page_dates.append(day)
                if start <= day <= end:
                    key = day.isoformat()
                    if key in points:
                        raise ValueError
                    points[key] = {"date": key, "value": value}
            oldest = min(page_dates)
            if previous_oldest is not None and oldest >= previous_oldest:
                raise ValueError
            if oldest <= start or headers.get("cont-yn") != "Y":
                break
            continuation = headers.get("next-key")
            if not continuation or len(continuation) > 256 or continuation in seen_keys or page == 9:
                raise ValueError
            seen_keys.add(continuation)
            previous_oldest = oldest
            time.sleep(0.3)
        if not points:
            raise HTTPException(422, "선택 기간에 키움 지수 관측값이 없습니다.")
        current = db.scalar(
            select(KiwoomConnection)
            .where(KiwoomConnection.owner_id == user.id, KiwoomConnection.tenant_id == user.tenant_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if (
            not current
            or not current.enabled
            or current.version != version
            or current.environment != environment
            or current.token_scope != reader.scope(environment)
        ):
            raise HTTPException(409, "수집 도중 키움 연결 정책이 변경되어 결과를 저장하지 않습니다.")
        values = [points[key] for key in sorted(points)]
        return {
            "points": values,
            "sha256": hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest(),
            "sha256_basis": "validated_points_json",
            "page_sha256": hashes,
            "source_url": dimension["source_url"],
            "series_code": dimension["series_code"],
            "provider": "kiwoom_index",
            "definition": dimension,
            "environment": environment,
            "representation": dimension["representation"],
            "scale_divisor": 100,
            "retrieved_at": now().isoformat(),
            "publication_times_verified": False,
        }
    except (ValueError, TypeError, KeyError, OverflowError):
        raise HTTPException(
            502, "키움 일봉의 날짜·지수 배율·중복·연속조회를 검증하지 못했습니다. 기존 자료는 유지합니다."
        ) from None


def connection_view(reader, connection, user):
    return {
        "version": connection.version if connection else 0,
        "enabled": connection.enabled if connection else False,
        "environment": connection.environment if connection else "mock",
        "key_owner_matches": user.email.lower() == reader.config.kiwoom_owner_email.strip().lower(),
        "missing": reader.missing(),
        "read_only": True,
        "token_valid": bool(
            connection
            and connection.enabled
            and connection.token_ciphertext
            and connection.token_expires_at
            and connection.token_scope == reader.scope(connection.environment)
            and aware(connection.token_expires_at) > now() + timedelta(seconds=60)
        ),
        "token_expires_at": local_time(connection.token_expires_at, reader.config.timezone)
        if connection
        else None,
        "last_verified_at": local_time(connection.last_verified_at, reader.config.timezone)
        if connection
        else None,
        "last_error": connection.last_error if connection else None,
    }


def kiwoom_router(administrator, db_session, config, transport=None):
    router = APIRouter()

    def row(db, user):
        return db.scalar(
            select(KiwoomConnection)
            .where(KiwoomConnection.owner_id == user.id, KiwoomConnection.tenant_id == user.tenant_id)
            .with_for_update()
        )

    @router.get("/api/admin/brokers/kiwoom")
    @router.get("/api/user/brokers/kiwoom")
    def status(user=Depends(administrator), db=Depends(db_session)):
        from .user_brokers import personal_config

        reader = KiwoomReader(personal_config(db, user, config, "kiwoom"), transport)
        return connection_view(reader, row(db, user), user)

    @router.put("/api/admin/brokers/kiwoom")
    @router.put("/api/user/brokers/kiwoom")
    def save(payload: KiwoomPolicy, user=Depends(administrator), db=Depends(db_session)):
        from .user_brokers import personal_config

        reader = KiwoomReader(personal_config(db, user, config, "kiwoom"), transport)
        reader.owner(db, user)
        # Serialize first-time creation too; existing user lock has a stable identity.
        db.scalar(select(User).where(User.id == user.id).with_for_update())
        connection = row(db, user)
        if payload.version != (connection.version if connection else 0):
            raise HTTPException(409, "키움 설정이 변경되었습니다. 새로 조회 후 저장하세요.")
        if payload.enabled and reader.missing():
            raise HTTPException(409, "키움 키·소유자·암호화 키를 서버 .env에 설정하세요.")
        if not connection:
            connection = KiwoomConnection(owner_id=user.id, tenant_id=user.tenant_id, version=0)
            db.add(connection)
        connection.enabled, connection.environment = payload.enabled, payload.environment
        connection.version += 1
        connection.token_ciphertext = connection.token_scope = connection.token_expires_at = None
        connection.last_verified_at = connection.last_error = None
        audit(
            db,
            user,
            "kiwoom.policy_saved",
            user.id,
            {"enabled": payload.enabled, "environment": payload.environment},
        )
        db.commit()
        return connection_view(reader, connection, user)

    @router.post("/api/admin/brokers/kiwoom/verify")
    @router.post("/api/user/brokers/kiwoom/verify")
    def verify(payload: KiwoomVerify, user=Depends(administrator), db=Depends(db_session)):
        from .user_brokers import personal_config

        reader = KiwoomReader(personal_config(db, user, config, "kiwoom"), transport)
        reader.owner(db, user)
        connection = row(db, user)
        if not connection or connection.version != payload.version:
            raise HTTPException(409, "키움 설정을 저장하거나 변경된 버전을 다시 확인하세요.")
        reader.token(db, user)
        return connection_view(reader, row(db, user), user)

    return router
