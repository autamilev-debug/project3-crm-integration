import os
import subprocess
import sys
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.dml import Update
from sqlalchemy.sql.selectable import Select

import project3_crm.worker as worker_module
from project3_crm.crm_delivery import CrmDeliveryAction, CrmDeliveryOutcome
from project3_crm.notifications.service import (
    AlertNotificationAction,
    AlertNotificationOutcome,
    BatchNotificationAction,
    BatchNotificationOutcome,
)
from project3_crm.worker import (
    WorkerStartupReadinessError,
    check_worker_startup_readiness,
    claim_one_received,
    main,
    recover_one_stale_processing,
    run_notification_check,
    run_worker_cycle,
    run_worker_loop,
)


NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FakeResult:
    def __init__(self, *, mapping: Any = None, scalar: int | None = None) -> None:
        self.mapping = mapping
        self.scalar = scalar

    def mappings(self) -> "FakeResult":
        return self

    def one_or_none(self) -> Any:
        return deepcopy(self.mapping)

    def scalar_one_or_none(self) -> int | None:
        return self.scalar


@dataclass
class FakeWorkerEngine:
    rows: list[dict[str, Any]]
    normalized_event_ids: set[int] = field(default_factory=set)
    statements: list[Any] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    disposed: bool = False

    def begin(self) -> "FakeTransaction":
        return FakeTransaction(self)

    def dispose(self) -> None:
        self.disposed = True
        self.events.append("dispose")


class FakeTransaction:
    def __init__(self, engine: FakeWorkerEngine) -> None:
        self.engine = engine
        self.snapshot: list[dict[str, Any]] = []

    def __enter__(self) -> "FakeConnection":
        self.snapshot = deepcopy(self.engine.rows)
        self.engine.events.append("begin")
        return FakeConnection(self.engine)

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        if exc_type is not None:
            self.engine.rows = self.snapshot
            self.engine.events.append("rollback")
            return False
        self.engine.events.append("commit")
        return False


class FakeConnection:
    def __init__(self, engine: FakeWorkerEngine) -> None:
        self.engine = engine
        self.selected_id: int | None = None

    def execute(self, statement: Any) -> FakeResult:
        self.engine.statements.append(statement)
        compiled = statement.compile(dialect=postgresql.dialect())
        sql = str(compiled)
        parameters = compiled.params

        if isinstance(statement, Select) and "FROM webhook_events" in sql:
            if "processing_started_at <" in sql:
                self.engine.events.append("select_stale")
                cutoff = next(
                    value for value in parameters.values() if isinstance(value, datetime)
                )
                candidates = [
                    row
                    for row in self.engine.rows
                    if row["event_status"] == "PROCESSING"
                    and row["processing_started_at"] is not None
                    and row["processing_started_at"] < cutoff
                ]
                candidates.sort(key=lambda row: (row["processing_started_at"], row["id"]))
            else:
                self.engine.events.append("select_received")
                candidates = [
                    row for row in self.engine.rows if row["event_status"] == "RECEIVED"
                ]
                candidates.sort(key=lambda row: (row["received_at"], row["id"]))
            selected = candidates[0] if candidates else None
            self.selected_id = selected["id"] if selected else None
            return FakeResult(mapping=selected)

        if isinstance(statement, Select) and "FROM normalized_leads" in sql:
            self.engine.events.append("select_normalized")
            scalar = (
                self.selected_id
                if self.selected_id in self.engine.normalized_event_ids
                else None
            )
            return FakeResult(scalar=scalar)

        if isinstance(statement, Update):
            self.engine.events.append("update_webhook")
            assert self.selected_id is not None
            row = next(row for row in self.engine.rows if row["id"] == self.selected_id)
            required_status = next(
                (
                    value
                    for key, value in parameters.items()
                    if key.startswith("event_status_")
                ),
                None,
            )
            if required_status is not None and row["event_status"] != required_status:
                return FakeResult(scalar=None)
            for key in (
                "event_status",
                "processing_started_at",
                "normalized_at",
                "completed_at",
                "last_error_category",
                "last_error",
                "updated_at",
            ):
                if key in parameters:
                    row[key] = parameters[key]
            returned_id = row["id"] if "RETURNING webhook_events.id" in sql else None
            return FakeResult(scalar=returned_id)

        raise AssertionError(f"Unexpected SQL statement: {sql}")


def webhook_row(
    row_id: int,
    status: str,
    *,
    received_at: datetime | None = None,
    processing_started_at: datetime | None = None,
    completed_at: datetime | None = None,
) -> dict[str, Any]:
    return {
        "id": row_id,
        "source": "website",
        "event_id": f"WEB-{row_id}",
        "raw_payload": {
            "submission_id": f"WEB-{row_id}",
            "submitted_at": "2026-09-03T10:00:00Z",
            "first_name": "Test",
            "last_name": "Lead",
            "email": "test@example.com",
        },
        "event_status": status,
        "received_at": received_at or NOW - timedelta(hours=1),
        "processing_started_at": processing_started_at,
        "normalized_at": None,
        "completed_at": completed_at,
        "last_error_category": "OLD_ERROR",
        "last_error": "old safe error",
        "updated_at": NOW - timedelta(hours=1),
    }


