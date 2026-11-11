"""Local settings. Loading settings never authenticates with Garmin."""

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
    )

    timezone: str = "Asia/Shanghai"
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    data_dir: Path = Path("data")
    web_auth_file: Path | None = None
    web_allowed_hosts: list[str] = ["localhost", "127.0.0.1", "[::1]"]
    environment_ingest_enabled: bool = False
    debug_api_docs: bool = False
    demo: bool = False
    garmin_base_url: Literal[
        "https://personal.connectapi.garmin.cn",
        "https://personal.connectapi.garmin.com",
    ] = Field(
        default="https://personal.connectapi.garmin.cn",
        validation_alias="GARMIN_PERSONAL_API_BASE_URL",
    )
    garmin_key_file: Path = Path(".secrets/garmin-personal-api-key")
    garmin_token_cache: Path = Path(".secrets/garmin-personal-api-bearer-token.json")

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
