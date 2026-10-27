import io
import json
import zipfile

import httpx
import pytest
from conftest import CODE, EMAIL, ORIGIN
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from nastack_trading.app import create_app
from nastack_trading.models import Tenant, User, UserSettings
from nastack_trading.security import create_session, hash_code

KEY = "a" * 40
PATH = "/api/user/connections/opendart"
QUERY = {"code": "005930", "year": 2025, "report": "11011", "fs_div": "CFS"}


def mock_transport(calls, key=KEY, status="000", callback=None):
    def respond(request):
        calls.append(request.url.path)
        assert request.url.host == "opendart.fss.or.kr"
        assert request.url.params["crtfc_key"] == key
        if callback:
            callback()
        if status not in {"000", "013"}:
            return httpx.Response(200, json={"status": status, "message": key})
        if request.url.path.endswith("corpCode.xml"):
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, "w") as archive:
                archive.writestr("CORPCODE.xml", '<result><list><corp_code>00126380</corp_code><corp_name>삼성전자</corp_name><stock_code>005930</stock_code></list></result>')
            return httpx.Response(200, content=stream.getvalue())
        if request.url.path.endswith("list.json"):
            return httpx.Response(200, json={"status": "013"})
        assert request.url.path.endswith("fnlttSinglAcntAll.json")
        assert request.url.params["corp_code"] == "00126380"
        return httpx.Response(200, json={"status": status, "list": [
            {"corp_code": "00126380", "bsns_year": "2025", "reprt_code": "11011", "account_nm": "자산총계", "sj_nm": "재무상태표", "thstrm_amount": "100"}]})
    return httpx.MockTransport(respond)


def app_config(setup):
    return setup[0].model_copy(update={"broker_encryption_key": SecretStr(Fernet.generate_key().decode())})


def login(c):
    assert c.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE}).status_code == 200


def save(c, version=0, enabled=True, key=KEY):
    return c.put(PATH, json={"version": version, "enabled": enabled, "fields": {"api_key": key}})


@pytest.mark.parametrize("cross_tenant", [False, True])
def test_encryption_owner_roles_relogin_merge_and_check_persistence(setup, cross_tenant, caplog):
    config, factory = app_config(setup), setup[1]
    calls = []
    caplog.set_level("DEBUG", logger="httpx")
    with TestClient(create_app(config, dimension_transport=mock_transport(calls)), headers={"origin": ORIGIN}) as c:
        assert c.post(PATH + "/test").status_code == 401
        login(c)
        assert save(c, key="sample").status_code == 422
        assert save(c).status_code == 200
        assert not calls  # Save is local only.
        assert KEY not in c.get(PATH).text
        assert save(c).status_code == 409
        assert c.post(PATH + "/test").json()["status"] == "connected"
        assert c.post(PATH + "/test").status_code == 429
        result = c.post(PATH + "/financial-check", json=QUERY)
        assert result.status_code == 200, result.text
        assert result.json()["status"] == "available" and result.json()["account_count"] == 1
        assert result.json()["financial_data_saved"] is False
        assert c.post(PATH + "/financial-check", json={**QUERY, "owner_id": "other"}).status_code == 422
        c.post("/api/auth/logout")
        login(c)
        assert c.get(PATH).json()["checks"]["opendart_financial"]["status"] == "available"
        with factory.begin() as db:
            user = db.scalar(select(User).where(User.email == EMAIL))
            row = db.get(UserSettings, user.id)
            assert KEY not in json.dumps(row.value)
            assert "thstrm_amount" not in json.dumps(row.value)
            tid = user.tenant_id
            if cross_tenant:
                tenant = Tenant(name="DART tenant", timezone="Asia/Seoul")
                db.add(tenant)
                db.flush()
                tid = tenant.id
            other = User(tenant_id=tid, email="dart-other@example.com", role="viewer", active=True, code_hash=hash_code(CODE))
            db.add(other)
            db.flush()
            other_id = other.id
            db.add(UserSettings(user_id=other.id, tenant_id=tid, value={"unrelated": "preserved"}))
            token = create_session(db, other, 24)
        c.cookies.clear()
        c.cookies.set("trading_session", token)
        assert not c.get(PATH).json()["configured"]
        assert c.get(PATH).json()["checks"] == {}
        count = len(calls)
        assert save(c).status_code == 403
        assert c.post(PATH + "/test").status_code == 403
        assert c.post(PATH + "/financial-check", json=QUERY).status_code == 403
        with factory.begin() as db:
            db.get(User, other_id).role = "trader"
        assert c.post(PATH + "/test").status_code == 409  # Never borrow A's key.
        assert len(calls) == count
        assert save(c, key="b" * 40).status_code == 200
        assert save(c, version=1, enabled=False, key="").status_code == 200
        assert c.post(PATH + "/test").status_code == 409
        with factory() as db:
            assert db.get(UserSettings, other_id).value["unrelated"] == "preserved"
    assert KEY not in caplog.text


