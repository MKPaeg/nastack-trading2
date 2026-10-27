from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import AliasChoices, EmailStr, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Config(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TRADING_",
        env_file=ROOT / ".env",
        extra="ignore",
        populate_by_name=True,
    )

    database_url: str = "postgresql+psycopg://nastack@127.0.0.1:54329/nastack_trading"
    live_orders_enabled: bool = False
    live_order_scope: dict[str, str | int] = Field(default_factory=dict)
    live_order_scope_started_at: datetime | None = None
    automatic_orders_enabled: bool = False
    live_worker_enabled: bool = True
    live_worker_poll_seconds: int = Field(default=5, ge=1, le=60)
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_chat_id: str = ""
    telegram_owner_email: str = ""
    admin_email: EmailStr = "admin@example.com"
    admin_access_code: SecretStr = SecretStr("")
    timezone: str = "Asia/Seoul"
    cookie_secure: bool = True
    development_lan_same_origin: bool = False
    allowed_origins: list[str] = ["http://127.0.0.1:8080", "http://localhost:8080"]
    worker_lease_seconds: int = Field(ge=10, le=600, default=30)
    research_enabled: bool = False
    news_enabled: bool = False
    namuh_owner_email: str = ""
    kiwoom_owner_email: str = ""
    kiwoom_app_key: SecretStr = SecretStr("")
    kiwoom_app_secret: SecretStr = SecretStr("")
    namuh_app_key: SecretStr = Field(
        default=SecretStr(""), validation_alias=AliasChoices("TRADING_NAMUH_APP_KEY", "NHPLUG_APP_KEY")
    )
    namuh_app_secret: SecretStr = Field(
        default=SecretStr(""), validation_alias=AliasChoices("TRADING_NAMUH_APP_SECRET", "NHPLUG_APP_SECRET")
    )
    broker_encryption_key: SecretStr = SecretStr("")
    vllm_base_url: str = Field(
        default="",
        validation_alias=AliasChoices("TRADING_VLLM_BASE_URL", "VLLM_BASE_URL_01", "VLLM_BASE_URL"),
    )
    vllm_model_id: str = Field(
        default="", validation_alias=AliasChoices("TRADING_VLLM_MODEL_ID", "VLLM_MODELS_01", "VLLM_MODELS")
    )
    vllm_api_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("TRADING_VLLM_API_KEY", "VLLM_API_KEY_01", "VLLM_API_KEY"),
    )
    tavily_api_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices(
            "TRADING_TAVILY_API_KEY", "TAVILY_API_KEY", "NASTACK_WEB_SEARCH_TAVILY_API_KEY"
        ),
    )
    openai_api_key: SecretStr = Field(
        default=SecretStr(""), validation_alias=AliasChoices("TRADING_OPENAI_API_KEY", "OPENAI_API_KEY")
    )
    smtp_host: str = Field(default="", validation_alias=AliasChoices("TRADING_SMTP_HOST", "SMTP_HOST"))
    smtp_port: int = Field(
        default=587, ge=1, le=65535, validation_alias=AliasChoices("TRADING_SMTP_PORT", "SMTP_PORT")
    )
    smtp_username: str = Field(
        default="", validation_alias=AliasChoices("TRADING_SMTP_USERNAME", "SMTP_USERNAME")
    )
    smtp_password: SecretStr = Field(
        default=SecretStr(""), validation_alias=AliasChoices("TRADING_SMTP_PASSWORD", "SMTP_PASSWORD")
    )
    smtp_from_email: str = Field(
        default="", validation_alias=AliasChoices("TRADING_SMTP_FROM_EMAIL", "SMTP_FROM_EMAIL")
    )
    smtp_use_tls: bool = Field(
        default=True, validation_alias=AliasChoices("TRADING_SMTP_USE_TLS", "SMTP_USE_TLS")
    )
    smtp_use_ssl: bool = False
    legacy_access_code_login_enabled: bool = False
    cloud_model_allowlist: list[str] = ["gpt-5.6-luna"]

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        ZoneInfo(value)
        return value
