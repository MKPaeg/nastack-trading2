"""Explicit, read-only NH PLUG quote/chart/holding retrieval and DB snapshots."""

import json
import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import holidays
import httpx
from cryptography.fernet import InvalidToken
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from .brokers import API_URLS, NamuhAuth
from .db import aware, local_time, now
from .models import BrokerConnection, BrokerSnapshot, Instrument, User
from .security import audit


def numeric(value):
    if value is None or value == "":
        return None
    try:
        number = Decimal(str(value).replace(",", ""))
        return float(number) if number.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def holding_quantity(item):
    """Use the broker's remaining position, including today's executions.

    Settled/integrated and unsettled activity are not the current position.
    Observed after a confirmed 5-share buy: integrated=0, unsettled=6,
    residual=5. Never add those fields or infer holdings from app fills.
    Missing residual is unknown, not zero. Sell availability is checked separately.
    """
    balance = numeric(item.get("rsdl_qty"))
    if balance is None or balance < 0 or not balance.is_integer():
        return None
    return balance


def market_change(amount, rate, sign, price=None, previous_close=None):
    """Prefer the actual price difference when the provider omits or misstates a sign."""
    value, percent = numeric(amount), numeric(rate)
    current, reference_close = numeric(price), numeric(previous_close)
    if current is not None and reference_close is not None and reference_close > 0:
        difference = current - reference_close
        direction = "up" if difference > 0 else "down" if difference < 0 else "flat"
        return difference, round(difference / reference_close * 100, 2), direction
    if sign in {"1", "2", "6", "7"}:
        direction = "up"
    elif sign in {"4", "5", "8", "9"}:
        direction = "down"
    elif sign in {"0", "3"}:
        direction = "flat"
    elif (value is not None and value < 0) or (percent is not None and percent < 0):
        reference = percent if percent not in (None, 0) else value
        direction = (
            "up"
            if reference is not None and reference > 0
            else "down"
            if reference is not None and reference < 0
            else "flat"
        )
    elif value == 0 and percent in (None, 0):
        direction = "flat"
    else:
        return None, None, "unknown"
    if direction == "up":
        return (
            abs(value) if value is not None else None,
            abs(percent) if percent is not None else None,
            direction,
        )
    if direction == "down":
        return (
            -abs(value) if value is not None else None,
            -abs(percent) if percent is not None else None,
            direction,
        )
    return 0 if value is not None else None, 0 if percent is not None else None, direction


def daily_date(value):
    """The provider documents YY/MM/DD; accept its common YYYYMMDD variant too."""
    raw = str(value or "").strip()
    try:
        if re.fullmatch(r"\d{2}/\d{2}/\d{2}", raw):
            return datetime.strptime(raw, "%y/%m/%d").date().isoformat()
        if re.fullmatch(r"\d{8}", raw):
            return datetime.strptime(raw, "%Y%m%d").date().isoformat()
    except ValueError:
        pass
    return None


def market_session(at=None):
    """Conservative KRX regular-session gate; no automatic quote calls on holidays."""
    current = at.astimezone(ZoneInfo("Asia/Seoul")) if at else datetime.now(ZoneInfo("Asia/Seoul"))
    today = current.date()
    closure = holidays.KR(years=today.year)
    last_day = date(today.year, 12, 31)
    while last_day.weekday() >= 5 or last_day in closure:
        last_day -= timedelta(days=1)
    if today.weekday() >= 5:
        reason = "주말"
    elif today in closure or today == last_day:
        reason = "휴장일"
    elif not time(9, 0) <= current.time() < time(15, 30):
        reason = "정규장 외 시간"
    else:
        reason = "정규장 운영 중"
    return {
        "is_open": reason == "정규장 운영 중",
        "reason": reason,
        "as_of": current.isoformat(),
        "timezone": "Asia/Seoul",
    }


def snapshot_view(item, timezone):
    if not item:
        return None
    value = dict(item.value)
    if item.kind == "holdings" and value.get("quantity_basis") != "broker_residual_v3":
        # Preserve historical records, but do not present their old inferred quantity.
        value["items"] = [{**row, "quantity": None} for row in value.get("items", [])]
        value["quantity_note"] = "이전 잔고 계산 기록 · 실제 보유수량 새로 조회 필요"
    if item.kind == "market" and not value.get("reference_close"):
        # Older snapshots were saved before comparison against the actual reference close.
        value.update(
            change=None,
            change_rate=None,
            direction="unknown",
            quote_note="이전 등락값은 검증 전 기록입니다. 새로 조회하세요.",
        )
    return {
        "environment": item.environment,
        "collected_at": local_time(item.created_at, timezone),
        **value,
    }


class AccountRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_no: str = Field(pattern=r"^[0-9]{11}$")


def eligible_account(item, environment):
    """NH PLUG lists live and mock accounts together; never cross the API environments."""
    allowed = {"01", "02"} if environment == "live" else {"03"}
    return (
        isinstance(item, dict)
        and re.fullmatch(r"[0-9]{11}", str(item.get("acct_no") or "")) is not None
        and str(item.get("acct_type") or "") in allowed
    )


def market_router(current_user, db_session, config, transport=None, default_attributes=None):
    router = APIRouter()

    @router.get("/api/market/session")
    def session_status(user=Depends(current_user)):
        return market_session()

    def cached(db, user, kind, symbol=""):
        return db.scalar(
            select(BrokerSnapshot)
            .where(
                BrokerSnapshot.owner_id == user.id,
                BrokerSnapshot.tenant_id == user.tenant_id,
                BrokerSnapshot.kind == kind,
                BrokerSnapshot.symbol == symbol,
            )
            .order_by(BrokerSnapshot.created_at.desc(), BrokerSnapshot.id.desc())
            .limit(1)
        )

    def credentials(db, user):
        from .user_brokers import personal_config

        scoped = personal_config(db, user, config, "namuh")
        auth = NamuhAuth(scoped, transport)
        owner_email = scoped.namuh_owner_email.strip().lower()
        owner_count = db.scalar(select(func.count()).select_from(User).where(User.email == owner_email))
        if user.email.lower() != owner_email or (scoped is config and owner_count != 1):
            raise HTTPException(403, "이 계정에는 나무증권 API 연결 권한이 없습니다.")
        connection = db.scalar(
            select(BrokerConnection).where(
                BrokerConnection.owner_id == user.id, BrokerConnection.tenant_id == user.tenant_id
            )
        )
        if not connection or not connection.enabled:
            raise HTTPException(409, "관리자 설정에서 나무증권 연결을 켜세요.")
        if auth.missing():
            raise HTTPException(409, "서버의 나무증권 연결 설정을 확인하세요.")
        if (
            not connection.token_ciphertext
            or connection.token_scope != auth.scope()
            or not connection.token_expires_at
            or aware(connection.token_expires_at) <= now() + timedelta(seconds=60)
        ):
            raise HTTPException(
                409,
                "나무증권 인증이 만료되었거나 재확인이 필요합니다.",
                headers={"X-Trading-Error": "NAMUH_AUTH_REQUIRED"},
            )
        try:
            token = auth.cipher().decrypt(connection.token_ciphertext.encode()).decode()
        except (InvalidToken, UnicodeDecodeError):
            raise HTTPException(
                409, "나무증권 인증 캐시를 읽지 못했습니다. 인증 확인을 다시 실행하세요."
            ) from None
        return connection.environment, token

    def request(db, user, path, payload):
        from .namuh_reads import read_gate, read_lane
        from .user_brokers import personal_config

        scoped = personal_config(db, user, config, "namuh")
        environment, token = credentials(db, user)
        gate = read_gate(user, environment, NamuhAuth(scoped).scope())
        with read_lane("display"):
            return gate.read(
                path, payload, lambda: uncached_request(db, user, path, payload), f"Bearer {token}"
            )

    def uncached_request(db, user, path, payload):
        from .user_brokers import personal_config

        scoped = personal_config(db, user, config, "namuh")
        environment, token = credentials(db, user)
        from .namuh_reads import read_gate

        gate = read_gate(user, environment, NamuhAuth(scoped).scope())
        gate.acquire()
        # No provider URL, credentials, request body or response body in API errors/logs.
        try:
            with httpx.Client(
                transport=transport, timeout=12, follow_redirects=False, trust_env=False
            ) as client:
                response = client.post(
                    API_URLS[environment] + path,
                    json={"Input_0": payload},
                    headers={
                        "x-client-id": scoped.namuh_app_key.get_secret_value(),
                        "x-client-secret": scoped.namuh_app_secret.get_secret_value(),
                        "authorization": f"Bearer {token}",
                        "content-type": "application/json; charset=UTF-8",
                    },
                )
            if response.status_code == 429:
                gate.limited(response.headers.get("Retry-After"))
            if len(response.content) > 2_000_000:
                raise HTTPException(502, "나무증권 응답 크기를 확인하지 못했습니다.")
            if response.status_code != 200:
                try:
                    failure = response.json()
                except ValueError:
                    failure = {}
                code = (
                    failure.get("rsp_cd") or failure.get("error_code") if isinstance(failure, dict) else None
                )
                safe_code = (
                    code if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,40}", code) else None
                )
                if response.status_code == 400 and safe_code == "IGW40023" and environment == "mock":
                    raise HTTPException(
                        409,
                        "나무증권 모의투자에서는 이 조회 API를 제공하지 않습니다 (IGW40023). "
                        "실전 시세를 조회하려면 관리자가 나무증권 연결 환경을 실전투자로 변경하고 인증 확인을 다시 해야 합니다. "
                        "환경은 자동 전환하지 않으며 주문 기능은 연결되지 않았습니다.",
                    )
                if response.status_code == 401:
                    raise HTTPException(
                        409, "나무증권 토큰이 거부되었습니다. 관리자 설정에서 인증 확인을 다시 실행하세요."
                    )
                raise HTTPException(
                    502,
                    f"나무증권 조회 거부 · HTTP {response.status_code}"
                    + (f" · {safe_code}" if safe_code else ""),
                )
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("Unexpected provider response")
            message = data.get("message")
            code = data.get("rsp_cd") or (message.get("rsp_cd") if isinstance(message, dict) else None)
            if code == "IGW42902":
                gate.limited(response.headers.get("Retry-After"))
            if path == "/krstock/inquiry/v1/balance":
                data["_has_more"] = (
                    response.headers.get("cont-yn") == "Y"
                    or bool(data.get("cts"))
                    or data.get("cts_flag") not in (None, "", "N", "0")
                )
            return data, environment
        except HTTPException:
            raise
        except httpx.TimeoutException:
            raise HTTPException(504, "나무증권 응답 시간이 초과됐습니다.") from None
        except (httpx.HTTPError, ValueError, json.JSONDecodeError):
            raise HTTPException(502, "나무증권 응답을 확인하지 못했습니다.") from None

    def instrument(db, user, code):
        if not re.fullmatch(r"[0-9]{6}", code):
            raise HTTPException(422, "6자리 국내 종목 코드를 입력하세요.")
        row = db.scalar(
            select(Instrument).where(Instrument.tenant_id == user.tenant_id, Instrument.code == code)
        )
        if row and row.value.get("status") == "active":
            return {"code": row.code, **row.value}
        portfolio = cached(db, user, "holdings")
        held = (
            next((item for item in portfolio.value.get("items", []) if item.get("code") == code), None)
            if portfolio
            else None
        )
        if held:
            return {
                "code": code,
                "name": held["name"],
                "market": "KRX",
                "asset_type": "stock",
                "source_note": "NH PLUG 보유종목 조회 기록",
            }
        entry = db.scalar(select(BrokerSnapshot).where(
            BrokerSnapshot.owner_id == user.id, BrokerSnapshot.tenant_id == user.tenant_id,
            BrokerSnapshot.kind == "analysis_entry", BrokerSnapshot.symbol == code))
        if entry and entry.value.get("status") == "active":
            return dict(entry.value)
        raise HTTPException(404, "사용 가능한 등록·보유 종목을 찾지 못했습니다.")

    @router.get("/api/market/instruments/{code}")
    def instrument_snapshot(code: str, user=Depends(current_user), db=Depends(db_session)):
        row = instrument(db, user, code)
        return {
            "instrument": row,
            "snapshot": snapshot_view(cached(db, user, "market", code), config.timezone),
        }

    @router.post("/api/market/instruments/{code}/refresh")
    def refresh_instrument(
        code: str,
        auto: bool = False,
        quote_only: bool = False,
        user=Depends(current_user),
        db=Depends(db_session),
    ):
        if user.role == "viewer":
            raise HTTPException(403, "조회 전용 사용자는 외부 데이터 갱신을 요청할 수 없습니다.")
        if quote_only and not auto:
            raise HTTPException(422, "빠른 시세 조회는 장중 자동 갱신에서만 사용할 수 있습니다.")
        row = instrument(db, user, code)
        recent = cached(db, user, "market", code)
        if auto:
            session = market_session()
            if not session["is_open"]:
                raise HTTPException(409, "정규장 외에는 자동 시세 갱신을 하지 않습니다.")
            if recent and (now() - aware(recent.created_at)).total_seconds() < 2.5:
                return {"instrument": row, "snapshot": snapshot_view(recent, config.timezone)}
        quote, environment = request(
            db, user, "/krstock/quote/v1/currentPrice", {"market_cd": "KRX", "iem_cd": code}
        )
        q = quote.get("Output_0")
        if not isinstance(q, dict) or numeric(q.get("stck_prpr")) is None:
            raise HTTPException(502, "현재가가 없는 응답입니다. 이전 기록을 유지합니다.")
        fast_quote = bool(
            quote_only and recent and recent.value.get("chart") and recent.value.get("chart_fetched_at")
        )
        if fast_quote:
            chart = [dict(point) for point in recent.value["chart"] if not point.get("provisional")]
            chart_fetched_at = recent.value["chart_fetched_at"]
        else:
            daily, _ = request(
                db,
                user,
                "/krstock/quote/v1/currentDaily",
                {"market_cd": "KRX", "iem_cd": code, "array_cnt": "250", "view_main_yn": "Y"},
            )
            bars = daily.get("Output_0")
            if not isinstance(bars, list):
                raise HTTPException(502, "나무증권 시세 형식을 확인하지 못했습니다. 이전 기록을 유지합니다.")
            chart = []
            for point in bars:
                if not isinstance(point, dict):
                    continue
                date = daily_date(point.get("bsop_date"))
                close = numeric(point.get("stck_clpr"))
                if close is None:
                    close = numeric(point.get("clpr"))
                if date and close is not None:
                    chart.append(
                        {
                            "date": date,
                            "close": close,
                            "high": numeric(point.get("stck_hgpr")),
                            "volume": numeric(point.get("acml_vol")),
                        }
                    )
            chart_fetched_at = now().isoformat()
        chart.sort(key=lambda item: item["date"])
        session = market_session()
        today = session["as_of"][:10]
        current_price = numeric(q.get("stck_prpr"))
        volume = numeric(q.get("acml_vol"))
        if session["is_open"] and current_price is not None:
            live_bar = {
                "date": today,
                "close": current_price,
                "high": numeric(q.get("stck_hgpr")),
                "volume": volume,
                "provisional": True,
            }
            if chart and chart[-1]["date"] == today:
                chart[-1] = live_bar
            elif not chart or chart[-1]["date"] < today:
                chart.append(live_bar)
        for index, point in enumerate(chart):
            previous = chart[index - 1]["close"] if index else None
            point["change"] = point["close"] - previous if previous is not None else None
            point["change_rate"] = (
                round(point["change"] / previous * 100, 2) if previous not in (None, 0) else None
            )
        reference_close = numeric(q.get("stck_prdy_clpr"))
        change, change_rate, direction = market_change(
            q.get("prdy_vrss"),
            q.get("prdy_ctrt"),
            str(q.get("prdy_vrss_sign") or ""),
            current_price,
            reference_close,
        )
        if (
            fast_quote
            and recent
            and all(
                (
                    recent.value.get("price") == current_price,
                    recent.value.get("volume") == volume,
                    recent.value.get("reference_close") == reference_close,
                    recent.value.get("change") == change,
                    recent.value.get("change_rate") == change_rate,
                    recent.value.get("chart") == chart,
                )
            )
        ):
            return {"instrument": row, "snapshot": snapshot_view(recent, config.timezone)}
        value = {
            "code": code,
            "name": row.get("name", code),
            "price": current_price,
            "volume": volume,
            "change": change,
            "reference_close": reference_close,
            "change_rate": change_rate,
            "direction": direction,
            "chart": chart,
            "source": "NH PLUG currentPrice/currentDaily",
            "chart_note": None if chart else "일별 차트 데이터가 없습니다.",
            "quote_note": None
            if reference_close is not None
            else "전일종가가 없어 등락 방향을 확인할 수 없습니다.",
            "chart_fetched_at": chart_fetched_at,
        }
        saved = BrokerSnapshot(
            tenant_id=user.tenant_id,
            owner_id=user.id,
            kind="market",
            symbol=code,
            environment=environment,
            value=value,
            created_at=aware(datetime.fromisoformat(quote["_read_at"])) if quote.get("_read_at") else now(),
        )
        db.add(saved)
        audit(
            db,
            user,
            "broker.market_refreshed",
            code,
            {"environment": environment, "chart_points": len(chart)},
        )
        db.commit()
        return {"instrument": row, "snapshot": snapshot_view(saved, config.timezone)}

    @router.get("/api/portfolio/holdings")
    def holdings(user=Depends(current_user), db=Depends(db_session)):
        return {"snapshot": snapshot_view(cached(db, user, "holdings"), config.timezone)}

    @router.post("/api/portfolio/holdings/refresh")
    def refresh_holdings(
        payload: AccountRequest,
        background: BackgroundTasks,
        user=Depends(current_user),
        db=Depends(db_session),
    ):
        if user.role == "viewer":
            raise HTTPException(403, "조회 전용 사용자는 외부 데이터 갱신을 요청할 수 없습니다.")
        accounts, environment = request(db, user, "/n2/acctinfo", {})
        listed = accounts.get("Output_0")
        if not isinstance(listed, list) or payload.account_no not in {
            str(item["acct_no"]) for item in listed if eligible_account(item, environment)
        }:
            raise HTTPException(422, "현재 조회 환경에서 사용 가능한 계좌를 선택하세요.")
        raw, _ = request(
            db,
            user,
            "/krstock/inquiry/v1/balance",
            {
                "act_no": payload.account_no,
                "bnc_bse_cd": "1",
                "ltg_aot_dit_cd": "1",
                "aet_bse": "1",
                "qut_dit_cd": "KRX",
                "aly_qut_cd": "1",
            },
        )
        summary, rows = raw.get("Output_0"), raw.get("Output_1")
        if not isinstance(summary, dict) or not isinstance(rows, list):
            raise HTTPException(502, "나무증권 잔고 형식을 확인하지 못했습니다. 이전 기록을 유지합니다.")
        items = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            code = str(item.get("iem_cd") or "")
            if not re.fullmatch(r"[0-9]{6}", code):
                continue
            items.append(
                {
                    "code": code,
                    "name": str(item.get("iem_nm") or code)[:80],
                    "quantity": holding_quantity(item),
                    "balance_quantity": numeric(item.get("itg_bnc_qty")),
                    "unsettled_quantity": numeric(item.get("ny_stl_qty")),
                    "residual_quantity": numeric(item.get("rsdl_qty")),
                    "purchase_price": numeric(item.get("phs_pr")),
                    "price": numeric(item.get("now_pr")),
                    "valuation": numeric(item.get("eal_amt")),
                    "profit": numeric(item.get("eal_pls_amt")),
                    "profit_rate": numeric(item.get("pft_rt")),
                }
            )
        value = {
            "quantity_basis": "broker_residual_v3",
            "account_last4": payload.account_no[-4:],
            "items": items,
            "total": len(items),
            "valuation": numeric(summary.get("tot_eal_amt")),
            "profit": numeric(summary.get("tot_eal_pls")),
            "profit_rate": numeric(summary.get("pft_rt")),
            "source": "NH PLUG balance",
            "partial": bool(raw.get("_has_more") or raw.get("cts_flag") not in (None, "", "N", "0")),
        }
        saved = BrokerSnapshot(
            tenant_id=user.tenant_id,
            owner_id=user.id,
            kind="holdings",
            symbol="",
            environment=environment,
            value=value,
            created_at=aware(datetime.fromisoformat(raw["_read_at"])) if raw.get("_read_at") else now(),
        )
        db.add(saved)
        audit(
            db,
            user,
            "broker.holdings_refreshed",
            user.id,
            {"environment": environment, "count": len(items), "partial": value["partial"]},
        )
        db.commit()
        if default_attributes:
            default_attributes(
                db, user, [{"code": item["code"], "name": item["name"]} for item in items], background
            )
        return {"snapshot": snapshot_view(saved, config.timezone)}

    @router.get("/api/portfolio/accounts")
    def account_options(user=Depends(current_user), db=Depends(db_session)):
        result, environment = request(db, user, "/n2/acctinfo", {})
        rows = result.get("Output_0")
        if not isinstance(rows, list):
            raise HTTPException(502, "나무증권 계좌 목록 형식을 확인하지 못했습니다.")
        return {
            "environment": environment,
            "accounts": [
                {
                    "account_no": str(item["acct_no"]),
                    "account_last4": str(item["acct_no"])[-4:],
                    "account_type": str(item.get("acct_type") or ""),
                }
                for item in rows
                if eligible_account(item, environment)
            ],
        }

    return router
