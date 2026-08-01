from decimal import Decimal
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from ``TRACKER_*`` variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="TRACKER_",
    )

    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_token: SecretStr
    log_level: str = "INFO"

    ebay_enabled: bool = True
    ebay_client_id: str = ""
    ebay_client_secret: SecretStr = SecretStr("")
    ebay_marketplace_id: str = "EBAY_DE"
    ebay_api_timeout: float = 30.0
    ebay_max_results_per_query: int = 200

    sscom_enabled: bool = False
    sscom_interval_minutes: int = Field(default=60, ge=5)
    sscom_request_timeout: float = Field(default=20.0, gt=0, le=120)
    sscom_max_detail_requests_per_run: int = Field(default=30, ge=0, le=100)
    sscom_detail_refresh_hours: int = Field(default=24, ge=1)

    telegram_enabled: bool = True
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_chat_id: str = ""

    database_url: str = "sqlite+aiosqlite:////app/data/tracker.db"
    minimum_match_score: int = 65
    price_drop_percent: Decimal = Decimal("10.0")
    max_consecutive_misses: int = 3
    scan_on_startup: bool = True
    search_interval_minutes: int = 30
    config_directory: Path = Path("/app/config")

    @field_validator("api_token")
    @classmethod
    def validate_api_token(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value().encode()) < 32:
            raise ValueError("TRACKER_API_TOKEN must contain at least 32 bytes")
        return value

    @property
    def parts_config_path(self) -> Path:
        return self.config_directory / "parts.yaml"

    @property
    def sources_config_path(self) -> Path:
        return self.config_directory / "sources.yaml"


def get_settings() -> Settings:
    # Pydantic supplies this required field from TRACKER_API_TOKEN at runtime.
    return Settings()  # type: ignore[call-arg]
