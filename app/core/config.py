"""Application settings, read once from KX_* environment variables."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KX_", env_file=".env", extra="ignore")

    env: Literal["development", "test", "production"] = "development"
    secret_key: SecretStr = Field(min_length=32)
    # Fernet key(s), comma-separated; the first encrypts (ADR 0011).
    encryption_key: SecretStr

    # The application connects as kx_app. Migrations use a separate role (ADR 0007).
    database_url: str
    migrator_database_url: str | None = None
    db_pool_size: int = 10
    db_max_overflow: int = 10

    redis_url: str = "redis://localhost:6379/0"
    base_url: str = "http://localhost:8000"
    timezone: str = "Africa/Lagos"
    web_threads: int = 40
    log_level: str = "INFO"

    # Object storage (S3 API; MinIO in docker-compose). Only app/files uses these.
    s3_endpoint_url: str | None = "http://localhost:9000"
    s3_region: str = "us-east-1"
    s3_access_key: SecretStr = SecretStr("")
    s3_secret_key: SecretStr = SecretStr("")
    s3_quarantine_bucket: str = "kx-quarantine"
    s3_clean_bucket: str = "kx-clean"
    # Virus scanning (ADR 0016). "accept_all" is for development and tests only; the app
    # refuses to start with it in production.
    scanner: Literal["clamd", "accept_all"] = "clamd"
    clamd_host: str = "localhost"
    clamd_port: int = 3310

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def dev_routes_enabled(self) -> bool:
        return self.env in ("development", "test")

    @property
    def secure_cookies(self) -> bool:
        return self.base_url.startswith("https://")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # required values come from the environment
