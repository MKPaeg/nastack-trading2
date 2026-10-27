import json
from datetime import timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from conftest import CODE, EMAIL, ORIGIN
from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select, update
from test_market import configured

from nastack_trading.app import create_app
from nastack_trading.db import now
from nastack_trading.kiwoom import KiwoomReader
from nastack_trading.kiwoom_catalog import KIWOOM_SERIES
from nastack_trading.models import DimensionCollection, KiwoomConnection


def configuration(config):
    return configured(config).model_copy(
        update={
            "kiwoom_owner_email": EMAIL,
            "kiwoom_app_key": SecretStr("synthetic-kiwoom-key"),
            "kiwoom_app_secret": SecretStr("synthetic-kiwoom-secret"),
        }
    )


def provider(calls, problem=None):
    def respond(request):
        calls.append((request.url.host, request.url.path, request.headers.get("api-id")))
        body = json.loads(request.content)
        if request.url.path == "/oauth2/token":
            assert body["appkey"] == "synthetic-kiwoom-key"
            return httpx.Response(
                200,
                json={"return_code": 3, "return_msg": "secret synthetic-kiwoom-secret"}
                if problem == "auth"
                else {
                    "return_code": 0,
                    "token": "synthetic-kiwoom-token",
                    "expires_dt": (now() + timedelta(hours=3))
                    .astimezone(ZoneInfo("Asia/Seoul"))
                    .strftime("%Y%m%d%H%M%S"),
                },
            )
        assert request.headers["authorization"] == "Bearer synthetic-kiwoom-token"
        api = request.headers["api-id"]
        if api == "ka10101":
            return httpx.Response(
                200,
                json={
                    "return_code": 0,
                    "list": [
                        {"code": code, "name": "changed" if problem == "master" else name}
                        for code, name, market, proxy in KIWOOM_SERIES.values()
                    ],
                },
            )
        assert api == "ka20006" and request.url.path == "/api/dostk/chart"
        if problem == "http":
            return httpx.Response(503)
        if problem == "rate":
            return httpx.Response(429)
        rows = [{"dt": "20260106", "cur_prc": "-78197"}, {"dt": "20260105", "cur_prc": "80889"}]
        if problem == "duplicate":
            rows.append(rows[0])
        if problem == "nan":
            rows[0]["cur_prc"] = "NaN"
        if problem == "decimal":
            rows[0]["cur_prc"] = "781.97"
        if problem == "date":
            rows[0]["dt"] = "20260230"
        if problem == "empty":
            rows = []
        if problem == "zero":
            rows[0]["cur_prc"] = "0"
        if problem == "wrong_code":
            code = "008"
        else:
            code = body["inds_cd"]
        headers = {}
        if problem == "pages":
            if request.headers.get("cont-yn") == "Y":
                assert request.headers["next-key"] == "page2"
                rows = [{"dt": "20260102", "cur_prc": "70000"}]
            else:
                headers = {"cont-yn": "Y", "next-key": "page2"}
        if problem == "stalled":
            headers = {"cont-yn": "Y", "next-key": "repeated"}
        return httpx.Response(
            200, headers=headers, json={"return_code": 0, "inds_cd": code, "inds_dt_pole_qry": rows}
        )

    return httpx.MockTransport(respond)


def login(client):
    assert client.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE}).status_code == 200


def enable(client):
    response = client.put(
        "/api/admin/brokers/kiwoom", json={"version": 0, "enabled": True, "environment": "live"}
    )
    assert response.status_code == 200, response.text


