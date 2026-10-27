from alembic import context
from sqlalchemy import engine_from_config, pool

from nastack_trading.config import Config
from nastack_trading.models import Base

config = context.config
target_metadata = Base.metadata


def migrate():
    with context.begin_transaction():
        context.run_migrations()


url = config.attributes.get("database_url") or Config().database_url
if context.is_offline_mode():
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    migrate()
else:
    options = config.get_section(config.config_ini_section) or {}
    options["sqlalchemy.url"] = url
    engine = engine_from_config(options, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        migrate()
    engine.dispose()
