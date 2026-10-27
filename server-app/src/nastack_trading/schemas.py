from ipaddress import ip_address
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelProfile(StrictModel):
    provider: Literal["vllm", "openai"] = "vllm"
    model_id: str = Field(default="", max_length=120)
    base_url: str = Field(default="", max_length=500)

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        if not value:
            return value
        parsed = urlparse(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("HTTP(S) 주소만 입력하세요. 키/계정정보를 URL에 넣지 마세요.")
        if parsed.query or parsed.fragment:
            raise ValueError("연결 주소에는 query/fragment를 사용할 수 없습니다.")
        return value.rstrip("/")


class SourceRule(StrictModel):
    domain: str = Field(min_length=1, max_length=253, pattern=r"^[a-zA-Z0-9.-]+$")
    disposition: Literal["use", "reference", "exclude"] = "reference"

    @field_validator("domain")
    @classmethod
    def normalize_domain(cls, value: str) -> str:
        value = value.lower().strip(".")
        if "." not in value or any(
            not part or part.startswith("-") or part.endswith("-") for part in value.split(".")
        ):
            raise ValueError("도메인을 입력하세요.")
        return value


class CatalogSource(SourceRule):
    name: str = Field(min_length=1, max_length=120)
    url: str = Field(min_length=1, max_length=500)
    region: Literal["KR", "US", "GLOBAL"] = "KR"
    category: str = Field(min_length=1, max_length=120)
    method: Literal["api", "file", "rss", "web"] = "web"
    access_notes: str = Field(default="", max_length=1000)
    status: Literal["candidate", "active", "retired"] = "candidate"

    @model_validator(mode="after")
    def public_source(self):
        parsed = urlparse(self.url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.port not in {None, 80, 443}
        ):
            raise ValueError("계정정보/query 없는 공개 HTTP(S) 출처 주소를 입력하세요.")
        if parsed.hostname != self.domain and not parsed.hostname.endswith("." + self.domain):
            raise ValueError("출처 주소와 도메인이 일치해야 합니다.")
        if self.domain.endswith((".local", ".localhost", ".internal")):
            raise ValueError("사설 도메인을 사용할 수 없습니다.")
        try:
            address = ip_address(self.domain)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError("사설 IP를 사용할 수 없습니다.")
        return self


class CatalogUpdate(CatalogSource):
    version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=200)


class Preferences(StrictModel):
    theme: Literal["midnight", "slate"] = "midnight"
    page_size: Literal[5, 10] = 5
    analysis_model: ModelProfile = Field(default_factory=ModelProfile)
    news_model: str = Field(default="default", max_length=200)
    default_attributes_enabled: bool = True
    default_attributes_cloud_enabled: bool = False
    cloud_enabled: bool = False
    cloud_model: str = Field(default="gpt-5.6-luna", min_length=1, max_length=120)
    timeout_seconds: int = Field(default=90, ge=5, le=300)
    sources: list[SourceRule] = Field(default_factory=list, max_length=100)

    @field_validator("sources")
    @classmethod
    def unique_sources(cls, value: list[SourceRule]) -> list[SourceRule]:
        if len({rule.domain for rule in value}) != len(value):
            raise ValueError("도메인 중복을 제거하세요.")
        return value


class SettingsUpdate(StrictModel):
    version: int = Field(ge=1)
    value: Preferences


class SchedulePlan(StrictModel):
    """Cadence proposals only; no task is executed from this policy yet."""

    holdings_minutes: int = Field(default=5, ge=5, le=1440)
    holdings_market_hours_only: bool = True
    analysis_minutes: int = Field(default=60, ge=15, le=10080)
    news_minutes: int = Field(default=15, ge=5, le=1440)
    instrument_master_minutes: int = Field(default=1440, ge=60, le=10080)


class SchedulePlanUpdate(StrictModel):
    version: int = Field(ge=1)
    value: SchedulePlan


class Login(StrictModel):
    email: EmailStr
    access_code: str = Field(min_length=1, max_length=128)
    remember_email: bool = False


class CodeRequest(StrictModel):
    email: EmailStr


class AdminPolicyUpdate(StrictModel):
    version: int = Field(ge=1)
    default_session_hours: int = Field(ge=1, le=168)
    max_session_hours: int = Field(ge=1, le=168)
    code_ttl_minutes: int = Field(default=10, ge=1, le=30)
    code_max_attempts: int = Field(default=5, ge=1, le=10)

    @model_validator(mode="after")
    def valid_range(self):
        if self.default_session_hours > self.max_session_hours:
            raise ValueError("기본 세션 시간은 최대 시간 이하여야 합니다.")
        return self


class UserCreate(StrictModel):
    email: EmailStr
    role: Literal["trader", "viewer"] = "viewer"
    session_hours: int | None = Field(default=None, ge=1, le=168)


class UserUpdate(StrictModel):
    role: Literal["admin", "trader", "viewer"]
    active: bool
    session_hours: int | None = Field(default=None, ge=1, le=168)


class RunCreate(StrictModel):
    symbol: str = Field(default="005930", pattern=r"^\d{6}$")
    request_key: str = Field(min_length=8, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")


class InstrumentFields(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    market: Literal["KOSPI", "KOSDAQ", "KONEX"]
    asset_type: Literal["stock", "etf"] = "stock"
    status: Literal["active", "inactive"] = "active"
    source_note: str = Field(min_length=1, max_length=500)
    business_description: str = Field(default="", max_length=4000)
    business_source_url: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def business_source(self):
        if bool(self.business_description.strip()) != bool(self.business_source_url.strip()):
            raise ValueError("사업 설명과 공개 출처 URL을 함께 입력하세요.")
        if self.business_source_url:
            from .research import public_url

            if not public_url(self.business_source_url):
                raise ValueError("사업 설명 출처는 공개 HTTP(S) URL이어야 합니다.")
        return self

    @field_validator("name", "source_note")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("내용을 입력하세요.")
        return value.strip()


class InstrumentCreate(InstrumentFields):
    code: str = Field(pattern=r"^\d{6}$")


class InstrumentUpdate(InstrumentFields):
    version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=200)


class WatchlistCreate(StrictModel):
    instrument_id: str = Field(min_length=36, max_length=36)
