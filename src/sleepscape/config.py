"""Local settings. Loading settings never authenticates with Garmin."""

from datetime import date
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SLEEPSCAPE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    timezone: str = "Asia/Shanghai"
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    data_dir: Path = Path("data")
    web_auth_file: Path | None = None
    web_allowed_hosts: list[str] = ["localhost", "127.0.0.1", "[::1]"]
    environment_ingest_enabled: bool = False
    debug_api_docs: bool = False
    dashboard_start_date: date | None = None
    demo: bool = False
    llm_enabled: bool = False
    llm_automatic_enabled: bool = False
    llm_model_ref: str = "sleep_analyst"
    llm_base_url: str | None = Field(default=None, repr=False)
    llm_endpoint: str | None = Field(default=None, repr=False)
    llm_key_file: Path = Field(default=Path(".secrets/sleep-llm-key"), repr=False)
    llm_config_file: Path = Field(default=Path("configs/models.toml"), repr=False)
    garmin_base_url: Literal[
        "https://personal.connectapi.garmin.cn",
        "https://personal.connectapi.garmin.com",
    ] = Field(
        default="https://personal.connectapi.garmin.cn",
        validation_alias="GARMIN_PERSONAL_API_BASE_URL",
    )
    garmin_key_file: Path = Path(".secrets/garmin-personal-api-key")
    garmin_token_cache: Path = Path(".secrets/garmin-personal-api-bearer-token.json")
    garmin_lookback_days: int = Field(default=7, ge=1, le=31)
    maintenance_keep_count: int = Field(default=3, ge=1, le=100)
    maintenance_max_bytes: int = Field(default=4 * 1024**3, ge=1)
    maintenance_export_mode: Literal["disabled", "path", "ssh"] = "disabled"
    maintenance_export_path: Path | None = Field(default=None, repr=False)
    maintenance_export_host: str | None = Field(default=None, repr=False)
    maintenance_export_user: str | None = Field(default=None, repr=False)
    maintenance_export_port: int = Field(default=22, ge=1, le=65535)
    maintenance_export_identity_file: Path | None = Field(default=None, repr=False)
    maintenance_export_known_hosts_file: Path | None = Field(default=None, repr=False)
    maintenance_export_timeout_seconds: int = Field(default=300, ge=5, le=1800)
    maintenance_export_max_bytes: int = Field(default=16 * 1024**3, ge=1)
    maintenance_export_daily_keep: int = Field(default=7, ge=1, le=31)
    maintenance_export_weekly_keep: int = Field(default=4, ge=0, le=52)

    @field_validator("dashboard_start_date", mode="before")
    @classmethod
    def validate_dashboard_start_date(cls, value):
        if value is None or value == "":
            return None
        if isinstance(value, str):
            try:
                parsed = date.fromisoformat(value)
            except ValueError as exc:
                raise ValueError("Use a dashboard wake-up date in YYYY-MM-DD format") from exc
            if parsed.isoformat() != value:
                raise ValueError("Use a dashboard wake-up date in YYYY-MM-DD format")
            value = parsed
        if type(value) is not date or not date(2000, 1, 1) <= value <= date(2100, 12, 31):
            raise ValueError("Dashboard start date must be between 2000-01-01 and 2100-12-31")
        return value

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("Use a valid IANA timezone, such as Asia/Shanghai") from exc
        return value

    @field_validator("web_allowed_hosts")
    @classmethod
    def validate_web_hosts(cls, values: list[str]) -> list[str]:
        if not values or any(not value or "*" in value or "/" in value for value in values):
            raise ValueError("Use explicit hostnames without wildcards, paths, or schemes")
        return values
