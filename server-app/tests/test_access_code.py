import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from nastack_trading.app import create_app
from nastack_trading.cli import migrate
from nastack_trading.config import Config
from nastack_trading.db import connect
from nastack_trading.models import AuditEvent, User, UserSettings
from nastack_trading.security import issue_admin_code, verify_code


def test_first_issue_without_env_code_and_login(tmp_path):
    config = Config(
        database_url=f"sqlite:///{tmp_path / 'fresh.db'}",
        admin_email="owner@example.com",
        admin_access_code="",
        legacy_access_code_login_enabled=True,
        cookie_secure=False,
        allowed_origins=["http://testserver"],
        _env_file=None,
    )
    migrate(config.database_url)
    engine, factory = connect(config.database_url)
    try:
        code = issue_admin_code(factory, config)
        assert len(code) >= 24
        with factory() as db:
            user = db.scalar(select(User))
            assert user.role == "admin" and verify_code(code, user.code_hash)
            assert db.get(UserSettings, user.id) is not None
            assert code not in str(db.scalar(select(AuditEvent)).detail)
            assert code not in user.code_hash
        with TestClient(create_app(config), headers={"origin": "http://testserver"}) as client:
            assert (
                client.post(
                    "/api/auth/login", json={"email": config.admin_email, "access_code": code}
                ).status_code
                == 200
            )
    finally:
        engine.dispose()


def test_issue_refuses_existing_admin_without_explicit_reissue(admin, setup):
    config, factory = setup
    with pytest.raises(ValueError, match="이미 관리자가"):
        issue_admin_code(factory, config)
    assert admin.get("/api/me").status_code == 200


def test_reissue_revokes_old_code_and_sessions(admin, setup):
    config, factory = setup
    code = issue_admin_code(factory, config, reissue=True)
    assert admin.get("/api/me").status_code == 401
    assert (
        admin.post(
            "/api/auth/login",
            json={"email": config.admin_email, "access_code": config.admin_access_code.get_secret_value()},
        ).status_code
        == 401
    )
    assert (
        admin.post("/api/auth/login", json={"email": config.admin_email, "access_code": code}).status_code
        == 200
    )
    with factory() as db:
        event = db.scalar(select(AuditEvent).where(AuditEvent.action == "admin.code_reissued"))
        assert event is not None and code not in str(event.detail)


def test_reissue_does_not_create_or_promote_account(admin, setup):
    config, factory = setup
    with pytest.raises(ValueError, match="활성 관리자"):
        issue_admin_code(
            factory, config.model_copy(update={"admin_email": "another@example.com"}), reissue=True
        )
    assert len(admin.get("/api/users").json()) == 1


def test_cli_requires_email_and_explicit_reset(monkeypatch, capsys, setup):
    import nastack_trading.cli as cli

    config, _ = setup
    monkeypatch.setattr(cli, "Config", lambda **kwargs: config)
    monkeypatch.setattr("sys.argv", ["cli", "issue-admin-code"])
    with pytest.raises(SystemExit):
        cli.main()
    assert "--email" in capsys.readouterr().err
    monkeypatch.setattr("sys.argv", ["cli", "issue-admin-code", "--email", "admin@example.com"])
    with pytest.raises(SystemExit):
        cli.main()
    captured = capsys.readouterr()
    assert "이미 관리자가" in captured.err and "이번에만 표시" not in captured.out
