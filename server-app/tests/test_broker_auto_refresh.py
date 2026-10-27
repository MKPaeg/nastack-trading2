from datetime import timedelta
from zoneinfo import ZoneInfo

import httpx
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import select

from nastack_trading.brokers import refresh_automatic_namuh
from nastack_trading.db import aware, now
from nastack_trading.kiwoom import KiwoomReader
from nastack_trading.live_worker import LiveWorker
from nastack_trading.models import BrokerConnection, BrokerProbe, KiwoomConnection, TradePlan, User
from nastack_trading.trade_gateway import TradeGateway


def test_namuh_refreshes_only_active_owner_before_expiry(setup):
    config, factory = setup
    config = config.model_copy(update={
        "namuh_owner_email": "admin@example.com",
        "namuh_app_key": SecretStr("synthetic-key"),
        "namuh_app_secret": SecretStr("synthetic-secret"),
        "broker_encryption_key": SecretStr(Fernet.generate_key().decode()),
    })
    calls = []

    def respond(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"access_token": "new-synthetic-token", "expires_in": 86400})

    transport = httpx.MockTransport(respond)
    with factory() as db:
        user = db.scalar(select(User))
        row = BrokerConnection(
            owner_id=user.id, tenant_id=user.tenant_id, enabled=True, environment="live",
            next_probe_at=now() - timedelta(seconds=1),
        )
        db.add(row)
        db.commit()
        assert not refresh_automatic_namuh(db, user, config, transport)
        assert calls == []
        db.add(TradePlan(owner_id=user.id, tenant_id=user.tenant_id, broker="namuh",
                         code="034220", value={"active": True, "mode": "automatic"}))
        db.commit()
        assert refresh_automatic_namuh(db, user, config, transport)
        assert calls == ["/oauth2/token"]
        db.refresh(row)
        assert aware(row.token_expires_at) > now() + timedelta(hours=23)
        assert row.token_ciphertext != "new-synthetic-token"
        assert not refresh_automatic_namuh(db, user, config, transport)
        assert len(calls) == 1
        assert db.scalar(select(BrokerProbe)).status == "completed"


def test_kiwoom_force_refresh_preserves_valid_token_on_failure(setup):
    config, factory = setup
    config = config.model_copy(update={
        "kiwoom_owner_email": "admin@example.com",
        "kiwoom_app_key": SecretStr("synthetic-key"),
        "kiwoom_app_secret": SecretStr("synthetic-secret"),
        "broker_encryption_key": SecretStr(Fernet.generate_key().decode()),
    })
    reader = KiwoomReader(config, httpx.MockTransport(lambda request: httpx.Response(
        200, json={"return_code": 3, "return_msg": "synthetic failure"}
    )))
    with factory() as db:
        user = db.scalar(select(User))
        row = KiwoomConnection(
            owner_id=user.id, tenant_id=user.tenant_id, enabled=True, environment="live",
            token_scope=reader.scope("live"),
            token_ciphertext=reader.cipher().encrypt(b"old-synthetic-token").decode(),
            token_expires_at=now() + timedelta(minutes=5),
        )
        db.add(row)
        db.commit()
        from fastapi import HTTPException

        try:
            reader.token(db, user, force=True)
        except HTTPException as error:
            assert error.status_code == 502
        else:
            raise AssertionError("forced refresh must report failure")
        db.refresh(row)
        assert row.token_ciphertext is not None
        assert aware(row.token_expires_at) > now()


def test_namuh_discards_refresh_when_connection_changes_during_request(setup):
    config, factory = setup
    config = config.model_copy(update={
        "namuh_owner_email": "admin@example.com",
        "namuh_app_key": SecretStr("synthetic-key"),
        "namuh_app_secret": SecretStr("synthetic-secret"),
        "broker_encryption_key": SecretStr(Fernet.generate_key().decode()),
    })

    def respond(request):
        with factory.begin() as other:
            row = other.scalar(select(BrokerConnection))
            row.enabled = False
            row.version += 1
        return httpx.Response(200, json={"access_token": "discarded-token", "expires_in": 86400})

    with factory() as db:
        user = db.scalar(select(User))
        db.add(BrokerConnection(
            owner_id=user.id, tenant_id=user.tenant_id, enabled=True, environment="live",
            next_probe_at=now() - timedelta(seconds=1),
        ))
        db.add(TradePlan(owner_id=user.id, tenant_id=user.tenant_id, broker="namuh",
                         code="034220", value={"active": True, "mode": "automatic"}))
        db.commit()
        assert not refresh_automatic_namuh(db, user, config, httpx.MockTransport(respond))
        db.expire_all()
        row = db.scalar(select(BrokerConnection))
        assert not row.enabled and row.token_ciphertext is None
        assert db.scalar(select(BrokerProbe)) is None


def test_worker_preemptively_refreshes_kiwoom_once_per_owner(setup):
    config, factory = setup
    config = config.model_copy(update={
        "kiwoom_owner_email": "admin@example.com",
        "kiwoom_app_key": SecretStr("synthetic-key"),
        "kiwoom_app_secret": SecretStr("synthetic-secret"),
        "broker_encryption_key": SecretStr(Fernet.generate_key().decode()),
    })
    calls = []

    def respond(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={
            "return_code": 0, "token": "renewed-synthetic-token",
            "expires_dt": (now() + timedelta(hours=3)).astimezone(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d%H%M%S"),
        })

    gateway = TradeGateway(config, kiwoom_transport=httpx.MockTransport(respond))
    with factory() as db:
        user = db.scalar(select(User))
        reader = KiwoomReader(config)
        db.add(KiwoomConnection(
            owner_id=user.id, tenant_id=user.tenant_id, enabled=True, environment="live",
            token_scope=reader.scope("live"),
            token_ciphertext=reader.cipher().encrypt(b"old-synthetic-token").decode(),
            token_expires_at=now() + timedelta(minutes=5),
        ))
        for code in ("034220", "005930"):
            db.add(TradePlan(owner_id=user.id, tenant_id=user.tenant_id, broker="kiwoom",
                             code=code, value={"active": True, "mode": "automatic"}))
        db.commit()
        engine = db.get_bind()
    worker = LiveWorker(config, engine, factory, gateway)
    worker.evaluate = lambda *args: None  # This test exercises credential renewal, never orders.
    worker.tick()
    assert calls == ["/oauth2/token"]
    with factory() as db:
        row = db.scalar(select(KiwoomConnection))
        assert aware(row.token_expires_at) > now() + timedelta(hours=2)
