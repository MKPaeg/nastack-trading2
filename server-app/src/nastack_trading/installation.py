"""Explicit installation initialization; never reset existing users or settings."""

from cryptography.fernet import Fernet
from sqlalchemy import select

from .cli import migrate
from .config import Config
from .db import connect
from .models import User
from .security import issue_admin_code


def initialize(config):
    # Refuse an incomplete installation before modifying the database.
    Fernet(config.broker_encryption_key.get_secret_value().encode())
    migrate(config.database_url)
    engine, factory = connect(config.database_url)
    try:
        with factory() as db:
            exists = db.scalar(select(User.id).where(User.role == "admin"))
        if not exists:
            issue_admin_code(factory, config)
            print("Initial administrator registered. Login requires configured SMTP.")
        else:
            print("Existing administrator and personal settings preserved.")
    finally:
        engine.dispose()


if __name__ == "__main__":
    initialize(Config())
