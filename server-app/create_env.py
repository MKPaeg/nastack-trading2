"""Create fresh isolated server credentials without starting services."""

import base64
import getpass
import json
import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def create_env(path, email, smtp):
    if path.exists():
        return False
    password = secrets.token_hex(32)
    ssl = smtp["TRADING_SMTP_PORT"] == "465"
    values = {
        "NEXT_POSTGRES_PASSWORD": password,
        "TRADING_ADMIN_EMAIL": email,
        "TRADING_BROKER_ENCRYPTION_KEY": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
        "TRADING_HTTP_PORT": "8082",
        "TRADING_TIMEZONE": "Asia/Seoul",
        "TRADING_COOKIE_SECURE": "false",
        "TRADING_ALLOWED_ORIGINS": json.dumps(["http://127.0.0.1:8082", "http://localhost:8082"]),
        "TRADING_RESEARCH_ENABLED": "false",
        "TRADING_NEWS_ENABLED": "false",
        "TRADING_LIVE_ORDERS_ENABLED": "false",
        "TRADING_AUTOMATIC_ORDERS_ENABLED": "false",
        "TRADING_SMTP_USE_SSL": str(ssl).lower(),
        "TRADING_SMTP_USE_TLS": str(not ssl).lower(),
        **smtp,
    }
    if any(any(char in value for char in "\r\n'") for value in values.values()):
        raise ValueError("Configuration must not contain newlines or single quotes.")
    with open(path, "x", encoding="utf-8", opener=lambda p, f: os.open(p, f, 0o600)) as stream:
        stream.write("# Isolated development server credentials; never commit.\n")
        stream.writelines(f"{key}='{value}'\n" for key, value in values.items())
    return True


def main():
    email = input("Development administrator email: ").strip().lower()
    if "@" not in email or email.endswith("@example.com"):
        raise SystemExit("Enter an administrator email for the isolated server.")
    smtp = {
        "TRADING_SMTP_HOST": input("SMTP host: ").strip(),
        "TRADING_SMTP_PORT": input("SMTP port [587]: ").strip() or "587",
        "TRADING_SMTP_USERNAME": input("SMTP username: ").strip(),
        "TRADING_SMTP_PASSWORD": getpass.getpass("SMTP app password: "),
        "TRADING_SMTP_FROM_EMAIL": input("SMTP sender email: ").strip(),
    }
    if not smtp["TRADING_SMTP_HOST"] or not smtp["TRADING_SMTP_PASSWORD"]:
        raise SystemExit("SMTP host and password are required.")
    if create_env(ROOT / ".env.next", email, smtp):
        print("Created isolated .env.next. No service was started.")
    else:
        print("Existing .env.next preserved.")


if __name__ == "__main__":
    main()
