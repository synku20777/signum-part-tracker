from __future__ import annotations

import re
from decimal import Decimal
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from irmscher_tracker.sources.ebay_client import EbayEnvironment


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

    ebay_enabled: bool = False
    ebay_environment: EbayEnvironment = EbayEnvironment.PRODUCTION
    ebay_client_id: str = ""
    ebay_client_secret: SecretStr = SecretStr("")
    ebay_marketplace_id: str = "EBAY_DE"
    ebay_api_timeout: float = 30.0
    ebay_max_results_per_query: int = 200
    ebay_deletion_endpoint_url: str = ""
    ebay_deletion_verification_token: SecretStr = SecretStr("")
    ebay_deletion_max_pending_hours: int = Field(default=24, ge=1, le=720)

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

    @model_validator(mode="after")
    def validate_ebay_deletion_callback(self) -> Settings:
        endpoint = self.ebay_deletion_endpoint_url
        token = self.ebay_deletion_verification_token.get_secret_value()
        if bool(endpoint) != bool(token):
            raise ValueError("eBay deletion endpoint URL and verification token are both required")
        if not endpoint:
            return self
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,80}", token):
            raise ValueError(
                "TRACKER_EBAY_DELETION_VERIFICATION_TOKEN must contain 32-80 "
                "letters, digits, underscores, or hyphens"
            )
        parsed = urlsplit(endpoint)
        if not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Invalid eBay deletion endpoint URL")
        if parsed.query or parsed.fragment:
            raise ValueError("eBay deletion endpoint URL cannot contain a query or fragment")
        if parsed.path != "/ebay/marketplace-account-deletion":
            raise ValueError("eBay deletion endpoint URL has an unexpected path")
        if self.ebay_environment is EbayEnvironment.PRODUCTION:
            if parsed.scheme != "https":
                raise ValueError("Production eBay deletion endpoint must use HTTPS")
            hostname = parsed.hostname.rstrip(".").lower()
            if hostname == "localhost" or hostname.endswith(".localhost"):
                raise ValueError("Production eBay deletion endpoint cannot use localhost")
            try:
                address = ip_address(hostname)
            except ValueError:
                pass
            else:
                if not address.is_global:
                    raise ValueError(
                        "Production eBay deletion endpoint cannot use a non-public IP address"
                    )
        elif parsed.scheme not in {"http", "https"}:
            raise ValueError("Sandbox eBay deletion endpoint must use HTTP or HTTPS")
        return self

    @property
    def ebay_deletion_callback_configured(self) -> bool:
        return bool(
            self.ebay_deletion_endpoint_url
            and self.ebay_deletion_verification_token.get_secret_value()
        )

    @property
    def ebay_deletion_callback_ready(self) -> bool:
        return bool(
            self.ebay_deletion_callback_configured
            and self.ebay_client_id
            and self.ebay_client_secret.get_secret_value()
        )

    @property
    def parts_config_path(self) -> Path:
        return self.config_directory / "parts.yaml"

    @property
    def sources_config_path(self) -> Path:
        return self.config_directory / "sources.yaml"

    @property
    def data_directory(self) -> Path:
        prefix = "sqlite+aiosqlite:///"
        if not self.database_url.startswith(prefix) or self.database_url.endswith(":memory:"):
            return Path("data").resolve()
        return Path(self.database_url.removeprefix(prefix)).resolve().parent


def get_settings() -> Settings:
    # Pydantic supplies this required field from TRACKER_API_TOKEN at runtime.
    return Settings()  # type: ignore[call-arg]