def loop_settings() -> Any:
    return SimpleNamespace(
        worker_lease_seconds=900,
        worker_poll_interval_seconds=1.5,
        worker_error_backoff_seconds=5.5,
        notification_failure_retry_seconds=300,
    )


def no_crm_work(
    engine: Any,
    adapter: Any,
    settings: Any,
    *,
    now: datetime | None = None,
) -> CrmDeliveryOutcome:
    return CrmDeliveryOutcome(action=CrmDeliveryAction.NO_WORK)


def test_worker_entry_point_is_importable() -> None:
    assert callable(main)


def test_worker_import_does_not_require_configuration_or_database_connection() -> None:
    environment = os.environ.copy()
    for name in (
        "DATABASE_URL",
        "WEBSITE_HMAC_SECRET",
        "LINKEDIN_BEARER_TOKEN",
        "PARTNER_API_KEY",
        "HUBSPOT_SERVICE_KEY",
        "SMTP_PASSWORD",
    ):
        environment.pop(name, None)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from project3_crm.worker import run_worker_cycle; print(callable(run_worker_cycle))",
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "True"


def test_claim_oldest_received_event_and_commit_state_before_return() -> None:
    later = webhook_row(3, "RECEIVED", received_at=NOW - timedelta(minutes=5))
    tie_higher_id = webhook_row(2, "RECEIVED", received_at=NOW - timedelta(minutes=10))
    tie_lower_id = webhook_row(1, "RECEIVED", received_at=NOW - timedelta(minutes=10))
    engine = FakeWorkerEngine([later, tie_higher_id, tie_lower_id])

    claimed = claim_one_received(engine, now=NOW)

    assert claimed is not None
    assert claimed.id == 1
    assert claimed.event_id == "WEB-1"
    assert claimed.raw_payload == tie_lower_id["raw_payload"]
    claimed_row = next(row for row in engine.rows if row["id"] == 1)
    assert claimed_row["event_status"] == "PROCESSING"
    assert claimed_row["processing_started_at"] == NOW
    assert claimed_row["completed_at"] is None
    assert claimed_row["updated_at"] == NOW
    assert engine.events == ["begin", "select_received", "update_webhook", "commit"]

    claim_sql = str(engine.statements[0].compile(dialect=postgresql.dialect()))
    assert "ORDER BY webhook_events.received_at ASC, webhook_events.id ASC" in claim_sql
    assert "LIMIT" in claim_sql
    assert "FOR UPDATE" in claim_sql


def test_claim_ignores_non_received_and_returns_none_when_unavailable() -> None:
    engine = FakeWorkerEngine(
        [
            webhook_row(1, "PROCESSING", processing_started_at=NOW),
            webhook_row(2, "NORMALIZED"),
            webhook_row(3, "NORMALIZATION_FAILED"),
        ]
    )

    claimed = claim_one_received(engine, now=NOW)

    assert claimed is None
    assert all(row["event_status"] != "RECEIVED" for row in engine.rows)
    assert engine.events == ["begin", "select_received", "commit"]


def test_worker_cycle_commits_claim_before_normalization() -> None:
    engine = FakeWorkerEngine([webhook_row(1, "RECEIVED")])

    def normalize(fake_engine: Any, event: Any) -> None:
        fake_engine.events.append("normalize")
        row = next(row for row in fake_engine.rows if row["id"] == event.id)
        assert row["event_status"] == "PROCESSING"

    useful_work = run_worker_cycle(
        engine,
        object(),
        loop_settings(),
        object(),
        normalize=normalize,
        crm_work=no_crm_work,
        now=NOW,
    )

    assert useful_work is True
    assert engine.events[-2:] == ["commit", "normalize"]
    assert engine.events.count("normalize") == 1


def test_stale_processing_without_normalized_row_resets_to_received() -> None:
    stale_started = NOW - timedelta(seconds=901)
    engine = FakeWorkerEngine(
        [
            webhook_row(
                1,
                "PROCESSING",
                processing_started_at=stale_started,
                completed_at=NOW - timedelta(days=1),
            )
        ]
    )

    recovery = recover_one_stale_processing(engine, 900, now=NOW)

    assert recovery is not None
    assert recovery.webhook_event_id == 1
    assert recovery.action == "RESET_TO_RECEIVED"
    assert engine.rows[0]["event_status"] == "RECEIVED"
    assert engine.rows[0]["processing_started_at"] is None
    assert engine.rows[0]["completed_at"] is None
    assert engine.rows[0]["updated_at"] == NOW
    assert engine.events[-1] == "commit"


