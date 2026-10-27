"""Explicit owner-scoped OpenDART authentication/financial availability checks.

No scheduled calls, raw financial persistence, LLM, orders, shared keys or fallback.
"""
import io
import json
import logging
import re
import zipfile
from datetime import datetime, timedelta
from typing import Literal
from xml.etree import ElementTree

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from .db import aware, now
from .models import User, UserSettings
from .personal_connections import records, values
from .security import audit

ROOT = "https://opendart.fss.or.kr/api/"


class _RedactKey(logging.Filter):
    def filter(self, record):
        record.msg = re.sub(r"(?i)(crtfc_key=)[^&\s'\"]+", r"\1[REDACTED]", record.getMessage())
        record.args = ()
        return True


for _name in ("httpx", "httpcore.http11", "httpcore.http2", "httpcore.connection", "httpcore.proxy"):
    logging.getLogger(_name).addFilter(_RedactKey())


class FinancialCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(pattern=r"^\d{6}$")
    year: int = Field(ge=2015, le=2100)
    report: Literal["11013", "11012", "11014", "11011"] = "11011"
    fs_div: Literal["CFS", "OFS"] = "CFS"


def key_for(db, user, config):
    value = values(db, user, config, "opendart") or {}
    key = value.get("api_key", "")
    if not value.get("enabled") or not re.fullmatch(r"[A-Fa-f0-9]{40}", str(key)):
        raise HTTPException(409, "본인의 OpenDART 정식 인증키를 저장·활성화하세요. 공용 키로 우회하지 않습니다.")
    return key


def get(path, key, params=None, transport=None):
    if path not in {"list.json", "corpCode.xml", "fnlttSinglAcntAll.json"}:
        raise HTTPException(403, "허용되지 않은 OpenDART 경로입니다.")
    limit = 8_000_000 if path == "corpCode.xml" else 2_000_000
    try:
        with httpx.Client(timeout=20, follow_redirects=False, trust_env=False, transport=transport) as client:
            with client.stream("GET", ROOT + path, params={**(params or {}), "crtfc_key": key}) as response:
                if response.status_code != 200:
                    raise HTTPException(502, "OpenDART HTTP 연결 확인 실패 · 기존 자료는 유지합니다.")
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > limit:
                        raise HTTPException(502, "OpenDART 응답 크기 제한 초과")
                return bytes(raw)
    except httpx.HTTPError:
        raise HTTPException(502, "OpenDART 연결 지연·실패 · 자동 재시도하지 않습니다.") from None


def checked_json(raw):
    try:
        data = json.loads(raw)
        status = data.get("status")
        if status not in {"000", "013"}:
            reason = {"010": "미등록 인증키", "011": "사용 중지 인증키", "012": "접근 IP 제한",
                      "020": "요청 한도 초과", "100": "요청 형식 확인 필요",
                      "800": "시스템 점검", "900": "제공자 오류", "901": "서비스 종료"}.get(status, "제공자 응답 거부")
            raise HTTPException(502, "OpenDART " + reason)
        return data
    except (ValueError, TypeError, AttributeError, UnicodeError):
        raise HTTPException(502, "OpenDART 응답 형식 검증 실패") from None


def company(raw, code):
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            members = archive.infolist()
            if len(members) != 1 or members[0].filename.upper() != "CORPCODE.XML" or members[0].file_size > 50_000_000:
                raise ValueError
            xml = archive.read(members[0])
        if b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
            raise ValueError
        root = ElementTree.fromstring(xml)
        if root.tag != "result":
            raise ValueError
        matches = [row for row in root.findall("list") if row.findtext("stock_code", "").strip() == code]
        if not matches:
            return None
        if len(matches) != 1 or not re.fullmatch(r"\d{8}", matches[0].findtext("corp_code", "")):
            raise ValueError
        return matches[0].findtext("corp_code"), matches[0].findtext("corp_name", "")[:200]
    except (zipfile.BadZipFile, ValueError, KeyError, RuntimeError, ElementTree.ParseError, UnicodeError):
        raise HTTPException(502, "OpenDART 기업 고유번호 파일 검증 실패") from None


