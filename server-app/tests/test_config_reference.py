from nastack_trading.config import ROOT, Config


def test_standalone_env_path_and_legacy_variable_aliases(tmp_path, monkeypatch):
    # Only synthetic credentials; never load real project secrets in this test.
    keys = [
        "TRADING_OPENAI_API_KEY",
        "OPENAI_API_KEY",
        "TRADING_SMTP_HOST",
        "SMTP_HOST",
        "TRADING_VLLM_BASE_URL",
        "VLLM_BASE_URL_01",
    ]
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    reference = tmp_path / "reference.env"
    reference.write_text(
        "SMTP_HOST=smtp.test\nSMTP_FROM_EMAIL=sender@example.com\n"
        "SMTP_PASSWORD=synthetic\nVLLM_BASE_URL_01=http://test:8000/v1\n"
        "VLLM_MODELS_01=test-model\nOPENAI_API_KEY=synthetic\n"
    )
    config = Config(_env_file=reference)
    assert config.smtp_host == "smtp.test"
    assert config.smtp_password.get_secret_value() == "synthetic"
    assert config.vllm_model_id == "test-model"
    assert config.openai_api_key.get_secret_value() == "synthetic"
    monkeypatch.setenv("TRADING_SMTP_HOST", "override.test")
    assert Config(_env_file=reference).smtp_host == "override.test"
    assert Config.model_config["env_file"] == ROOT / ".env"