def test_stale_processing_with_normalized_row_repairs_parent() -> None:
    engine = FakeWorkerEngine(
        [
            webhook_row(
                1,
                "PROCESSING",
                processing_started_at=NOW - timedelta(seconds=901),
            )
        ],
        normalized_event_ids={1},
    )

    recovery = recover_one_stale_processing(engine, 900, now=NOW)

    assert recovery is not None
    assert recovery.action == "REPAIRED_NORMALIZED"
    assert engine.rows[0]["event_status"] == "NORMALIZED"
    assert engine.rows[0]["normalized_at"] == NOW
    assert engine.rows[0]["completed_at"] == NOW
    assert engine.rows[0]["last_error_category"] is None
    assert engine.rows[0]["last_error"] is None
    assert engine.normalized_event_ids == {1}


@pytest.mark.parametrize(
    ("age_seconds", "lease_seconds", "should_recover"),
    [(899, 900, False), (900, 900, False), (901, 900, True), (901, 1_200, False)],
)
def test_stale_cutoff_uses_configured_lease(
    age_seconds: int,
    lease_seconds: int,
    should_recover: bool,
) -> None:
    engine = FakeWorkerEngine(
        [
            webhook_row(
                1,
                "PROCESSING",
                processing_started_at=NOW - timedelta(seconds=age_seconds),
            )
        ]
    )

    recovery = recover_one_stale_processing(engine, lease_seconds, now=NOW)

    assert (recovery is not None) is should_recover
    expected_status = "RECEIVED" if should_recover else "PROCESSING"
    assert engine.rows[0]["event_status"] == expected_status


def test_recovery_rejects_naive_clock_values() -> None:
    engine = FakeWorkerEngine([])

    with pytest.raises(ValueError, match="timezone-aware"):
        recover_one_stale_processing(engine, 900, now=datetime(2026, 9, 3, 12, 0))


def test_recovery_repairs_only_one_oldest_stale_event_with_id_tiebreaker() -> None:
    oldest = NOW - timedelta(hours=2)
    rows = [
        webhook_row(3, "PROCESSING", processing_started_at=NOW - timedelta(hours=1)),
        webhook_row(2, "PROCESSING", processing_started_at=oldest),
        webhook_row(1, "PROCESSING", processing_started_at=oldest),
    ]
    engine = FakeWorkerEngine(rows)

    recovery = recover_one_stale_processing(engine, 900, now=NOW)

    assert recovery is not None
    assert recovery.webhook_event_id == 1
    assert next(row for row in engine.rows if row["id"] == 1)["event_status"] == "RECEIVED"
    assert next(row for row in engine.rows if row["id"] == 2)["event_status"] == "PROCESSING"

    recovery_sql = str(engine.statements[0].compile(dialect=postgresql.dialect()))
    assert "processing_started_at ASC, webhook_events.id ASC" in recovery_sql
    assert "FOR UPDATE" in recovery_sql


def test_recovered_event_can_be_claimed_and_normalized_in_same_cycle() -> None:
    engine = FakeWorkerEngine(
        [
            webhook_row(
                1,
                "PROCESSING",
                processing_started_at=NOW - timedelta(seconds=901),
            )
        ]
    )
    normalized_ids: list[int] = []

    def normalize(fake_engine: Any, event: Any) -> None:
        normalized_ids.append(event.id)

    useful_work = run_worker_cycle(
        engine,
        object(),
        loop_settings(),
        object(),
        normalize=normalize,
        crm_work=no_crm_work,
        now=NOW,
    )

    assert useful_work is True
    assert normalized_ids == [1]
    assert engine.rows[0]["event_status"] == "PROCESSING"


def test_unexpected_normalization_exception_leaves_claimed_event_processing() -> None:
    engine = FakeWorkerEngine([webhook_row(1, "RECEIVED")])

    def fail_normalization(fake_engine: Any, event: Any) -> None:
        raise RuntimeError("private.person@example.com")

    with pytest.raises(RuntimeError):
        run_worker_cycle(
            engine,
            object(),
            loop_settings(),
            object(),
            normalize=fail_normalization,
            crm_work=no_crm_work,
            now=NOW,
        )

    assert engine.rows[0]["event_status"] == "PROCESSING"
    assert engine.rows[0]["processing_started_at"] == NOW


def test_cycle_normalizes_then_allows_crm_work_in_same_cycle() -> None:
    engine = FakeWorkerEngine([webhook_row(1, "RECEIVED")])
    order: list[str] = []

    def normalize(fake_engine: Any, event: Any) -> None:
        order.append("normalize")

    def crm_work(*args: Any, **kwargs: Any) -> CrmDeliveryOutcome:
        assert order == ["normalize"]
        order.append("create")
        return CrmDeliveryOutcome(action=CrmDeliveryAction.SUCCESS, lead_id=10)

    useful = run_worker_cycle(
        engine,
        object(),
        loop_settings(),
        object(),
        normalize=normalize,
        crm_work=crm_work,
        now=NOW,
    )

    assert useful is True
    assert order == ["normalize", "create"]