def check_financial(key, query, transport=None):
    raw = get("corpCode.xml", key, transport=transport)
    if not raw.startswith(b"PK"):
        checked_json(raw)  # Some provider failures are JSON instead of ZIP.
        raise HTTPException(502, "OpenDART 기업 고유번호 ZIP 응답이 아닙니다.")
    match = company(raw, query.code)
    base = {"code": query.code, "year": query.year, "report": query.report, "fs_div": query.fs_div,
            "financial_data_saved": False, "llm_tokens": 0}
    if not match:
        return {**base, "status": "unmapped", "message": "종목코드의 공시대상 회사가 없습니다. ETF 등은 회사 재무제표 대상이 아닐 수 있습니다.", "request_count": 1}
    corp, name = match
    data = checked_json(get("fnlttSinglAcntAll.json", key,
                           {"corp_code": corp, "bsns_year": query.year, "reprt_code": query.report, "fs_div": query.fs_div}, transport))
    result = {**base, "company_name": name, "request_count": 2}
    if data["status"] == "013":
        return {**result, "status": "unavailable", "message": "선택한 연도·보고서·연결/별도 재무자료가 없습니다. 미공시·대상 제외일 수 있으며 연결 오류와 다릅니다."}
    rows = data.get("list")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 10000:
        raise HTTPException(502, "OpenDART 재무 응답에 유효한 계정 목록이 없습니다.")
    for row in rows:
        if (not isinstance(row, dict) or row.get("corp_code") != corp or row.get("bsns_year") != str(query.year)
                or row.get("reprt_code") != query.report or not isinstance(row.get("account_nm"), str)):
            raise HTTPException(502, "OpenDART 재무자료의 기업·연도·보고서 검증 실패")
    numeric_accounts = sum(bool(re.fullmatch(r"-?\d+", str(r.get("thstrm_amount", "")).replace(",", ""))) for r in rows)
    if not numeric_accounts:
        return {**result, "status": "empty_values", "account_count": len(rows),
                "message": "계정 목록은 있지만 당기 금액을 확인할 수 없습니다. 수집 성공이나 0원으로 취급하지 않습니다."}
    return {**result, "status": "available", "account_count": len(rows),
            "amount_account_count": numeric_accounts,
            "statements": sorted({str(r.get("sj_nm", ""))[:100] for r in rows}),
            "message": "재무 계정 응답 확인. 시계열 저장·그래프·재무 해석은 아직 실행하지 않았습니다."}


def opendart_router(current_user, db_session, config, transport=None):
    router = APIRouter(prefix="/api/user/connections/opendart")

    def run_check(db, user, kind, work):
        owner, tenant = user.id, user.tenant_id
        if not user.active or user.role == "viewer":
            raise HTTPException(403, "조회 전용 사용자는 외부 연결·자료 확인을 실행할 수 없습니다.")
        row = db.scalar(select(UserSettings).where(UserSettings.user_id == owner, UserSettings.tenant_id == tenant).with_for_update())
        key = key_for(db, user, config)
        version = records(db, user)["opendart"]["version"]
        previous = row.value.get("connection_checks", {}).get(kind, {})
        if previous.get("started_at") and aware(datetime.fromisoformat(previous["started_at"])) + timedelta(seconds=15) > now():
            raise HTTPException(429, "같은 확인 작업은 15초 간격으로 실행하세요.")
        started = now().isoformat()
        row.value = {**row.value, "connection_checks": {**row.value.get("connection_checks", {}), kind: {"started_at": started, "status": "running"}}}
        audit(db, user, "connection.check_started", kind, {"version": version})
        db.commit()
        failure = None
        try:
            result = work(key)
        except HTTPException as error:
            failure, result = error, {"status": "failed", "http_status": error.status_code}
        db.expire_all()
        current = db.get(User, owner, populate_existing=True)
        if not current or current.tenant_id != tenant or not current.active or current.role == "viewer":
            raise HTTPException(403, "확인 도중 사용자 권한이 변경되었습니다.")
        row = db.scalar(select(UserSettings).where(UserSettings.user_id == owner, UserSettings.tenant_id == tenant).execution_options(populate_existing=True).with_for_update())
        if records(db, current).get("opendart", {}).get("version") != version or key_for(db, current, config) != key:
            raise HTTPException(409, "개인 연결이 변경되어 이전 확인 결과를 적용하지 않습니다.")
        result = {**result, "checked_at": now().isoformat(), "connection_version": version}
        row.value = {**row.value, "connection_checks": {**row.value.get("connection_checks", {}), kind: {"started_at": started, **result}}}
        audit(db, current, "connection.check_completed", kind, {"version": version, "status": result["status"]})
        db.commit()
        if failure:
            raise failure
        return result

    @router.post("/test")
    def test(user=Depends(current_user), db=Depends(db_session)):
        def work(key):
            today = now().date().strftime("%Y%m%d")
            data = checked_json(get("list.json", key, {"bgn_de": today, "end_de": today, "page_count": 1}, transport))
            return {"status": "connected", "provider_status": data["status"], "request_count": 1,
                    "financial_data_saved": False, "llm_tokens": 0}
        return run_check(db, user, "opendart", work)

    @router.post("/financial-check")
    def financial(query: FinancialCheck, user=Depends(current_user), db=Depends(db_session)):
        if query.year > now().year:
            raise HTTPException(422, "미래 사업연도는 확인할 수 없습니다.")
        return run_check(db, user, "opendart_financial", lambda key: check_financial(key, query, transport))

    return router
