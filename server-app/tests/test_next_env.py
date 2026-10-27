import importlib.util
from pathlib import Path


def test_next_env_is_private_and_not_overwritten(tmp_path):
    module_path = Path(__file__).resolve().parents[1] / "create_env.py"
    spec = importlib.util.spec_from_file_location("next_create_env", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    path = tmp_path / ".env.next"
    smtp = {"TRADING_SMTP_PORT": "587", "TRADING_SMTP_PASSWORD": "literal$dollar"}
    assert module.create_env(path, "dev@test.com", smtp)
    original = path.read_bytes()
    assert path.stat().st_mode & 0o777 == 0o600
    assert b"NEXT_POSTGRES_PASSWORD='" in original
    assert b"TRADING_HTTP_PORT='8082'" in original
    assert b"TRADING_LIVE_ORDERS_ENABLED='false'" in original
    assert not module.create_env(path, "other@test.com", smtp)
    assert path.read_bytes() == original
