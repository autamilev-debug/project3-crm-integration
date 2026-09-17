import pytest
from pydantic import ValidationError

from project3_crm.config import DatabaseSettings, Settings


REQUIRED_TEST_ENV = {
    "DATABASE_URL": "postgresql://project3:test-password@localhost:5432/project3_test",
    "WEBSITE_HMAC_SECRET": "test-website-secret",
    "LINKEDIN_BEARER_TOKEN": "test-linkedin-token",
    "PARTNER_API_KEY": "test-partner-key",
    "HUBSPOT_SERVICE_KEY": "test-hubspot-key",
    "SMTP_HOST": "smtp.example.test",
    "SMTP_USERNAME": "test-smtp-user",
    "SMTP_PASSWORD": "test-smtp-password",
    "SMTP_FROM_EMAIL": "automation@example.test",
    "OPS_EMAIL_TO": "ops@example.test",
    "AUTOMATION_EMAIL_TO": "automation@example.test",
}


def test_settings_load_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in REQUIRED_TEST_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("WORKER_LEASE_SECONDS", "1200")
    monkeypatch.setenv("WORKER_POLL_INTERVAL_SECONDS", "2.5")
    monkeypatch.setenv("WORKER_ERROR_BACKOFF_SECONDS", "7.5")

    settings = Settings(_env_file=None)

    assert (
        settings.database_url.get_secret_value()
        == REQUIRED_TEST_ENV["DATABASE_URL"]
    )
    assert settings.sqlalchemy_database_url.startswith("postgresql+psycopg://")
    assert settings.worker_lease_seconds == 1200
    assert settings.worker_poll_interval_seconds == 2.5
    assert settings.worker_error_backoff_seconds == 7.5
    assert settings.hubspot_api_version == "2026-03"
    assert settings.smtp_starttls is True
    assert settings.smtp_timeout_seconds == 30
    assert settings.notification_batch_interval_seconds == 7_200
    assert settings.notification_batch_max_items == 100
    assert settings.notification_failure_retry_seconds == 300
    assert settings.website_hmac_secret.get_secret_value() == "test-website-secret"
    assert "test-website-secret" not in repr(settings)
    assert "test-password" not in repr(settings)
    assert "test-password" not in str(settings)


@pytest.mark.parametrize(
    "database_url",
    [
        "postgres://user:password@localhost:5432/project3",
        "postgresql://user:password@localhost:5432/project3",
        "postgresql+psycopg://user:password@localhost:5432/project3",
    ],
)
def test_postgresql_database_urls_are_accepted(database_url: str) -> None:
    settings = DatabaseSettings(database_url=database_url, _env_file=None)

    assert settings.database_url.get_secret_value() == database_url
    assert settings.sqlalchemy_database_url.startswith("postgresql+psycopg://")


@pytest.mark.parametrize(
    "database_url",
    [
        "sqlite:///project3.db",
        "mysql://user:password@localhost/project3",
        "not-a-database-url",
    ],
)
def test_non_postgresql_database_urls_are_rejected(database_url: str) -> None:
    with pytest.raises(ValidationError, match="PostgreSQL-compatible"):
        DatabaseSettings(database_url=database_url, _env_file=None)


def test_missing_required_configuration_fails_clearly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in REQUIRED_TEST_ENV:
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None)

    missing_fields = {
        error["loc"][0]
        for error in exc_info.value.errors()
        if error["type"] == "missing"
    }
    assert "database_url" in missing_fields
    assert "hubspot_service_key" in missing_fields
    assert "smtp_password" in missing_fields


@pytest.mark.parametrize(
    "overrides",
    [
        {"worker_poll_interval_seconds": 0},
        {"worker_poll_interval_seconds": -1},
        {"worker_error_backoff_seconds": 0},
        {"worker_error_backoff_seconds": -1},
    ],
)
def test_worker_loop_timings_must_be_positive(overrides: dict[str, float]) -> None:
    values = {name.lower(): value for name, value in REQUIRED_TEST_ENV.items()}
    values.update(overrides)

    with pytest.raises(ValidationError):
        Settings(**values, _env_file=None)


def test_custom_positive_smtp_timeout_is_accepted() -> None:
    values = {name.lower(): value for name, value in REQUIRED_TEST_ENV.items()}

    settings = Settings(
        **values,
        smtp_timeout_seconds=12.5,
        _env_file=None,
    )

    assert settings.smtp_timeout_seconds == 12.5


def test_smtp_timeout_loads_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in REQUIRED_TEST_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("SMTP_TIMEOUT_SECONDS", "18.5")

    settings = Settings(_env_file=None)

    assert settings.smtp_timeout_seconds == 18.5


@pytest.mark.parametrize("timeout", [0, -1, -0.1])
def test_smtp_timeout_must_be_positive(timeout: float) -> None:
    values = {name.lower(): value for name, value in REQUIRED_TEST_ENV.items()}

    with pytest.raises(ValidationError):
        Settings(
            **values,
            smtp_timeout_seconds=timeout,
            _env_file=None,
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"notification_batch_interval_seconds": 0},
        {"notification_batch_interval_seconds": -1},
        {"notification_batch_max_items": 0},
        {"notification_batch_max_items": -1},
        {"notification_failure_retry_seconds": 0},
        {"notification_failure_retry_seconds": -1},
    ],
)
def test_notification_scheduler_timings_must_be_positive(
    overrides: dict[str, int],
) -> None:
    values = {name.lower(): value for name, value in REQUIRED_TEST_ENV.items()}
    values.update(overrides)

    with pytest.raises(ValidationError):
        Settings(**values, _env_file=None)


def test_notification_scheduler_timings_load_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in REQUIRED_TEST_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("NOTIFICATION_BATCH_INTERVAL_SECONDS", "3600")
    monkeypatch.setenv("NOTIFICATION_BATCH_MAX_ITEMS", "25")
    monkeypatch.setenv("NOTIFICATION_FAILURE_RETRY_SECONDS", "120")

    configured = Settings(_env_file=None)

    assert configured.notification_batch_interval_seconds == 3_600
    assert configured.notification_batch_max_items == 25
    assert configured.notification_failure_retry_seconds == 120