def test_connection_auth_cache_collection_provenance_and_policy(setup):
    config = configuration(setup[0])
    calls = []
    with TestClient(
        create_app(config, kiwoom_transport=provider(calls, "pages")), headers={"origin": ORIGIN}
    ) as client:
        assert client.get("/api/admin/brokers/kiwoom").status_code == 401
        login(client)
        assert client.get("/api/admin/brokers/kiwoom").json()["enabled"] is False
        enable(client)
        verified = client.post("/api/admin/brokers/kiwoom/verify", json={"version": 1})
        assert verified.status_code == 200, verified.text
        assert verified.json()["token_valid"] is True
        assert "synthetic-kiwoom-token" not in verified.text
        assert client.post("/api/admin/brokers/kiwoom/verify", json={"version": 1}).status_code == 200
        response = client.post(
            "/api/dimensions/market-03/collect", json={"start": "2026-01-01", "end": "2026-01-06"}
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["coverage"]["source_observations"] == 3
        assert data["points"][-1]["value"] == 781.97
        assert data["dimension"]["unit"] == "지수 포인트"
        assert data["dimension"]["representation"] == "original_series"
        assert (
            client.put(
                "/api/admin/brokers/kiwoom", json={"version": 0, "enabled": False, "environment": "live"}
            ).status_code
            == 409
        )
        disabled = client.put(
            "/api/admin/brokers/kiwoom", json={"version": 1, "enabled": False, "environment": "live"}
        )
        assert disabled.status_code == 200 and disabled.json()["token_valid"] is False
        assert (
            client.get(
                "/api/dimensions/market-03/series", params={"start": "2026-01-01", "end": "2026-01-06"}
            ).json()["points"]
            == data["points"]
        )
    assert sum(path == "/oauth2/token" for host, path, api in calls) == 1
    assert all(host == "api.kiwoom.com" for host, path, api in calls)
    assert {api for host, path, api in calls} == {None, "ka10101", "ka20006"}
    with setup[1]() as db:
        connection = db.scalar(select(KiwoomConnection))
        assert connection.token_ciphertext is None
        saved = db.scalar(select(DimensionCollection))
        assert saved.value["scale_divisor"] == 100 and len(saved.value["page_sha256"]) == 2
        assert "synthetic-kiwoom-token" not in str(saved.value)


@pytest.mark.parametrize(
    "problem",
    [
        "auth",
        "master",
        "http",
        "rate",
        "duplicate",
        "nan",
        "decimal",
        "date",
        "empty",
        "zero",
        "wrong_code",
        "stalled",
    ],
)
def test_bad_responses_do_not_create_success_or_leak_secrets(setup, problem):
    config = configuration(setup[0])
    calls = []
    with TestClient(
        create_app(config, kiwoom_transport=provider(calls, problem)), headers={"origin": ORIGIN}
    ) as client:
        login(client)
        enable(client)
        response = client.post(
            "/api/dimensions/market-03/collect", json={"start": "2026-01-01", "end": "2026-01-06"}
        )
        assert response.status_code in {409, 429, 502}, response.text
        assert all(
            secret not in response.text for secret in ["synthetic-kiwoom-secret", "synthetic-kiwoom-token"]
        )
        assert (
            client.get(
                "/api/dimensions/market-03/series", params={"start": "2026-01-01", "end": "2026-01-06"}
            ).json()["coverage"]["source_observations"]
            == 0
        )
    with setup[1]() as db:
        assert db.scalar(select(DimensionCollection)).status == "failed"


def test_owner_viewer_and_fixed_endpoint_guard(setup):
    config = configuration(setup[0])
    calls = []
    with TestClient(
        create_app(config, kiwoom_transport=provider(calls)), headers={"origin": ORIGIN}
    ) as client:
        login(client)
        enable(client)
        added = client.post(
            "/api/users", json={"email": "kiwoom-viewer@example.com", "role": "viewer"}
        ).json()
        client.post("/api/auth/logout")
        client.post("/api/auth/login", json={"email": added["email"], "access_code": added["access_code"]})
        assert (
            client.post(
                "/api/dimensions/market-03/collect", json={"start": "2026-01-01", "end": "2026-01-06"}
            ).status_code
            == 403
        )
        assert client.get("/api/admin/brokers/kiwoom").status_code == 403
    assert calls == []
    reader = KiwoomReader(config, provider(calls))
    with pytest.raises(HTTPException):
        reader.read("token", "live", "kt10000", {})
    with pytest.raises(HTTPException):
        reader.post("live", "/api/dostk/ordr", {})
    assert calls == []


def test_failed_refresh_preserves_prior_success(setup):
    config = configuration(setup[0])
    payload = {"start": "2026-01-01", "end": "2026-01-06"}
    with TestClient(create_app(config, kiwoom_transport=provider([])), headers={"origin": ORIGIN}) as client:
        login(client)
        enable(client)
        assert client.post("/api/dimensions/market-03/collect", json=payload).status_code == 200
    with setup[1].begin() as db:
        db.execute(update(DimensionCollection).values(created_at=now() - timedelta(minutes=1)))
        connection = db.scalar(select(KiwoomConnection))
        assert (
            Fernet(config.broker_encryption_key.get_secret_value()).decrypt(
                connection.token_ciphertext.encode()
            )
            == b"synthetic-kiwoom-token"
        )
    with TestClient(
        create_app(config, kiwoom_transport=provider([], "http")), headers={"origin": ORIGIN}
    ) as client:
        login(client)
        assert (
            client.post("/api/dimensions/market-03/collect", json={**payload, "force": True}).status_code
            == 502
        )
        data = client.get("/api/dimensions/market-03/series", params=payload).json()
        assert data["coverage"]["source_observations"] == 2 and data["last_attempt"]["status"] == "failed"