@pytest.mark.parametrize("status,expected", [("013", "unavailable"), ("010", None), ("020", None)])
def test_no_financial_data_is_not_auth_success_or_zero_balance(setup, status, expected):
    calls = []
    with TestClient(create_app(app_config(setup), dimension_transport=mock_transport(calls, status=status)), headers={"origin": ORIGIN}) as c:
        login(c)
        save(c)
        r = c.post(PATH + "/financial-check", json=QUERY)
        assert KEY not in r.text
        if expected:
            assert r.status_code == 200 and r.json()["status"] == expected
        else:
            assert r.status_code == 502


def test_key_changed_during_check_is_not_applied(setup):
    config, factory = app_config(setup), setup[1]
    def revoke():
        from nastack_trading.user_brokers import cipher
        with factory.begin() as db:
            user = db.scalar(select(User).where(User.email == EMAIL))
            row = db.get(UserSettings, user.id)
            record = row.value["personal_connections"]["opendart"]
            row.value = {**row.value, "personal_connections": {"opendart": {
                "version": record["version"] + 1,
                "ciphertext": cipher(config).encrypt(json.dumps({"enabled": False, "api_key": KEY}).encode()).decode()}}}
    with TestClient(create_app(config, dimension_transport=mock_transport([], callback=revoke)), headers={"origin": ORIGIN}) as c:
        login(c)
        save(c)
        assert c.post(PATH + "/test").status_code == 409
        assert c.get(PATH).json()["checks"] == {}


def test_corrupt_zip_and_wrong_company_fail_closed(setup):
    from fastapi import HTTPException

    from nastack_trading.opendart import company
    for raw in [b"PKbad", b"<error>" + KEY.encode() + b"</error>"]:
        with pytest.raises(HTTPException) as error:
            company(raw, "005930")
        assert KEY not in error.value.detail


def test_missing_values_and_unmapped_etf_do_not_claim_success(setup):
    config = app_config(setup)
    underlying = mock_transport([])
    def respond(request):
        if request.url.path.endswith("fnlttSinglAcntAll.json"):
            return httpx.Response(200, json={"status": "000", "list": [{
                "corp_code": "00126380", "bsns_year": "2025", "reprt_code": "11011",
                "account_nm": "자산총계", "sj_nm": "재무상태표", "thstrm_amount": "-"}]})
        return underlying.handle_request(request)
    with TestClient(create_app(config, dimension_transport=httpx.MockTransport(respond)), headers={"origin": ORIGIN}) as c:
        login(c)
        save(c)
        result = c.post(PATH + "/financial-check", json={**QUERY, "code": "494310"})
        assert result.status_code == 200 and result.json()["status"] == "unmapped"
        assert result.json()["request_count"] == 1
    with TestClient(create_app(config, dimension_transport=httpx.MockTransport(respond)), headers={"origin": ORIGIN}) as c:
        login(c)
        # Advance only the synthetic test's check timestamp, never a real service.
        with setup[1].begin() as db:
            user = db.scalar(select(User).where(User.email == EMAIL))
            row = db.get(UserSettings, user.id)
            row.value = {**row.value, "connection_checks": {}}
        result = c.post(PATH + "/financial-check", json=QUERY)
        assert result.status_code == 200 and result.json()["status"] == "empty_values"
        assert result.json()["financial_data_saved"] is False
