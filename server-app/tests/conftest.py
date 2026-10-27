import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from nastack_trading.app import create_app
from nastack_trading.cli import migrate
from nastack_trading.config import Config
from nastack_trading.db import connect
from nastack_trading.security import bootstrap

EMAIL = "admin@example.com"
CODE = "test-admin-code-only-123456"
ORIGIN = "http://testserver"


@pytest.fixture
def setup(tmp_path):
    url = os.environ.get("TRADING_TEST_DATABASE_URL")
    maintenance = None
    if url:
        if not (make_url(url).database or "").startswith("nastack_test"):
            raise ValueError("Integration tests require an isolated nastack_test* database")
        maintenance = create_engine(url)
        schema = "test_" + uuid4().hex
        with maintenance.begin() as db:
            db.execute(text(f'CREATE SCHEMA "{schema}"'))
        parsed = make_url(url)
        url = parsed.update_query_dict({"options": f"-csearch_path={schema}"}).render_as_string(
            hide_password=False
        )
    else:
        url = f"sqlite:///{tmp_path / 'test.db'}"
    config = Config(
        database_url=url,
        admin_email=EMAIL,
        admin_access_code=CODE,
        cookie_secure=False,
        allowed_origins=[ORIGIN],
        legacy_access_code_login_enabled=True,
        _env_file=None,
    )
    migrate(url)
    engine, factory = connect(url)
    bootstrap(factory, config)
    yield config, factory
    engine.dispose()
    if maintenance:
        with maintenance.begin() as db:
            db.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        maintenance.dispose()


@pytest.fixture
def client(setup):
    with TestClient(create_app(setup[0]), headers={"origin": ORIGIN}) as client:
        yield client


@pytest.fixture
def admin(client):
    assert client.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE}).status_code == 200
    return client


def enqueue(client, key="request-key-one", symbol="005930"):
    response = client.post("/api/runs", json={"symbol": symbol, "request_key": key})
    assert response.status_code == 202, response.text
    return response.json()
