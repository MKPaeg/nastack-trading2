from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
from conftest import CODE, EMAIL, ORIGIN
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from nastack_trading.app import create_app
from nastack_trading.brokers import AUTH_URL, NamuhAuth
from nastack_trading.db import now
from nastack_trading.market import daily_date, eligible_account, market_change, market_session, snapshot_view
from nastack_trading.models import BrokerConnection, BrokerSnapshot


def configured(config):
    return config.model_copy(update={
        "namuh_owner_email": EMAIL,
        "namuh_app_key": SecretStr("synthetic-key"),
        "namuh_app_secret": SecretStr("synthetic-secret"),
        "broker_encryption_key": SecretStr(Fernet.generate_key().decode()),
    })


def test_read_only_market_portfolio_snapshots_and_isolation(setup, monkeypatch):
    seen = []
    # Initial fixture contains two finalized bars; do not depend on the real market clock.
    monkeypatch.setattr("nastack_trading.market.market_session", lambda: {"is_open": False, "reason": "장 마감", "as_of": "2026-10-09T16:00:00+09:00"})

    def respond(request):
        path = request.url.path
        seen.append(path)
        if str(request.url).split("?")[0] == AUTH_URL:
            return httpx.Response(200, json={"access_token": "synthetic-token", "expires_in": 86400})
        assert request.headers["authorization"] == "Bearer synthetic-token"
        assert request.headers["x-client-id"] == "synthetic-key"
        if path.endswith("/currentPrice"):
            return httpx.Response(200, json={"Output_0": {"stck_prpr": "1200", "stck_prdy_clpr": "1100", "prdy_vrss": "100", "prdy_ctrt": "9.09", "prdy_vrss_sign": "2"}})
        if path.endswith("/currentDaily"):
            return httpx.Response(200, json={"Output_0": [
                {"bsop_date": "26/10/08", "stck_clpr": "1100", "prdy_vrss": "0"},
                {"bsop_date": "20261009", "stck_clpr": "1200", "prdy_vrss": "100"},
            ]})
        if path.endswith("/acctinfo"):
            return httpx.Response(200, json={"Output_0": [{"acct_no": "12345678901", "acct_type": "03"}]})
        if path.endswith("/balance"):
            return httpx.Response(200, json={"Output_0": {"tot_eal_amt": "12000", "tot_eal_pls": "1000", "pft_rt": "9.09"}, "Output_1": [
                {"iem_cd": "123456", "iem_nm": "시험 회사", "itg_bnc_qty": "10", "rsdl_qty": "10", "now_pr": "1200", "eal_amt": "12000", "eal_pls_amt": "1000", "pft_rt": "9.09"}
            ]})
        raise AssertionError(path)

    config = configured(setup[0])
    with TestClient(create_app(config, broker_transport=httpx.MockTransport(respond)), headers={"origin": ORIGIN}) as client:
        assert client.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE}).status_code == 200
        assert client.get("/api/portfolio/holdings").json()["snapshot"] is None
        instrument = client.post("/api/admin/instruments", json={
            "code": "123456", "name": "시험 회사", "market": "KOSPI", "asset_type": "stock",
            "status": "active", "source_note": "합성 테스트",
        })
        assert instrument.status_code == 201
        assert client.post("/api/market/instruments/123456/refresh").status_code == 409
        assert client.put("/api/admin/brokers/namuh", json={"version": 0, "enabled": True, "environment": "mock"}).status_code == 200
        assert client.post("/api/admin/brokers/namuh/verify", json={"version": 1}).status_code == 200
        market = client.post("/api/market/instruments/123456/refresh")
        assert market.status_code == 200, market.text
        assert market.json()["snapshot"]["price"] == 1200
        assert len(market.json()["snapshot"]["chart"]) == 2
        assert market.json()["snapshot"]["chart"][0]["date"] == "2026-10-08"
        assert market.json()["snapshot"]["chart"][1]["change"] == 100
        assert market.json()["snapshot"]["direction"] == "up"
        assert market.json()["snapshot"]["reference_close"] == 1100
        assert "이전 등락값" not in str(market.json()["snapshot"])
        with setup[1].begin() as db:
            saved_market = db.scalar(select(BrokerSnapshot).where(BrokerSnapshot.kind == "market"))
            saved_market.created_at = now() - timedelta(seconds=5)
        monkeypatch.setattr("nastack_trading.market.market_session", lambda: {"is_open": True, "reason": "정규장 운영 중", "as_of": "2026-10-09T10:00:00+09:00"})
        calls_before_fast = len(seen)
        fast = client.post("/api/market/instruments/123456/refresh?auto=true&quote_only=true")
        assert fast.status_code == 200, fast.text
        assert seen[calls_before_fast:] == []  # The just-confirmed same-user quote is reused briefly.
        assert fast.json()["snapshot"]["chart"][-1]["provisional"] is True
        calls_before_holiday = len(seen)
        monkeypatch.setattr("nastack_trading.market.market_session", lambda: {"is_open": False, "reason": "휴장일", "as_of": "2026-10-09T10:00:00+09:00"})
        assert client.get("/api/market/session").json()["reason"] == "휴장일"
        assert client.post("/api/market/instruments/123456/refresh?auto=true").status_code == 409
        assert len(seen) == calls_before_holiday
        assert client.get("/api/market/instruments/123456").json()["snapshot"]["price"] == 1200
        options = client.get("/api/portfolio/accounts")
        assert options.status_code == 200 and options.json()["accounts"][0]["account_last4"] == "8901"
        assert client.post("/api/portfolio/holdings/refresh", json={"account_no": "00000000000"}).status_code == 422
        portfolio = client.post("/api/portfolio/holdings/refresh", json={"account_no": "12345678901"})
        assert portfolio.status_code == 200, portfolio.text
        assert portfolio.json()["snapshot"]["items"][0]["quantity"] == 10
        assert "12345678901" not in portfolio.text
        with setup[1]() as db:
            snapshots = db.scalars(select(BrokerSnapshot)).all()
            assert len(snapshots) == 3
            assert all("12345678901" not in str(item.value) for item in snapshots)
        other = client.post("/api/users", json={"email": "other@example.com", "role": "trader"}).json()
        client.post("/api/auth/logout")
        client.post("/api/auth/login", json={"email": other["email"], "access_code": other["access_code"]})
        assert client.get("/api/portfolio/holdings").json()["snapshot"] is None
        assert client.get("/api/market/instruments/123456").json()["snapshot"] is None
        assert client.get("/api/portfolio/accounts").status_code == 403
        assert client.post("/api/market/instruments/123456/refresh").status_code == 403
        assert all("order" not in path for path in seen)


