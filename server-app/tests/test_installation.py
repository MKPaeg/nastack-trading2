import importlib.util
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from nastack_trading.config import Config
from nastack_trading.db import connect
from nastack_trading.installation import initialize
from nastack_trading.models import User


def installer():
    spec = importlib.util.spec_from_file_location(
        "installer", Path(__file__).parents[1] / "scripts/install.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_env_creation_preserves_existing_and_generates_unique_secrets(tmp_path):
    module = installer()
    first, second = tmp_path / "one.env", tmp_path / "two.env"
    smtp = {"TRADING_SMTP_PASSWORD": "literal$dollar"}
    assert module.create_env(first, "owner@test.com", 8082, smtp)
    config = Config(_env_file=first)
    Fernet(config.broker_encryption_key.get_secret_value())
    original = first.read_bytes()
    assert not module.create_env(first, "other@test.com", 9090, {})
    assert first.read_bytes() == original
    module.create_env(second, "owner@test.com", 8082, smtp)
    assert first.read_bytes() != second.read_bytes()
    assert "TRADING_LIVE_ORDERS_ENABLED='false'" in first.read_text()


def test_initialization_is_repeatable_without_resetting_admin(tmp_path):
    config = Config(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'install.db'}",
        admin_email="a@test.com",
        broker_encryption_key=Fernet.generate_key().decode(),
    )
    initialize(config)
    engine, factory = connect(config.database_url)
    with factory.begin() as db:
        admin = db.scalar(select(User).where(User.role == "admin"))
        admin.active = False
        identity = admin.id
    initialize(config.model_copy(update={"admin_email": "b@test.com"}))
    with factory() as db:
        users = list(db.scalars(select(User)))
        assert len(users) == 1
        assert users[0].id == identity
        assert users[0].email == "a@test.com"
        assert users[0].role == "admin"
        assert not users[0].active
    engine.dispose()


def test_invalid_env_is_not_partially_written(tmp_path):
    path = tmp_path / "invalid.env"
    with pytest.raises(ValueError):
        installer().create_env(path, "a@test.com", 8080, {"TRADING_SMTP_PASSWORD": "bad\nvalue"})
    assert not path.exists()


def test_invalid_encryption_key_does_not_create_database(tmp_path):
    path = tmp_path / "missing.db"
    config = Config(_env_file=None, database_url=f"sqlite:///{path}", broker_encryption_key="")
    with pytest.raises(ValueError):
        initialize(config)
    assert not path.exists()
