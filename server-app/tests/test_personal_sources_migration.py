from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import select

from nastack_trading.catalog import catalog_changed
from nastack_trading.cli import migrate
from nastack_trading.config import ROOT, Config
from nastack_trading.db import connect
from nastack_trading.models import AnalysisRun, PersonalSource, SourceCatalog, User, UserSourceList
from nastack_trading.security import bootstrap


def test_migration_copies_existing_catalog_without_rewriting_history(tmp_path):
    url = f"sqlite:///{tmp_path / 'legacy.db'}"
    migration = AlembicConfig(str(ROOT / "alembic.ini"))
    migration.attributes["database_url"] = url
    command.upgrade(migration, "0007")
    config = Config(
        database_url=url,
        admin_email="migration@example.com",
        admin_access_code="migration-test-secret-only",
        _env_file=None,
    )
    engine, factory = connect(url)
    bootstrap(factory, config)
    with factory.begin() as db:
        user = db.scalar(select(User))
        source = SourceCatalog(
            tenant_id=user.tenant_id,
            domain="example.com",
            version=3,
            value={"name": "기존 목록", "status": "active"},
        )
        db.add(source)
        db.flush()
        snapshot = [{"id": source.id, "domain": "example.com", "version": 3, "name": "기존 목록"}]
        run = AnalysisRun(
            tenant_id=user.tenant_id,
            owner_id=user.id,
            request_key="legacy-run",
            symbol="005930",
            kind="research",
            settings_version=1,
            settings_snapshot={"sources": [{"domain": "example.com", "disposition": "use"}]},
            source_catalog_snapshot=snapshot,
        )
        db.add(run)
        db.flush()
        user_id, source_id, run_id = user.id, source.id, run.id
    migrate(url)
    with factory.begin() as db:
        personal = db.scalar(select(PersonalSource))
        assert personal.owner_id == user_id and personal.template_id == source_id
        assert personal.version == 3 and personal.id != source_id
        assert db.get(UserSourceList, user_id)
        record = db.get(AnalysisRun, run_id)
        assert record.source_catalog_snapshot == snapshot
        assert not catalog_changed(db, record)
        personal.version = 4
        db.flush()
        assert catalog_changed(db, record)
        assert db.get(SourceCatalog, source_id).version == 3
    engine.dispose()
