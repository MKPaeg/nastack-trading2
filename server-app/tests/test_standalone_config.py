from nastack_trading.config import ROOT, Config


def test_local_credentials_loaded_without_sibling_access(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("TRADING_OPENAI_API_KEY", raising=False)
    trading = tmp_path / "trading.env"
    trading.write_text(
        "TRADING_OPENAI_API_KEY=synthetic-local-test-key\nTRADING_NEWS_ENABLED=true\n", encoding="utf-8"
    )
    config = Config(_env_file=trading)
    assert config.openai_api_key.get_secret_value() == "synthetic-local-test-key"
    assert config.news_enabled is True
    assert "synthetic-local-test-key" not in repr(config)
    assert Config.model_config["env_file"] == ROOT / ".env"


def test_process_environment_takes_precedence(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("TRADING_OPENAI_API_KEY", raising=False)
    trading = tmp_path / "trading.env"
    trading.write_text("TRADING_OPENAI_API_KEY=synthetic-trading-test-key\n", encoding="utf-8")
    monkeypatch.setenv("TRADING_OPENAI_API_KEY", "synthetic-process-test-key")
    config = Config(_env_file=trading)
    assert config.openai_api_key.get_secret_value() == "synthetic-process-test-key"
