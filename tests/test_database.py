import os
from pathlib import Path
import subprocess
import sys
from typing import Any

from sqlalchemy.dialects import postgresql

from project3_crm.config import DatabaseSettings
from project3_crm.db.database import (
    check_database_readiness,
    create_database_engine,
    create_session_factory,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_database_engine_and_session_factory_are_created_without_connecting() -> None:
    settings = DatabaseSettings(
        database_url=(
            "postgresql://project3:test-password@127.0.0.1:1/project3_test"
        ),
        _env_file=None,
    )

    engine = create_database_engine(settings)
    try:
        assert engine.url.drivername == "postgresql+psycopg"
        assert engine.url.render_as_string(hide_password=True).endswith(
            "@127.0.0.1:1/project3_test"
        )
        session_factory = create_session_factory(engine)
        assert session_factory.kw["expire_on_commit"] is False
    finally:
        engine.dispose()


def test_web_application_import_does_not_require_database_configuration() -> None:
    environment = os.environ.copy()
    environment.pop("DATABASE_URL", None)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from project3_crm.web import app; print(app.title)",
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "Project #3 CRM Integration"


def test_database_readiness_executes_only_select_one() -> None:
    statements: list[Any] = []

    class Result:
        def scalar_one(self) -> int:
            return 1

    class Connection:
        def __enter__(self) -> "Connection":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def execute(self, statement: Any) -> Result:
            statements.append(statement)
            return Result()

    class Engine:
        def connect(self) -> Connection:
            return Connection()

    check_database_readiness(Engine())  # type: ignore[arg-type]

    assert len(statements) == 1
    assert str(statements[0].compile(dialect=postgresql.dialect())) == "SELECT 1"
