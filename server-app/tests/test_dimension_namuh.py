from datetime import date, timedelta

import httpx
import pytest
from conftest import CODE, EMAIL, ORIGIN
from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_market import configured

from nastack_trading.app import create_app
from nastack_trading.brokers import NamuhAuth
from nastack_trading.db import now
from nastack_trading.dimension_catalog import BY_ID
from nastack_trading.dimension_namuh import fetch_etf
from nastack_trading.models import BrokerConnection, DimensionCollection, InstrumentMaster, User


def seed(setup, expired=False, wrong_name=False):
    config = configured(setup[0])
    with setup[1].begin() as db:
        user = db.scalar(select(User).where(User.email == EMAIL))
        db.add(BrokerConnection(owner_id=user.id, tenant_id=user.tenant_id, enabled=True, environment="live",
            token_ciphertext=Fernet(config.broker_encryption_key.get_secret_value()).encrypt(b"synthetic-token").decode(),
            token_scope=NamuhAuth(config).scope(), token_expires_at=now() + timedelta(hours=-1 if expired else 1)))
        db.add(InstrumentMaster(tenant_id=user.tenant_id, value={"items":[{
            "code":"091160", "name":"changed" if wrong_name else "KODEX 반도체", "asset_type":"etf", "status":"active"}]}))
    return config


def test_proxy_collection_uses_only_daily_read_and_records_identity(setup):
    config = seed(setup)
    seen = []
    def respond(request):
        seen.append(request.url.path)
        assert request.url.host == "api.nhplug.com"
        assert request.headers["authorization"] == "Bearer synthetic-token"
        assert request.read().decode().find('091160') >= 0
        return httpx.Response(200, json={"Output_0":[
            {"bsop_date":"26/01/02", "stck_clpr":"10000"},
            {"bsop_date":"20260105", "clpr":"10500"}]})
    with TestClient(create_app(config, broker_transport=httpx.MockTransport(respond)), headers={"origin":ORIGIN}) as client:
        client.post("/api/auth/login",json={"email":EMAIL,"access_code":CODE})
        response = client.post("/api/dimensions/sector-01/collect",json={"start":"2026-01-01","end":"2026-01-06"})
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["dimension"]["representation"] == "etf_proxy"
        assert data["coverage"]["source_observations"] == 2
        assert data["coverage"]["missing"] == 1
        assert data["points"][1]["value"] == 10500
    assert seen == ["/krstock/quote/v1/currentDaily"]
    with setup[1]() as db:
        saved = db.scalar(select(DimensionCollection))
        assert saved.value["instrument_name"] == "KODEX 반도체"
        assert saved.value["definition"]["representation"] == "etf_proxy"
        assert saved.value["adjustment_verified"] is False
        assert "synthetic-token" not in str(saved.value)


@pytest.mark.parametrize("problem", ["expired", "name", "owner", "duplicate", "nan", "empty", "provider", "zero"])
def test_rejects_auth_master_and_invalid_provider_data(setup, problem):
    config = seed(setup, expired=problem=="expired", wrong_name=problem=="name")
    if problem == "owner":
        config = config.model_copy(update={"namuh_owner_email":"somebody@example.com"})
    calls=[]
    def respond(request):
        calls.append(request.url.path)
        bar = {"bsop_date":"20260102", "stck_clpr":"NaN" if problem=="nan" else "0" if problem=="zero" else "100"}
        return httpx.Response(503 if problem=="provider" else 200, json={"Output_0":[] if problem=="empty" else [bar,bar] if problem=="duplicate" else [bar]})
    with setup[1]() as db:
        user=db.scalar(select(User).where(User.email==EMAIL))
        with pytest.raises(HTTPException):
            fetch_etf(db,user,config,BY_ID["sector-01"],date(2026,1,1),date(2026,1,6),httpx.MockTransport(respond))
    if problem in {"expired","name","owner"}:
        assert not calls