def test_market_provider_failure_preserves_previous_snapshot(setup):
    def respond(request):
        if request.url.path.endswith("/currentPrice"):
            return httpx.Response(200, json={"rsp_msg": "provider failure"})
        raise AssertionError(request.url.path)

    config = configured(setup[0])
    with setup[1].begin() as db:
        from nastack_trading.models import User
        user = db.scalar(select(User).where(User.email == EMAIL))
        db.add(BrokerConnection(
            owner_id=user.id, tenant_id=user.tenant_id, enabled=True, environment="mock",
            token_ciphertext=Fernet(config.broker_encryption_key.get_secret_value()).encrypt(b"synthetic-token").decode(),
            token_scope=NamuhAuth(config).scope(),
            token_expires_at=now() + timedelta(hours=1),
        ))
    with TestClient(create_app(config, broker_transport=httpx.MockTransport(respond)), headers={"origin": ORIGIN}) as client:
        client.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE})
        client.post("/api/admin/instruments", json={
            "code": "123456", "name": "시험 회사", "market": "KOSPI", "asset_type": "stock",
            "status": "active", "source_note": "합성 테스트",
        })
        assert client.post("/api/market/instruments/123456/refresh").status_code == 502
        assert client.get("/api/market/instruments/123456").json()["snapshot"] is None


def test_mock_unsupported_quote_explains_environment_without_fallback(setup):
    calls = []

    def respond(request):
        calls.append(str(request.url))
        return httpx.Response(400, json={
            "rsp_cd": "IGW40023",
            "rsp_msg": "모의투자에서는 제공하지 않는 API입니다. 실전투자 환경을 이용해주세요.",
        })

    config = configured(setup[0])
    with setup[1].begin() as db:
        from nastack_trading.models import User
        user = db.scalar(select(User).where(User.email == EMAIL))
        db.add(BrokerConnection(
            owner_id=user.id, tenant_id=user.tenant_id, enabled=True, environment="mock",
            token_ciphertext=Fernet(config.broker_encryption_key.get_secret_value()).encrypt(b"synthetic-token").decode(),
            token_scope=NamuhAuth(config).scope(),
            token_expires_at=now() + timedelta(hours=1),
        ))
    with TestClient(create_app(config, broker_transport=httpx.MockTransport(respond)), headers={"origin": ORIGIN}) as client:
        client.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE})
        client.post("/api/admin/instruments", json={
            "code": "123456", "name": "시험 회사", "market": "KOSPI", "asset_type": "stock",
            "status": "active", "source_note": "합성 테스트",
        })
        result = client.post("/api/market/instruments/123456/refresh")
        assert result.status_code == 409
        assert "모의투자" in result.json()["detail"] and "실전투자" in result.json()["detail"]
        assert "synthetic-token" not in result.text
        assert client.get("/api/market/instruments/123456").json()["snapshot"] is None
        assert len(calls) == 1 and calls[0].startswith("https://moapi.nhplug.com:8443/")


