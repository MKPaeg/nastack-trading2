import importlib.util
import plistlib
from pathlib import Path
from types import SimpleNamespace

import pytest


def module(name):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).resolve().parents[1] / "scripts" / f"{name}.py"
    )
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def test_launcher_build_is_local_and_preserves_shell_quoting(tmp_path, monkeypatch):
    builder = module("build_macos_launcher")
    root = tmp_path / "project with spaces"
    icons = root / "assets/macos"
    icons.mkdir(parents=True)
    (icons / "NaStackTrading.icns").write_bytes(b"synthetic-icon")
    monkeypatch.setattr(builder, "ROOT", root)
    builder.build()
    contents = root / "dist/NaStack Trading.app/Contents"
    info = plistlib.loads((contents / "Info.plist").read_bytes())
    executable = contents / "MacOS" / info["CFBundleExecutable"]
    assert executable.stat().st_mode & 0o111
    assert "start --open" in executable.read_text()
    assert "restart" not in executable.read_text()
    assert (contents / "Resources" / info["CFBundleIconFile"]).read_bytes() == b"synthetic-icon"


def test_existing_server_start_does_not_restart(monkeypatch):
    runtime = module("runtime")
    monkeypatch.setattr(runtime, "listener", lambda: 123)
    monkeypatch.setattr(runtime, "healthy", lambda: True)
    monkeypatch.setattr(runtime.os, "kill", lambda *args: pytest.fail("must not stop existing server"))
    monkeypatch.setattr(
        runtime.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("no duplicate server")
    )
    runtime.run("start")


def test_other_port_owner_is_never_stopped(monkeypatch):
    runtime = module("runtime")
    monkeypatch.setattr(runtime.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout="123\n"))
    monkeypatch.setattr(runtime.subprocess, "check_output", lambda *args, **kwargs: "another-program")
    with pytest.raises(RuntimeError, match="다른 프로그램"):
        runtime.listener()


def test_migration_failure_prevents_server_and_worker_start(monkeypatch):
    runtime = module("runtime")
    monkeypatch.setattr(runtime, "listener", lambda: None)
    monkeypatch.setattr(runtime, "ensure_database", lambda: None)
    commands = []

    def fail_migration(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(runtime.subprocess, "run", fail_migration)
    monkeypatch.setattr(
        runtime.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("migration failed: no server")
    )
    with pytest.raises(RuntimeError, match="migration"):
        runtime.run("start")
    assert commands == [[str(runtime.ROOT / ".venv/bin/python"), "-m", "alembic", "upgrade", "head"]]
