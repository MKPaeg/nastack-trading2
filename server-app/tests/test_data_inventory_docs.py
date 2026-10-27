"""Offline documentation contract; never connect to a user's DB or services."""
import importlib.util
import json
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def inventory():
    spec = importlib.util.spec_from_file_location("inventory_docs", ROOT / "scripts/update_data_inventory.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_inventory_complete_and_public(inventory):
    data = json.loads(inventory.CURRENT.read_text())
    from nastack_trading.dimension_catalog import DIMENSIONS
    from nastack_trading.statistics import SERIES
    from nastack_trading.news import FEEDS
    assert data["dimensions"] == DIMENSIONS
    assert data["statistics"] == SERIES
    assert set(data["feeds"]) == set(FEEDS)
    from nastack_trading.business_context import REVIEWED
    assert {row["code"] for row in data["reviewed_business_sources"]} == set(REVIEWED)
    assert len({r["id"] for r in data["dimensions"]}) == len(DIMENSIONS)
    for row in data["registered_candidates_historical"]:
        assert set(row) == {"name", "domain", "information", "evidence_date"}
    assert not any(x in data for x in ("owner_id", "tenant_id", "credentials", "personal_connections"))
    inventory.check()


@pytest.fixture
def isolated(inventory, tmp_path, monkeypatch):
    for attr in ("NOTE", "PDF", "CURRENT", "MANIFEST"):
        old = getattr(inventory, attr)
        target = tmp_path / old.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(old, target)
        monkeypatch.setattr(inventory, attr, target)
    manifest = json.loads(inventory.MANIFEST.read_text())
    for version in manifest:
        for path in version["files"]:
            target = tmp_path / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / path, target)
    hashes = json.loads(inventory.CURRENT.read_text())["source_hashes"]
    monkeypatch.setattr(inventory, "ROOT", tmp_path)
    monkeypatch.setattr(inventory, "source_hashes", lambda: hashes.copy())
    return inventory


def test_check_detects_source_drift(isolated, monkeypatch):
    monkeypatch.setattr(isolated, "source_hashes", lambda: {"modified": "hash"})
    with pytest.raises(SystemExit, match="소스 지문 변경"):
        isolated.check()


def test_check_detects_document_drift(isolated):
    isolated.NOTE.write_text(isolated.NOTE.read_text() + "\nchanged\n")
    with pytest.raises(SystemExit, match="정본 Markdown/PDF"):
        isolated.check()


def test_check_detects_history_tamper(isolated):
    entry = json.loads(isolated.MANIFEST.read_text())[0]
    path = isolated.ROOT / next(iter(entry["files"]))
    path.write_bytes(b"tampered")
    with pytest.raises(SystemExit, match="보존 이력 손상"):
        isolated.check()