def test_expired_quote_token_requests_explicit_auth_recovery(setup):
    config = configured(setup[0])
    calls = []

    def respond(request):
        calls.append(request.url.path)
        raise AssertionError("Expired credentials must not reach the quote provider")

    with setup[1].begin() as db:
        from nastack_trading.models import User
        owner = db.scalar(select(User).where(User.email == EMAIL))
        db.add(BrokerConnection(
            owner_id=owner.id, tenant_id=owner.tenant_id, enabled=True, environment="live",
            token_ciphertext="expired-placeholder", token_scope=NamuhAuth(config).scope(),
            token_expires_at=now() - timedelta(days=1),
        ))
    with TestClient(create_app(config, broker_transport=httpx.MockTransport(respond)), headers={"origin": ORIGIN}) as client:
        client.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE})
        client.post("/api/admin/instruments", json={
            "code": "123456", "name": "시험 회사", "market": "KOSPI", "asset_type": "stock",
            "status": "active", "source_note": "합성 테스트",
        })
        response = client.post("/api/market/instruments/123456/refresh")
        assert response.status_code == 409
        assert response.headers["X-Trading-Error"] == "NAMUH_AUTH_REQUIRED"
        assert calls == []


def test_provider_sign_codes_and_daily_dates():
    assert market_change("150", "2.3", "2") == (150.0, 2.3, "up")
    assert market_change("150", "2.3", "5") == (-150.0, -2.3, "down")
    assert market_change("-150", "-2.3", "9") == (-150.0, -2.3, "down")
    assert market_change("0", "0", "3") == (0, 0, "flat")
    assert market_change("150", "-2.3", "") == (-150.0, -2.3, "down")
    assert market_change("2900", "5.91", "", "46200", "49100") == (-2900.0, -5.91, "down")
    assert market_change("2900", "5.91", "") == (None, None, "unknown")
    assert daily_date("26/10/09") == "2026-10-09"
    assert daily_date("20261009") == "2026-10-09"
    assert daily_date("26/99/09") is None


def test_account_type_matches_broker_environment():
    live = {"acct_no": "12345678901", "acct_type": "01"}
    proxy = {"acct_no": "12345678902", "acct_type": "02"}
    mock = {"acct_no": "12345678903", "acct_type": "03"}
    assert eligible_account(live, "live") and eligible_account(proxy, "live")
    assert not eligible_account(mock, "live")
    assert eligible_account(mock, "mock")
    assert not eligible_account(live, "mock")
    assert not eligible_account({"acct_no": "bad", "acct_type": "01"}, "live")


def test_live_account_list_hides_mock_and_rejects_wrong_environment(setup):
    paths = []

    def respond(request):
        paths.append(request.url.path)
        if request.url.path.endswith("/acctinfo"):
            return httpx.Response(200, json={"Output_0": [
                {"acct_no": "12345678901", "acct_type": "01"},
                {"acct_no": "12345678903", "acct_type": "03"},
            ]})
        raise AssertionError("An ineligible account must never trigger a balance call")

    config = configured(setup[0])
    with setup[1].begin() as db:
        from nastack_trading.models import User
        user = db.scalar(select(User).where(User.email == EMAIL))
        db.add(BrokerConnection(
            owner_id=user.id, tenant_id=user.tenant_id, enabled=True, environment="live",
            token_ciphertext=Fernet(config.broker_encryption_key.get_secret_value()).encrypt(b"synthetic-token").decode(),
            token_scope=NamuhAuth(config).scope(), token_expires_at=now() + timedelta(hours=1),
        ))
    with TestClient(create_app(config, broker_transport=httpx.MockTransport(respond)), headers={"origin": ORIGIN}) as client:
        client.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE})
        listing = client.get("/api/portfolio/accounts")
        assert listing.status_code == 200
        assert [item["account_type"] for item in listing.json()["accounts"]] == ["01"]
        rejected = client.post("/api/portfolio/holdings/refresh", json={"account_no": "12345678903"})
        assert rejected.status_code == 422
        assert paths == ["/n2/acctinfo"]  # Reuse never broadens eligible live/mock accounts.


def test_market_session_excludes_korean_holiday_and_off_hours():
    seoul = ZoneInfo("Asia/Seoul")
    assert market_session(datetime(2026, 10, 8, 10, 0, tzinfo=seoul))["is_open"]
    holiday = market_session(datetime(2026, 10, 9, 10, 0, tzinfo=seoul))
    assert not holiday["is_open"] and holiday["reason"] == "휴장일"
    assert not market_session(datetime(2026, 10, 8, 16, 0, tzinfo=seoul))["is_open"]
    assert not market_session(datetime(2026, 10, 10, 10, 0, tzinfo=seoul))["is_open"]


def test_old_unsigned_snapshot_keeps_history_but_hides_unverified_change():
    old = SimpleNamespace(
        kind="market", environment="live", created_at=now(),
        value={"price": 46200, "change": 2900, "change_rate": 5.91, "direction": "up", "chart": []},
    )
    view = snapshot_view(old, "Asia/Seoul")
    assert view["price"] == 46200 and view["change"] is None
    assert view["direction"] == "unknown" and "새로 조회" in view["quote_note"]
    assert old.value["change"] == 2900
