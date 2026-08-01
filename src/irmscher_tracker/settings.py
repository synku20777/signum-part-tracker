from decimal import Decimal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Application settings for the Irmscher Parts Tracker.
    Configuration is loaded from environment variables and the .env file.
    """
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="TRACKER_",
    )

    # API / Core
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_token: str = ""
    log_level: str = "INFO"
    timezone: str = "Europe/Riga"

    # eBay
    ebay_enabled: bool = True
    ebay_client_id: str = ""
    ebay_client_secret: str = ""
    ebay_marketplace_id: str = "EBAY_DE"
    ebay_api_timeout: float = 30.0
    ebay_max_results_per_query: int = 200

    # Telegram
    telegram_enabled: bool = True
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Database
    database_url: str = "sqlite+aiosqlite:////app/data/tracker.db"

    # Matching
    minimum_match_score: int = 65
    price_drop_percent: Decimal = Decimal("10.0")

    # Lifecycle
    max_consecutive_misses: int = 3

    # Scheduler
    scan_on_startup: bool = True
    search_interval_minutes: int = 30

    # Config Directory
    config_directory: str = "/app/config"

    @property
    def parts_config_path(self) -> str:
        import os
        return os.path.join(self.config_directory, "parts.yaml")

    @property
    def searches_config_path(self) -> str:
        import os
        return os.path.join(self.config_directory, "searches.yaml")


def get_settings() -> Settings:
    """
    Get the application settings instance.
    
    Returns:
        Settings: The application settings.
    """
    return Settings()
