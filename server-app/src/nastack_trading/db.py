from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker


def now() -> datetime:
    return datetime.now(timezone.utc)


def aware(value: datetime) -> datetime:
    # SQLite drops timezone info; PostgreSQL timestamptz preserves it.
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def local_time(value: datetime | None, timezone_name: str) -> str | None:
    return aware(value).astimezone(ZoneInfo(timezone_name)).isoformat() if value else None


def connect(url: str):
    options = {"check_same_thread": False, "timeout": 10} if url.startswith("sqlite") else {}
    engine = create_engine(url, pool_pre_ping=True, connect_args=options)
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def enable_foreign_keys(connection, record):
            connection.execute("PRAGMA foreign_keys=ON")

    return engine, sessionmaker(engine, expire_on_commit=False)
