from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.dml import Update
from sqlalchemy.sql.selectable import Select

import project3_crm.operator as operator_module
from project3_crm.operator import OperatorStateError, clear_crm_pause


NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


class FakeResult:
    def __init__(self, mapping: dict[str, Any] | None = None) -> None:
        self.mapping = mapping

    def mappings(self) -> FakeResult:
        return self

    def one_or_none(self) -> dict[str, Any] | None:
        return deepcopy(self.mapping)


@dataclass
class FakeOperatorEngine:
    runtime: dict[str, Any] | None
    leads: list[dict[str, Any]] = field(default_factory=list)
    statements: list[Any] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    disposed: bool = False

    def begin(self) -> FakeTransaction:
        return FakeTransaction(self)

    def dispose(self) -> None:
        self.disposed = True


class FakeTransaction:
    def __init__(self, engine: FakeOperatorEngine) -> None:
        self.engine = engine
        self.runtime_snapshot: dict[str, Any] | None = None

    def __enter__(self) -> FakeConnection:
        self.runtime_snapshot = deepcopy(self.engine.runtime)
        self.engine.events.append("begin")
        return FakeConnection(self.engine)

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        if exc_type is not None:
            self.engine.runtime = self.runtime_snapshot
            self.engine.events.append("rollback")
            return False
        self.engine.events.append("commit")
        return False


class FakeConnection:
    def __init__(self, engine: FakeOperatorEngine) -> None:
        self.engine = engine

    def execute(self, statement: Any) -> FakeResult:
        self.engine.statements.append(statement)
        if isinstance(statement, Select):
            return FakeResult(self.engine.runtime)
        if isinstance(statement, Update):
            assert self.engine.runtime is not None
            values = statement.compile(dialect=postgresql.dialect()).params
            for key in (
                "crm_delivery_paused",
                "pause_reason",
                "paused_at",
                "pause_alert_sent_at",
                "pause_alert_last_error",
                "updated_at",
            ):
                if key in values:
                    self.engine.runtime[key] = values[key]
            return FakeResult()
        raise AssertionError(f"unexpected statement: {statement}")


def runtime_row(*, paused: bool, reason: str | None) -> dict[str, Any]:
    return {
        "id": 1,
        "crm_delivery_paused": paused,
        "pause_reason": reason,
        "paused_at": NOW if paused else None,
        "pause_alert_sent_at": NOW if paused else None,
        "pause_alert_last_error": "safe alert failure" if paused else None,
        "updated_at": NOW,
    }


@pytest.mark.parametrize("reason", ["AUTH", "CONFIG"])
def test_clear_crm_pause_clears_only_runtime_pause_fields(reason: str) -> None:
    leads = [
        {
            "id": 10,
            "delivery_status": "UNKNOWN",
            "write_attempt_count": 2,
            "automatic_retry_count": 1,
            "reconciliation_not_found_count": 1,
            "reconciliation_error_count": 2,
        }
    ]
    engine = FakeOperatorEngine(runtime_row(paused=True, reason=reason), leads)
    original_leads = deepcopy(leads)

    result = clear_crm_pause(engine, now=NOW)

    assert result.was_paused is True
    assert result.previous_reason == reason
    assert engine.runtime == {
        "id": 1,
        "crm_delivery_paused": False,
        "pause_reason": None,
        "paused_at": None,
        "pause_alert_sent_at": None,
        "pause_alert_last_error": None,
        "updated_at": NOW,
    }
    assert engine.leads == original_leads
    assert engine.events == ["begin", "commit"]
    assert len(engine.statements) == 2
    select_sql = str(engine.statements[0].compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in select_sql
    assert "integration_runtime_state.id" in select_sql


def test_clear_crm_pause_is_clean_noop_when_not_paused() -> None:
    engine = FakeOperatorEngine(runtime_row(paused=False, reason=None))

    result = clear_crm_pause(engine, now=NOW)

    assert result.was_paused is False
    assert result.previous_reason is None
    assert len(engine.statements) == 1
    assert engine.events == ["begin", "commit"]


def test_clear_crm_pause_rejects_missing_singleton() -> None:
    engine = FakeOperatorEngine(None)

    with pytest.raises(OperatorStateError, match="singleton id=1 is missing"):
        clear_crm_pause(engine, now=NOW)

    assert engine.events == ["begin", "rollback"]


@pytest.mark.parametrize("reason", ["AUTH", "CONFIG"])
def test_operator_command_reports_safe_reason_and_disposes_engine(
    reason: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine = FakeOperatorEngine(runtime_row(paused=True, reason=reason))
    monkeypatch.setattr(operator_module, "get_database_engine", lambda: engine)
    for name in (
        "WEBSITE_HMAC_SECRET",
        "LINKEDIN_BEARER_TOKEN",
        "PARTNER_API_KEY",
        "HUBSPOT_SERVICE_KEY",
        "SMTP_USERNAME",
        "SMTP_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)

    exit_code = operator_module.main(["clear-crm-pause"])

    assert exit_code == 0
    assert capsys.readouterr().out.strip() == (
        f"CRM pause cleared; previous reason: {reason}"
    )
    assert engine.disposed is True


def test_operator_command_reports_no_active_pause(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine = FakeOperatorEngine(runtime_row(paused=False, reason=None))
    monkeypatch.setattr(operator_module, "get_database_engine", lambda: engine)

    assert operator_module.main(["clear-crm-pause"]) == 0
    assert capsys.readouterr().out.strip() == "CRM delivery is not paused"
