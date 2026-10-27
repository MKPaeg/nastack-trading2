import httpx
import pytest
from sqlalchemy import select

from nastack_trading.instrument_master import HEADER, SOURCE, download_master, parse_master, save_master
from nastack_trading.models import Instrument, Tenant, User
from nastack_trading.security import create_session, hash_code

HEADER_FIXTURE = """typedef struct {
char sCode[6]; char sMarket[1]; char sKorName[41]; char rest[113];
char eStop[1]; char more[3]; char gVenture[1]; char tail[70]; char dummy[1];
} NEW_STOCK;"""


def record(code="123456", name="*시험 종목", market="1", etf="8"):
    row = bytearray(b" " * 237)
    row[0:6] = code.encode()
    row[6:7] = market.encode()
    encoded = name.encode("cp949")
    row[7 : 7 + len(encoded)] = encoded
    row[161] = ord("N")
    row[165] = ord(etf)
    row[-1] = 10
    return bytes(row)


def test_public_download_and_parser():
    def handler(request):
        assert str(request.url) in {HEADER, SOURCE}
        assert "authorization" not in request.headers and not request.url.query
        return httpx.Response(
            200, content=HEADER_FIXTURE.encode() if str(request.url) == HEADER else record()
        )

    data = download_master(httpx.MockTransport(handler))
    assert data["items"][0]["name"] == "시험 종목"
    assert data["items"][0]["asset_type"] == "etf"
    assert (
        parse_master(HEADER_FIXTURE, record(market="4", name="#코스닥 시험"))["items"][0]["market"]
        == "KOSDAQ"
    )
    for bad in [record()[:-1], record()[:-1] + b"X", record() + record(), b""]:
        with pytest.raises(ValueError):
            parse_master(HEADER_FIXTURE, bad)
    with pytest.raises(ValueError):
        parse_master(HEADER_FIXTURE.replace("[113]", "[114]"), record())
    with pytest.raises(httpx.HTTPStatusError):
        download_master(
            httpx.MockTransport(
                lambda _: httpx.Response(302, headers={"location": "https://elsewhere.invalid"})
            )
        )


def seed(setup):
    value = parse_master(HEADER_FIXTURE, record())
    with setup[1].begin() as db:
        save_master(db, db.scalar(select(User).where(User.role == "admin")), value)
    return value


def test_search_register_and_watchlist(admin, setup):
    assert admin.get("/api/instrument-search").json()["updated_at"] is None
    value = seed(setup)
    result = admin.get("/api/instrument-search?q=시험").json()
    assert result["total"] == 1 and result["items"][0]["registered_id"] is None
    assert admin.get("/api/instrument-search?q=%25").json()["total"] == 0
    assert admin.get("/api/instrument-search?page_size=6").status_code == 422
    path = f"/api/admin/instrument-search/123456?snapshot={value['sha256']}"
    assert admin.post(path.replace(value["sha256"], "0" * 64)).status_code == 409
    assert admin.post(path.replace("123456", "999999")).status_code == 404
    response = admin.post(path)
    assert response.status_code == 201
    item = response.json()
    assert item["name"] == "시험 종목" and item["registration"] == "nh_public_master"
    assert admin.post(path).status_code == 409
    assert admin.post("/api/watchlist", json={"instrument_id": item["id"]}).status_code == 201
    assert admin.get("/api/instrument-search").json()["items"][0]["registered_id"] == item["id"]
    with setup[1]() as db:
        assert db.get(Instrument, item["id"]).value["source_snapshot"] == value["sha256"]
    assert admin.post("/api/admin/instrument-master/refresh").status_code == 429


def test_refresh_failure_preserves_snapshot(admin, setup, monkeypatch):
    def broken():
        raise ValueError("upstream private response must not leak")

    monkeypatch.setattr("nastack_trading.instruments.download_master", broken)
    response = admin.post("/api/admin/instrument-master/refresh")
    assert response.status_code == 502 and "private" not in response.text
    assert admin.get("/api/instrument-search").json()["updated_at"] is None


def test_lookup_permissions(admin, client, setup):
    seed(setup)
    other = admin.post("/api/users", json={"email": "lookup@example.com", "role": "viewer"}).json()
    admin.post("/api/auth/logout")
    client.post("/api/auth/login", json={"email": other["email"], "access_code": other["access_code"]})
    assert client.get("/api/instrument-search").json()["total"] == 1
    assert client.post("/api/admin/instrument-master/refresh").status_code == 403
    assert client.post("/api/admin/instrument-search/123456?snapshot=" + "0" * 64).status_code == 403


def test_trader_can_register_official_stock_without_admin(admin, client, setup):
    value = seed(setup)
    other = admin.post("/api/users", json={"email": "trader-lookup@example.com", "role": "trader"}).json()
    admin.post("/api/auth/logout")
    assert (
        client.post(
            "/api/auth/login", json={"email": other["email"], "access_code": other["access_code"]}
        ).status_code
        == 200
    )
    result = client.post("/api/admin/instrument-search/123456?snapshot=" + value["sha256"])
    assert result.status_code == 201, result.text
    assert client.post("/api/watchlist", json={"instrument_id": result.json()["id"]}).status_code == 201
    assert client.get("/api/watchlist").json()["total"] == 1
    assert client.get("/api/users").status_code == 403


def test_other_tenant_cannot_use_snapshot(admin, setup):
    value = seed(setup)
    with setup[1].begin() as db:
        tenant = Tenant(name="lookup other", timezone="Asia/Seoul")
        db.add(tenant)
        db.flush()
        user = User(
            tenant_id=tenant.id,
            email="lookup-other@example.com",
            role="admin",
            code_hash=hash_code("synthetic-test-only"),
        )
        db.add(user)
        db.flush()
        token = create_session(db, user, 24)
    admin.cookies.set("trading_session", token)
    assert admin.get("/api/instrument-search").json()["total"] == 0
    assert admin.post("/api/admin/instrument-search/123456?snapshot=" + value["sha256"]).status_code == 409