def test_cycle_can_perform_crm_work_without_normalization() -> None:
    engine = FakeWorkerEngine([])
    crm_calls = 0

    def crm_work(*args: Any, **kwargs: Any) -> CrmDeliveryOutcome:
        nonlocal crm_calls
        crm_calls += 1
        return CrmDeliveryOutcome(action=CrmDeliveryAction.SUCCESS, lead_id=10)

    useful = run_worker_cycle(
        engine,
        object(),
        loop_settings(),
        object(),
        crm_work=crm_work,
        now=NOW,
    )

    assert useful is True
    assert crm_calls == 1


@pytest.mark.parametrize(
    "action",
    [CrmDeliveryAction.NO_WORK, CrmDeliveryAction.CRM_BLOCKED],
)
def test_cycle_does_not_count_idle_or_fresh_sending_block_as_useful(
    action: CrmDeliveryAction,
) -> None:
    engine = FakeWorkerEngine([])

    def crm_work(*args: Any, **kwargs: Any) -> CrmDeliveryOutcome:
        return CrmDeliveryOutcome(action=action)

    useful = run_worker_cycle(
        engine,
        object(),
        loop_settings(),
        object(),
        crm_work=crm_work,
        now=NOW,
    )

    assert useful is False


def test_cycle_continues_normalization_when_crm_is_paused() -> None:
    engine = FakeWorkerEngine([webhook_row(1, "RECEIVED")])
    adapter_io: list[str] = []
    normalized: list[int] = []

    class NoIoAdapter:
        def create_contact(self, *args: Any) -> None:
            adapter_io.append("create")

        def get_contact_by_integration_reference(self, *args: Any) -> None:
            adapter_io.append("read")

    def crm_work(*args: Any, **kwargs: Any) -> CrmDeliveryOutcome:
        return CrmDeliveryOutcome(action=CrmDeliveryAction.NO_WORK)

    useful = run_worker_cycle(
        engine,
        NoIoAdapter(),
        loop_settings(),
        object(),
        normalize=lambda fake_engine, event: normalized.append(event.id),
        crm_work=crm_work,
        now=NOW,
    )

    assert useful is True
    assert normalized == [1]
    assert adapter_io == []


def test_worker_reuses_one_adapter_across_cycles() -> None:
    adapter = object()
    seen_adapters: list[Any] = []

    def cycle(
        engine: Any,
        received_adapter: Any,
        settings: Any,
        sender: Any,
    ) -> bool:
        seen_adapters.append(received_adapter)
        return True

    run_worker_loop(
        object(),
        adapter,
        loop_settings(),
        object(),
        cycle=cycle,
        sleep=lambda seconds: None,
        should_stop=lambda: len(seen_adapters) >= 2,
    )

    assert seen_adapters == [adapter, adapter]


