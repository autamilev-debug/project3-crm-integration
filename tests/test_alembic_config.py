import os
from pathlib import Path
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
UNRELATED_CONFIGURATION = {
    "WEBSITE_HMAC_SECRET",
    "LINKEDIN_BEARER_TOKEN",
    "PARTNER_API_KEY",
    "HUBSPOT_SERVICE_KEY",
    "SMTP_HOST",
    "SMTP_USERNAME",
    "SMTP_PASSWORD",
    "SMTP_FROM_EMAIL",
    "OPS_EMAIL_TO",
    "AUTOMATION_EMAIL_TO",
}


def _run_alembic_offline(*arguments: str) -> subprocess.CompletedProcess[str]:
    database_password = "alembic-test-password"
    environment = os.environ.copy()
    for name in UNRELATED_CONFIGURATION:
        environment.pop(name, None)
    environment["DATABASE_URL"] = (
        f"postgresql://project3:{database_password}@localhost:5432/project3_test"
    )

    return subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_alembic_offline_upgrade_requires_only_database_url() -> None:
    result = _run_alembic_offline("upgrade", "head", "--sql")

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "alembic-test-password" not in output

    sql = output.upper()
    assert "CREATE TABLE WEBHOOK_EVENTS" in sql
    assert "CREATE SEQUENCE NORMALIZED_LEADS_ID_SEQ" in sql
    assert "CREATE TABLE NORMALIZED_LEADS" in sql
    assert "CREATE TABLE INTEGRATION_RUNTIME_STATE" in sql
    assert "ON CONFLICT (ID) DO NOTHING" in sql
    assert "CK_NORMALIZED_LEADS_AUTOMATIC_RETRY_COUNT_RANGE" in sql

    webhook_table_sql = sql.split(
        "CREATE TABLE WEBHOOK_EVENTS",
        maxsplit=1,
    )[1].split(");", maxsplit=1)[0]
    assert "COMPLETED_AT TIMESTAMP WITH TIME ZONE" in webhook_table_sql

    runtime_table_sql = sql.split(
        "CREATE TABLE INTEGRATION_RUNTIME_STATE",
        maxsplit=1,
    )[1].split(");", maxsplit=1)[0]
    assert "ID SMALLINT NOT NULL" in runtime_table_sql
    assert "SMALLSERIAL" not in runtime_table_sql
    assert "INTEGRATION_RUNTIME_STATE_ID_SEQ" not in sql
    assert "VALUES (1, FALSE, CURRENT_TIMESTAMP)" in sql
    assert "CK_INTEGRATION_RUNTIME_STATE_SINGLETON_ID" in runtime_table_sql
    assert "CREATE SEQUENCE NORMALIZED_LEADS_ID_SEQ" in sql


def test_alembic_offline_downgrade_compiles() -> None:
    result = _run_alembic_offline(
        "downgrade",
        "20260903_0001:base",
        "--sql",
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "alembic-test-password" not in output

    sql = output.upper()
    assert "DROP TABLE INTEGRATION_RUNTIME_STATE" in sql
    assert "DROP TABLE NORMALIZED_LEADS" in sql
    assert "DROP SEQUENCE NORMALIZED_LEADS_ID_SEQ" in sql
    assert "DROP TABLE WEBHOOK_EVENTS" in sql
