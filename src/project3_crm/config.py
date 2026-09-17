"""Centralized environment-based application configuration."""

from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


POSTGRESQL_SCHEMES = frozenset(
    {
        "postgres",
        "postgresql",
        "postgresql+psycopg",
    }
)


class DatabaseSettings(BaseSettings):
    """Database-only configuration used by the application and Alembic."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        case_sensitive=False,
        extra="ignore",
        str_strip_whitespace=True,
        validate_default=True,
    )

    database_url: SecretStr = Field(min_length=1)

    @field_validator("database_url")
    @classmethod
    def validate_postgresql_database_url(cls, value: SecretStr) -> SecretStr:
        """Reject non-PostgreSQL URLs without revealing their contents."""

        raw_url = value.get_secret_value()
        scheme, separator, remainder = raw_url.partition("://")
        if (
            separator != "://"
            or scheme.lower() not in POSTGRESQL_SCHEMES
            or not remainder
        ):
            raise ValueError("DATABASE_URL must use a PostgreSQL-compatible scheme")
        return value

    @property
    def sqlalchemy_database_url(self) -> str:
        """Explicitly reveal the URL and select psycopg 3 for SQLAlchemy."""

        raw_url = self.database_url.get_secret_value()
        scheme, _, remainder = raw_url.partition("://")
        if scheme.lower() in {"postgres", "postgresql"}:
            return f"postgresql+psycopg://{remainder}"
        return raw_url


class Settings(DatabaseSettings):
    """Configuration shared by the future web and worker processes."""

    max_webhook_body_bytes: int = Field(default=262_144, gt=0)
    website_hmac_secret: SecretStr
    website_hmac_max_skew_seconds: int = Field(default=300, ge=0)
    linkedin_bearer_token: SecretStr
    partner_api_key: SecretStr

    hubspot_service_key: SecretStr
    hubspot_api_base_url: str = Field(default="https://api.hubapi.com", min_length=1)
    hubspot_api_version: str = Field(default="2026-03", pattern=r"^\d{4}-\d{2}$")
    hubspot_http_timeout_seconds: float = Field(default=30, gt=0)

    worker_lease_seconds: int = Field(default=900, gt=0)
    worker_poll_interval_seconds: float = Field(default=1, gt=0)
    worker_error_backoff_seconds: float = Field(default=5, gt=0)
    crm_retry_delay_1_seconds: int = Field(default=300, gt=0)
    crm_retry_delay_2_seconds: int = Field(default=1_800, gt=0)
    crm_retry_delay_3_seconds: int = Field(default=7_200, gt=0)
    crm_reconcile_initial_delay_seconds: int = Field(default=15, gt=0)
    crm_reconcile_not_found_delay_2_seconds: int = Field(default=60, gt=0)
    crm_reconcile_not_found_delay_3_seconds: int = Field(default=300, gt=0)
    crm_reconcile_error_delay_1_seconds: int = Field(default=300, gt=0)
    crm_reconcile_error_delay_2_seconds: int = Field(default=1_800, gt=0)

    cluster_failure_threshold: int = Field(default=5, gt=0)
    cluster_failure_window_seconds: int = Field(default=600, gt=0)
    notification_batch_interval_seconds: int = Field(default=7_200, gt=0)
    notification_batch_max_items: int = Field(default=100, gt=0)
    notification_failure_retry_seconds: int = Field(default=300, gt=0)

    smtp_host: str = Field(min_length=1)
    smtp_port: int = Field(default=587, ge=1, le=65_535)
    smtp_timeout_seconds: float = Field(default=30, gt=0)
    smtp_username: SecretStr
    smtp_password: SecretStr
    smtp_from_email: str = Field(min_length=1)
    smtp_starttls: bool = True
    ops_email_to: str = Field(min_length=1)
    automation_email_to: str = Field(min_length=1)



@lru_cache
def get_database_settings() -> DatabaseSettings:
    """Load only the validated database configuration needed by Alembic."""

    return DatabaseSettings()


@lru_cache
def get_settings() -> Settings:
    """Load and cache validated environment configuration."""

    return Settings()