def test_crm_exception_after_sending_commit_backs_off_without_duplicate_create(
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = {"status": "PENDING", "create_calls": 0, "cycle_calls": 0}
    sleeps: list[float] = []

    def cycle(engine: Any, adapter: Any, settings: Any, sender: Any) -> bool:
        state["cycle_calls"] += 1
        if state["status"] == "PENDING":
            state["status"] = "SENDING"
            state["create_calls"] += 1
            raise RuntimeError("private.person@example.com")
        assert state["status"] == "SENDING"
        return False

    with caplog.at_level("ERROR", logger="project3_crm.worker"):
        run_worker_loop(
            object(),
            object(),
            loop_settings(),
            object(),
            cycle=cycle,
            sleep=sleeps.append,
            should_stop=lambda: state["cycle_calls"] >= 2,
        )

    assert state == {"status": "SENDING", "create_calls": 1, "cycle_calls": 2}
    assert sleeps == [5.5, 1.5]
    assert "private.person@example.com" not in caplog.text


def test_idle_loop_sleeps_poll_interval() -> None:
    cycle_calls = 0
    sleeps: list[float] = []

    def cycle(engine: Any, adapter: Any, settings: Any, sender: Any) -> bool:
        nonlocal cycle_calls
        cycle_calls += 1
        return False

    run_worker_loop(
        object(),
        object(),
        loop_settings(),
        object(),
        cycle=cycle,
        sleep=sleeps.append,
        should_stop=lambda: cycle_calls >= 1,
    )

    assert sleeps == [1.5]


def test_successful_work_does_not_sleep() -> None:
    cycle_calls = 0
    sleeps: list[float] = []

    def cycle(engine: Any, adapter: Any, settings: Any, sender: Any) -> bool:
        nonlocal cycle_calls
        cycle_calls += 1
        return True

    run_worker_loop(
        object(),
        object(),
        loop_settings(),
        object(),
        cycle=cycle,
        sleep=sleeps.append,
        should_stop=lambda: cycle_calls >= 1,
    )

    assert sleeps == []


def test_unexpected_cycle_error_uses_backoff_logs_safely_and_continues(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cycle_calls = 0
    sleeps: list[float] = []
    private_marker = "private.person@example.com"

    def cycle(engine: Any, adapter: Any, settings: Any, sender: Any) -> bool:
        nonlocal cycle_calls
        cycle_calls += 1
        if cycle_calls == 1:
            raise RuntimeError(private_marker)
        return True

    with caplog.at_level("ERROR", logger="project3_crm.worker"):
        run_worker_loop(
            object(),
            object(),
            loop_settings(),
            object(),
            cycle=cycle,
            sleep=sleeps.append,
            should_stop=lambda: cycle_calls >= 2,
        )

    assert cycle_calls == 2
    assert sleeps == [5.5]
    assert "Worker cycle failed" in caplog.text
    assert private_marker not in caplog.text


def test_keyboard_interrupt_exits_without_error_or_sleep() -> None:
    sleeps: list[float] = []

    def interrupt(
        engine: Any,
        adapter: Any,
        settings: Any,
        sender: Any,
    ) -> bool:
        raise KeyboardInterrupt

    run_worker_loop(
        object(),
        object(),
        loop_settings(),
        object(),
        cycle=interrupt,
        sleep=sleeps.append,
    )

    assert sleeps == []


def test_main_disposes_engine_after_worker_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = FakeWorkerEngine([])
    monkeypatch.setattr(worker_module, "configure_logging", lambda: None)
    monkeypatch.setattr(worker_module, "get_settings", loop_settings)
    monkeypatch.setattr(worker_module, "get_database_engine", lambda: engine)

    class FakeAdapter:
        def __init__(self, settings: Any) -> None:
            engine.events.append("adapter_created")

        def close(self) -> None:
            engine.events.append("adapter_closed")

    monkeypatch.setattr(worker_module, "HubSpotAdapter", FakeAdapter)
    monkeypatch.setattr(
        worker_module,
        "check_worker_startup_readiness",
        lambda received_engine: engine.events.append("preflight"),
    )

    class FakeSender:
        def __init__(self, settings: Any) -> None:
            engine.events.append("sender_created")

    monkeypatch.setattr(worker_module, "SmtpNotificationSender", FakeSender)
    monkeypatch.setattr(
        worker_module,
        "run_worker_loop",
        lambda *args: engine.events.append("worker_loop"),
    )

    main()

    assert engine.disposed is True
    assert engine.events[-6:] == [
        "preflight",
        "adapter_created",
        "sender_created",
        "worker_loop",
        "adapter_closed",
        "dispose",
    ]


@dataclass
class FakePreflightEngine:
    singleton_id: int | None = 1
    failure: Exception | None = None
    statements: list[Any] = field(default_factory=list)

    def connect(self) -> "FakePreflightConnection":
        if self.failure is not None:
            raise self.failure
        return FakePreflightConnection(self)


class FakePreflightConnection:
    def __init__(self, engine: FakePreflightEngine) -> None:
        self.engine = engine

    def __enter__(self) -> "FakePreflightConnection":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, statement: Any) -> Any:
        self.engine.statements.append(statement)
        sql = str(statement.compile(dialect=postgresql.dialect()))
        if sql == "SELECT 1":
            return SimpleNamespace(scalar_one=lambda: 1)
        assert "FROM integration_runtime_state" in sql
        assert "integration_runtime_state.id =" in sql
        return SimpleNamespace(
            scalar_one_or_none=lambda: self.engine.singleton_id,
        )


def test_worker_startup_readiness_checks_database_then_runtime_singleton() -> None:
    engine = FakePreflightEngine()

    check_worker_startup_readiness(engine)  # type: ignore[arg-type]

    assert len(engine.statements) == 2
    assert str(engine.statements[0].compile(dialect=postgresql.dialect())) == (
        "SELECT 1"
    )
    singleton_sql = str(
        engine.statements[1].compile(dialect=postgresql.dialect())
    )
    assert "SELECT integration_runtime_state.id" in singleton_sql
    assert "WHERE integration_runtime_state.id =" in singleton_sql


def test_worker_startup_readiness_rejects_missing_singleton_without_mutation() -> None:
    engine = FakePreflightEngine(singleton_id=None)

    with pytest.raises(
        WorkerStartupReadinessError,
        match="runtime singleton is not initialized",
    ):
        check_worker_startup_readiness(engine)  # type: ignore[arg-type]

    sql = " ".join(
        str(statement.compile(dialect=postgresql.dialect()))
        for statement in engine.statements
    ).upper()
    assert "INSERT" not in sql
    assert "UPDATE" not in sql


def test_worker_startup_database_failure_is_sanitized() -> None:
    private_marker = "postgresql://user:database-password@private-host/project3"
    engine = FakePreflightEngine(failure=RuntimeError(private_marker))

    with pytest.raises(WorkerStartupReadinessError) as captured:
        check_worker_startup_readiness(engine)  # type: ignore[arg-type]

    assert str(captured.value) == "worker database readiness check failed"
    assert captured.value.__cause__ is None
    assert private_marker not in str(captured.value)


def test_main_preflight_failure_skips_external_objects_loop_and_disposes_engine(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    engine = FakeWorkerEngine([])
    external_events: list[str] = []
    monkeypatch.setattr(worker_module, "configure_logging", lambda: None)
    monkeypatch.setattr(worker_module, "get_settings", loop_settings)
    monkeypatch.setattr(worker_module, "get_database_engine", lambda: engine)
    monkeypatch.setattr(
        worker_module,
        "check_worker_startup_readiness",
        lambda received_engine: (_ for _ in ()).throw(
            WorkerStartupReadinessError("safe startup readiness failure")
        ),
    )
    monkeypatch.setattr(
        worker_module,
        "HubSpotAdapter",
        lambda settings: external_events.append("adapter"),
    )
    monkeypatch.setattr(
        worker_module,
        "SmtpNotificationSender",
        lambda settings: external_events.append("sender"),
    )
    monkeypatch.setattr(
        worker_module,
        "run_worker_loop",
        lambda *args: external_events.append("loop"),
    )

    with caplog.at_level("ERROR", logger="project3_crm.worker"):
        with pytest.raises(WorkerStartupReadinessError):
            main()

    assert external_events == []
    assert engine.disposed is True
    assert "Worker startup readiness failed" in caplog.text


def test_global_pause_result_attempts_one_immediate_alert_after_crm_work() -> None:
    engine = FakeWorkerEngine([])
    sender = object()
    order: list[str] = []

    def crm_work(*args: Any, **kwargs: Any) -> CrmDeliveryOutcome:
        order.append("crm_committed")
        return CrmDeliveryOutcome(
            action=CrmDeliveryAction.GLOBAL_PAUSE_ACTIVATED,
            lead_id=17,
        )

    def pause_alert(
        received_engine: Any,
        received_sender: Any,
        received_settings: Any,
        *,
        now: datetime,
    ) -> AlertNotificationOutcome:
        assert received_engine is engine
        assert received_sender is sender
        assert now == NOW
        order.append("pause_alert")
        return AlertNotificationOutcome(AlertNotificationAction.SEND_FAILED)

    useful = run_worker_cycle(
        engine,
        object(),
        loop_settings(),
        sender,
        crm_work=crm_work,
        pause_alert=pause_alert,
        now=NOW,
    )

    assert useful is True
    assert order == ["crm_committed", "pause_alert"]


def test_immediate_pause_alert_exception_is_sanitized_and_does_not_undo_pause(
    caplog: pytest.LogCaptureFixture,
) -> None:
    engine = FakeWorkerEngine([])
    state = {"paused": False, "lead_status": "PENDING"}
    private_marker = "smtp-password-and-private.person@example.test"

    def crm_work(*args: Any, **kwargs: Any) -> CrmDeliveryOutcome:
        state["paused"] = True
        return CrmDeliveryOutcome(CrmDeliveryAction.GLOBAL_PAUSE_ACTIVATED, 19)

    def broken_alert(*args: Any, **kwargs: Any) -> AlertNotificationOutcome:
        raise RuntimeError(private_marker)

    with caplog.at_level("ERROR", logger="project3_crm.worker"):
        useful = run_worker_cycle(
            engine,
            object(),
            loop_settings(),
            object(),
            crm_work=crm_work,
            pause_alert=broken_alert,
            now=NOW,
        )

    assert useful is True
    assert state == {"paused": True, "lead_status": "PENDING"}
    assert "Immediate CRM pause alert processing failed unexpectedly" in caplog.text
    assert private_marker not in caplog.text


def test_periodic_notification_operation_order_and_normal_outcome_independence() -> None:
    order: list[str] = []

    def operation(name: str, outcome: Any) -> Any:
        def invoke(*args: Any, **kwargs: Any) -> Any:
            order.append(name)
            return outcome

        return invoke

    useful = run_notification_check(
        object(),
        object(),
        loop_settings(),
        pause_alert=operation(
            "pause",
            AlertNotificationOutcome(AlertNotificationAction.NO_ALERT),
        ),
        cluster_alert=operation(
            "cluster",
            AlertNotificationOutcome(AlertNotificationAction.SEND_FAILED),
        ),
        crm_failure_batch=operation(
            "crm_batch",
            BatchNotificationOutcome(BatchNotificationAction.NOT_DUE),
        ),
        normalization_failure_batch=operation(
            "normalization_batch",
            BatchNotificationOutcome(BatchNotificationAction.SENT),
        ),
        now=NOW,
    )

    assert useful is True
    assert order == ["pause", "cluster", "crm_batch", "normalization_batch"]


def test_periodic_notification_exceptions_are_isolated_from_other_streams_and_cycle(
    caplog: pytest.LogCaptureFixture,
) -> None:
    order: list[str] = []
    private_marker = "Authorization: Bearer do-not-log-this"

    def pause(*args: Any, **kwargs: Any) -> AlertNotificationOutcome:
        order.append("pause")
        raise RuntimeError(private_marker)

    def cluster(*args: Any, **kwargs: Any) -> AlertNotificationOutcome:
        order.append("cluster")
        return AlertNotificationOutcome(AlertNotificationAction.NO_ALERT)

    def crm_batch(*args: Any, **kwargs: Any) -> BatchNotificationOutcome:
        order.append("crm_batch")
        return BatchNotificationOutcome(BatchNotificationAction.SEND_FAILED)

    def normalization_batch(*args: Any, **kwargs: Any) -> BatchNotificationOutcome:
        order.append("normalization_batch")
        return BatchNotificationOutcome(BatchNotificationAction.EMPTY)

    def notification_check(*args: Any, **kwargs: Any) -> bool:
        return run_notification_check(
            args[0],
            args[1],
            args[2],
            pause_alert=pause,
            cluster_alert=cluster,
            crm_failure_batch=crm_batch,
            normalization_failure_batch=normalization_batch,
            now=kwargs["now"],
        )

    def cycle(*args: Any) -> bool:
        order.append("cycle")
        return False

    with caplog.at_level("ERROR", logger="project3_crm.worker"):
        run_worker_loop(
            object(),
            object(),
            loop_settings(),
            object(),
            cycle=cycle,
            notification_check=notification_check,
            monotonic=lambda: 0,
            utcnow=lambda: NOW,
            sleep=lambda seconds: None,
            should_stop=lambda: "cycle" in order,
        )

    assert order == [
        "pause",
        "cluster",
        "crm_batch",
        "normalization_batch",
        "cycle",
    ]
    assert "CRM pause alert processing failed unexpectedly" in caplog.text
    assert private_marker not in caplog.text


def test_periodic_check_runs_before_cycle_at_startup_and_on_configured_cadence() -> None:
    clock_values = iter((0, 1, 299, 300, 301))
    events: list[tuple[str, int]] = []
    cycle_count = 0
    current_clock = -1
    sleeps: list[float] = []

    def monotonic() -> float:
        nonlocal current_clock
        current_clock = next(clock_values)
        return current_clock

    def notification_check(*args: Any, **kwargs: Any) -> bool:
        events.append(("notification", current_clock))
        return False

    def cycle(*args: Any) -> bool:
        nonlocal cycle_count
        cycle_count += 1
        events.append(("cycle", current_clock))
        return False

    run_worker_loop(
        object(),
        object(),
        loop_settings(),
        object(),
        cycle=cycle,
        notification_check=notification_check,
        sleep=sleeps.append,
        monotonic=monotonic,
        utcnow=lambda: NOW,
        should_stop=lambda: cycle_count >= 5,
    )

    assert events == [
        ("notification", 0),
        ("cycle", 0),
        ("cycle", 1),
        ("cycle", 299),
        ("notification", 300),
        ("cycle", 300),
        ("cycle", 301),
    ]
    assert sleeps == [1.5] * 5


def test_failed_immediate_pause_is_not_retried_until_periodic_cadence() -> None:
    engine = FakeWorkerEngine([])
    state = {"paused": False, "cycle_count": 0}
    attempts: list[str] = []
    clock_values = iter((0, 1, 300))

    def crm_work(*args: Any, **kwargs: Any) -> CrmDeliveryOutcome:
        state["cycle_count"] += 1
        if state["cycle_count"] == 1:
            state["paused"] = True
            return CrmDeliveryOutcome(CrmDeliveryAction.GLOBAL_PAUSE_ACTIVATED, 23)
        return CrmDeliveryOutcome(CrmDeliveryAction.NO_WORK)

    def immediate_alert(*args: Any, **kwargs: Any) -> AlertNotificationOutcome:
        attempts.append("immediate")
        return AlertNotificationOutcome(AlertNotificationAction.SEND_FAILED)

    def cycle(
        received_engine: Any,
        adapter: Any,
        settings: Any,
        sender: Any,
    ) -> bool:
        return run_worker_cycle(
            received_engine,
            adapter,
            settings,
            sender,
            crm_work=crm_work,
            pause_alert=immediate_alert,
            now=NOW,
        )

    def notification_check(*args: Any, **kwargs: Any) -> bool:
        if state["paused"]:
            attempts.append("periodic")
        return False

    run_worker_loop(
        engine,
        object(),
        loop_settings(),
        object(),
        cycle=cycle,
        notification_check=notification_check,
        monotonic=lambda: next(clock_values),
        utcnow=lambda: NOW,
        sleep=lambda seconds: None,
        should_stop=lambda: state["cycle_count"] >= 3,
    )

    assert attempts == ["immediate", "periodic"]
    assert state["paused"] is True


def test_due_but_idle_notification_check_still_uses_normal_idle_sleep() -> None:
    sleeps: list[float] = []
    cycle_calls = 0

    def cycle(*args: Any) -> bool:
        nonlocal cycle_calls
        cycle_calls += 1
        return False

    run_worker_loop(
        object(),
        object(),
        loop_settings(),
        object(),
        cycle=cycle,
        notification_check=lambda *args, **kwargs: False,
        monotonic=lambda: 0,
        utcnow=lambda: NOW,
        sleep=sleeps.append,
        should_stop=lambda: cycle_calls >= 1,
    )

    assert sleeps == [1.5]


def test_pending_cluster_retry_runs_only_on_periodic_notification_checks() -> None:
    cluster_attempts = 0
    cycle_calls = 0
    clock_values = iter((0, 1, 300))

    def cluster(*args: Any, **kwargs: Any) -> AlertNotificationOutcome:
        nonlocal cluster_attempts
        cluster_attempts += 1
        return AlertNotificationOutcome(AlertNotificationAction.SEND_FAILED)

    def notification_check(*args: Any, **kwargs: Any) -> bool:
        return run_notification_check(
            args[0],
            args[1],
            args[2],
            pause_alert=lambda *args, **kwargs: AlertNotificationOutcome(
                AlertNotificationAction.NO_ALERT
            ),
            cluster_alert=cluster,
            crm_failure_batch=lambda *args, **kwargs: BatchNotificationOutcome(
                BatchNotificationAction.NOT_DUE
            ),
            normalization_failure_batch=lambda *args, **kwargs: (
                BatchNotificationOutcome(BatchNotificationAction.NOT_DUE)
            ),
            now=kwargs["now"],
        )

    def cycle(*args: Any) -> bool:
        nonlocal cycle_calls
        cycle_calls += 1
        return False

    run_worker_loop(
        object(),
        object(),
        loop_settings(),
        object(),
        cycle=cycle,
        notification_check=notification_check,
        monotonic=lambda: next(clock_values),
        utcnow=lambda: NOW,
        sleep=lambda seconds: None,
        should_stop=lambda: cycle_calls >= 3,
    )

    assert cycle_calls == 3
    assert cluster_attempts == 2


def test_batch_stream_outcomes_do_not_short_circuit_each_other() -> None:
    calls: list[str] = []

    def crm_batch(*args: Any, **kwargs: Any) -> BatchNotificationOutcome:
        calls.append("crm")
        return BatchNotificationOutcome(BatchNotificationAction.NOT_DUE)

    def normalization_batch(*args: Any, **kwargs: Any) -> BatchNotificationOutcome:
        calls.append("normalization")
        return BatchNotificationOutcome(BatchNotificationAction.SEND_FAILED)

    useful = run_notification_check(
        object(),
        object(),
        loop_settings(),
        pause_alert=lambda *args, **kwargs: AlertNotificationOutcome(
            AlertNotificationAction.NO_ALERT
        ),
        cluster_alert=lambda *args, **kwargs: AlertNotificationOutcome(
            AlertNotificationAction.NO_ALERT
        ),
        crm_failure_batch=crm_batch,
        normalization_failure_batch=normalization_batch,
        now=NOW,
    )

    assert calls == ["crm", "normalization"]
    assert useful is True


@pytest.mark.parametrize("boundary", ["clock", "notification", "cycle", "sleep"])
def test_keyboard_interrupt_at_worker_boundaries_stops_cleanly(boundary: str) -> None:
    cycle_calls = 0
    notification_calls = 0
    sleep_calls = 0

    def monotonic() -> float:
        if boundary == "clock":
            raise KeyboardInterrupt
        return 0

    def notification_check(*args: Any, **kwargs: Any) -> bool:
        nonlocal notification_calls
        notification_calls += 1
        if boundary == "notification":
            raise KeyboardInterrupt
        return False

    def cycle(*args: Any) -> bool:
        nonlocal cycle_calls
        cycle_calls += 1
        if boundary == "cycle":
            raise KeyboardInterrupt
        return False

    def sleep(seconds: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if boundary == "sleep":
            raise KeyboardInterrupt

    run_worker_loop(
        object(),
        object(),
        loop_settings(),
        object(),
        cycle=cycle,
        notification_check=notification_check,
        monotonic=monotonic,
        utcnow=lambda: NOW,
        sleep=sleep,
        should_stop=lambda: sleep_calls >= 1,
    )

    assert cycle_calls <= 1
    assert notification_calls <= 1
    assert sleep_calls <= 1
