from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.dml import Update
from sqlalchemy.sql.selectable import Select

from project3_crm.config import Settings
from project3_crm.notifications.service import (
    BatchNotificationAction,
    NotificationStateError,
    process_crm_failure_batch,
    process_normalization_failure_batch,
)
from project3_crm.notifications.smtp import (
    NotificationMessage,
    NotificationSendCategory,
    NotificationSendResult,
)


NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": "postgresql://test:test@localhost/project3_test",
        "website_hmac_secret": "fake-website-secret",
        "linkedin_bearer_token": "fake-linkedin-token",
        "partner_api_key": "fake-partner-key",
        "hubspot_service_key": "fake-hubspot-key",
        "smtp_host": "smtp.example.test",
        "smtp_username": "fake-smtp-user",
        "smtp_password": "fake-smtp-password",
        "smtp_from_email": "sender@example.test",
        "ops_email_to": "ops@example.test",
        "automation_email_to": "automation@example.test",
    }
    values.update(overrides)
    return Settings(**values, _env_file=None)


def runtime_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": 1,
        "crm_delivery_paused": False,
        "pause_reason": None,
        "paused_at": None,
        "pause_alert_sent_at": None,
        "pause_alert_last_error": None,
        "last_crm_batch_attempt_at": None,
        "last_crm_batch_success_at": None,
        "last_crm_batch_error": None,
        "last_normalization_batch_attempt_at": None,
        "last_normalization_batch_success_at": None,
        "last_normalization_batch_error": None,
        "updated_at": NOW - timedelta(days=1),
    }
    row.update(overrides)
    return row


def crm_row(
    row_id: int,
    status: str,
    *,
    failure_at: datetime | None = None,
    notified_at: datetime | None = None,
) -> dict[str, Any]:
    return {
        "id": row_id,
        "integration_reference": f"IR-{row_id:05d}",
        "source": "website",
        "source_event_id": f"WEB-{row_id}",
        "delivery_status": status,
        "write_attempt_count": 2,
        "first_failure_at": NOW - timedelta(hours=2),
        "last_failure_at": NOW - timedelta(hours=1),
        "last_attempt_at": NOW - timedelta(hours=1),
        "failure_at": failure_at or NOW - timedelta(hours=1),
        "last_error_category": "SAFE_CATEGORY",
        "last_error_code": "SAFE_CODE",
        "last_error": "safe diagnostic",
        "crm_correlation_id": f"corr-{row_id}",
        "escalation_notified_at": notified_at,
        "source_metadata": {"private": "must-not-appear"},
        "email": "private.person@example.test",
    }


def webhook_row(
    row_id: int,
    status: str,
    *,
    completed_at: datetime | None = None,
    notified_at: datetime | None = None,
) -> dict[str, Any]:
    return {
        "id": row_id,
        "source": "partner",
        "event_id": f"PARTNER-{row_id}",
        "event_status": status,
        "received_at": NOW - timedelta(hours=2),
        "completed_at": completed_at or NOW - timedelta(hours=1),
        "last_error_category": "PROVIDER_VALIDATION",
        "last_error": "safe normalization diagnostic",
        "escalation_notified_at": notified_at,
        "raw_payload": {
            "email": "private.person@example.test",
            "name": "Private Person",
        },
    }


class FakeResult:
    def __init__(
        self,
        *,
        mapping: dict[str, Any] | None = None,
        rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self.mapping = mapping
        self.rows = rows or []

    def mappings(self) -> FakeResult:
        return self

    def one_or_none(self) -> dict[str, Any] | None:
        return deepcopy(self.mapping)

    def all(self) -> list[dict[str, Any]]:
        return deepcopy(self.rows)


@dataclass
class FakeNotificationEngine:
    runtime: dict[str, Any] | None = field(default_factory=runtime_row)
    crm_rows: list[dict[str, Any]] = field(default_factory=list)
    webhook_rows: list[dict[str, Any]] = field(default_factory=list)
    statements: list[Any] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    transaction_depth: int = 0
    fail_result_persistence: bool = False

    def begin(self) -> FakeTransaction:
        return FakeTransaction(self)


class FakeTransaction:
    def __init__(self, engine: FakeNotificationEngine) -> None:
        self.engine = engine
        self.snapshot: tuple[Any, Any, Any] | None = None

    def __enter__(self) -> FakeConnection:
        self.snapshot = (
            deepcopy(self.engine.runtime),
            deepcopy(self.engine.crm_rows),
            deepcopy(self.engine.webhook_rows),
        )
        self.engine.transaction_depth += 1
        self.engine.events.append("begin")
        return FakeConnection(self.engine)

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        self.engine.transaction_depth -= 1
        if exc_type is not None:
            assert self.snapshot is not None
            self.engine.runtime, self.engine.crm_rows, self.engine.webhook_rows = (
                self.snapshot
            )
            self.engine.events.append("rollback")
            return False
        self.engine.events.append("commit")
        return False


class FakeConnection:
    def __init__(self, engine: FakeNotificationEngine) -> None:
        self.engine = engine

    def execute(self, statement: Any) -> FakeResult:
        self.engine.statements.append(statement)
        compiled = statement.compile(dialect=postgresql.dialect())
        sql = str(compiled)
        parameters = compiled.params

        if isinstance(statement, Select) and "integration_runtime_state" in sql:
            return FakeResult(mapping=self.engine.runtime)

        if isinstance(statement, Select) and "FROM normalized_leads" in sql:
            rows = [
                row
                for row in self.engine.crm_rows
                if row["delivery_status"] in {"FAILED_ESCALATED", "UNKNOWN_ESCALATED"}
                and row["escalation_notified_at"] is None
            ]
            rows.sort(key=lambda row: (row["failure_at"], row["id"]))
            limit = next(
                (
                    value
                    for key, value in parameters.items()
                    if key.startswith("param_") and isinstance(value, int)
                ),
                None,
            )
            if limit is not None:
                rows = rows[:limit]
            return FakeResult(rows=rows)

        if isinstance(statement, Select) and "FROM webhook_events" in sql:
            rows = [
                row
                for row in self.engine.webhook_rows
                if row["event_status"] == "NORMALIZATION_FAILED"
                and row["escalation_notified_at"] is None
            ]
            rows.sort(key=lambda row: (row["completed_at"], row["id"]))
            limit = next(
                (
                    value
                    for key, value in parameters.items()
                    if key.startswith("param_") and isinstance(value, int)
                ),
                None,
            )
            if limit is not None:
                rows = rows[:limit]
            return FakeResult(rows=rows)

        if isinstance(statement, Update) and "integration_runtime_state" in sql:
            assert self.engine.runtime is not None
            for key in self.engine.runtime:
                if key in parameters:
                    self.engine.runtime[key] = parameters[key]
            return FakeResult()

        if isinstance(statement, Update) and "UPDATE normalized_leads" in sql:
            if self.engine.fail_result_persistence:
                self.engine.fail_result_persistence = False
                raise RuntimeError("simulated result persistence failure")
            selected_ids = next(
                value
                for value in parameters.values()
                if isinstance(value, list)
                and all(isinstance(item, int) for item in value)
            )
            for row in self.engine.crm_rows:
                if (
                    row["id"] in selected_ids
                    and row["delivery_status"]
                    in {"FAILED_ESCALATED", "UNKNOWN_ESCALATED"}
                    and row["escalation_notified_at"] is None
                ):
                    row["escalation_notified_at"] = parameters[
                        "escalation_notified_at"
                    ]
            return FakeResult()

        if isinstance(statement, Update) and "UPDATE webhook_events" in sql:
            if self.engine.fail_result_persistence:
                self.engine.fail_result_persistence = False
                raise RuntimeError("simulated result persistence failure")
            selected_ids = next(
                value
                for value in parameters.values()
                if isinstance(value, list)
                and all(isinstance(item, int) for item in value)
            )
            for row in self.engine.webhook_rows:
                if (
                    row["id"] in selected_ids
                    and row["event_status"] == "NORMALIZATION_FAILED"
                    and row["escalation_notified_at"] is None
                ):
                    row["escalation_notified_at"] = parameters[
                        "escalation_notified_at"
                    ]
            return FakeResult()

        raise AssertionError(f"unexpected SQL: {sql}")


class FakeSender:
    def __init__(
        self,
        engine: FakeNotificationEngine,
        *results: NotificationSendResult,
    ) -> None:
        self.engine = engine
        self.results = list(results) or [send_result(NotificationSendCategory.SUCCESS)]
        self.messages: list[NotificationMessage] = []

    def send(self, notification: NotificationMessage) -> NotificationSendResult:
        assert self.engine.transaction_depth == 0
        self.engine.events.append("smtp_send")
        self.messages.append(notification)
        return self.results.pop(0)


def send_result(
    category: NotificationSendCategory,
    description: str = "safe SMTP diagnostic",
) -> NotificationSendResult:
    return NotificationSendResult(category, description, 450)


def test_crm_batch_selects_only_terminal_unnotified_rows_in_stable_order() -> None:
    tie_time = NOW - timedelta(hours=3)
    rows = [
        crm_row(5, "SUCCESS"),
        crm_row(4, "EXISTING_CONTACT"),
        crm_row(3, "UNKNOWN_ESCALATED", failure_at=tie_time),
        crm_row(2, "FAILED_ESCALATED", failure_at=tie_time),
        crm_row(1, "UNKNOWN"),
        crm_row(6, "FAILED_ESCALATED", notified_at=NOW - timedelta(days=1)),
    ]
    engine = FakeNotificationEngine(crm_rows=rows)
    sender = FakeSender(engine)
    other_stream = {
        key: value
        for key, value in engine.runtime.items()
        if key.startswith("last_normalization_batch_")
    }

    outcome = process_crm_failure_batch(engine, sender, settings(), now=NOW)

    assert outcome.action is BatchNotificationAction.SENT
    assert outcome.item_count == 2
    assert len(sender.messages) == 1
    notification = sender.messages[0]
    assert notification.to == ("ops@example.test",)
    assert notification.cc == ("automation@example.test",)
    assert notification.body.index("IR-00002") < notification.body.index("IR-00003")
    assert "safe diagnostic" in notification.body
    assert "private.person@example.test" not in notification.body
    assert "must-not-appear" not in notification.body
    assert [row["id"] for row in rows if row["escalation_notified_at"] == NOW] == [3, 2]
    assert engine.runtime["last_crm_batch_attempt_at"] == NOW
    assert engine.runtime["last_crm_batch_success_at"] == NOW
    assert engine.runtime["last_crm_batch_error"] is None
    assert {
        key: value
        for key, value in engine.runtime.items()
        if key.startswith("last_normalization_batch_")
    } == other_stream
    selection_sql = next(
        str(statement.compile(dialect=postgresql.dialect()))
        for statement in engine.statements
        if isinstance(statement, Select) and "FROM normalized_leads" in str(statement)
    )
    assert "failure_at ASC, normalized_leads.id ASC" in selection_sql


def test_normalization_batch_selects_only_failed_unnotified_events_safely() -> None:
    tie_time = NOW - timedelta(hours=3)
    rows = [
        webhook_row(5, "RECEIVED"),
        webhook_row(4, "PROCESSING"),
        webhook_row(3, "NORMALIZATION_FAILED", completed_at=tie_time),
        webhook_row(2, "NORMALIZATION_FAILED", completed_at=tie_time),
        webhook_row(1, "NORMALIZED"),
        webhook_row(6, "NORMALIZATION_FAILED", notified_at=NOW - timedelta(days=1)),
    ]
    engine = FakeNotificationEngine(webhook_rows=rows)
    sender = FakeSender(engine)
    other_stream = {
        key: value
        for key, value in engine.runtime.items()
        if key.startswith("last_crm_batch_")
    }

    outcome = process_normalization_failure_batch(engine, sender, settings(), now=NOW)

    assert outcome.action is BatchNotificationAction.SENT
    assert outcome.item_count == 2
    notification = sender.messages[0]
    assert notification.to == ("automation@example.test",)
    assert notification.cc == ()
    assert notification.body.index("PARTNER-2") < notification.body.index("PARTNER-3")
    assert "safe normalization diagnostic" in notification.body
    assert "private.person@example.test" not in notification.body
    assert "Private Person" not in notification.body
    assert [row["id"] for row in rows if row["escalation_notified_at"] == NOW] == [3, 2]
    assert {
        key: value
        for key, value in engine.runtime.items()
        if key.startswith("last_crm_batch_")
    } == other_stream
    selection_sql = next(
        str(statement.compile(dialect=postgresql.dialect()))
        for statement in engine.statements
        if isinstance(statement, Select) and "FROM webhook_events" in str(statement)
    )
    assert "completed_at ASC, webhook_events.id ASC" in selection_sql


def test_crm_batch_limit_marks_only_selected_rows_and_leaves_remainder() -> None:
    rows = [
        crm_row(1, "FAILED_ESCALATED", failure_at=NOW - timedelta(hours=1)),
        crm_row(3, "FAILED_ESCALATED", failure_at=NOW - timedelta(hours=3)),
        crm_row(2, "UNKNOWN_ESCALATED", failure_at=NOW - timedelta(hours=2)),
    ]
    engine = FakeNotificationEngine(crm_rows=rows)
    sender = FakeSender(
        engine,
        send_result(NotificationSendCategory.SUCCESS),
        send_result(NotificationSendCategory.SUCCESS),
    )
    batch_settings = settings(
        notification_batch_max_items=2,
        notification_batch_interval_seconds=7_200,
    )

    first = process_crm_failure_batch(engine, sender, batch_settings, now=NOW)

    assert first.action is BatchNotificationAction.SENT
    assert first.item_count == 2
    assert "IR-00003" in sender.messages[0].body
    assert "IR-00002" in sender.messages[0].body
    assert "IR-00001" not in sender.messages[0].body
    assert next(row for row in rows if row["id"] == 1)[
        "escalation_notified_at"
    ] is None
    assert {
        row["id"]
        for row in rows
        if row["escalation_notified_at"] == NOW
    } == {2, 3}

    second_time = NOW + timedelta(seconds=7_200)
    second = process_crm_failure_batch(
        engine,
        sender,
        batch_settings,
        now=second_time,
    )

    assert second.action is BatchNotificationAction.SENT
    assert second.item_count == 1
    assert next(row for row in rows if row["id"] == 1)[
        "escalation_notified_at"
    ] == second_time


def test_normalization_batch_limit_marks_only_selected_events() -> None:
    rows = [
        webhook_row(
            1,
            "NORMALIZATION_FAILED",
            completed_at=NOW - timedelta(hours=1),
        ),
        webhook_row(
            3,
            "NORMALIZATION_FAILED",
            completed_at=NOW - timedelta(hours=3),
        ),
        webhook_row(
            2,
            "NORMALIZATION_FAILED",
            completed_at=NOW - timedelta(hours=2),
        ),
    ]
    engine = FakeNotificationEngine(webhook_rows=rows)
    sender = FakeSender(engine)

    outcome = process_normalization_failure_batch(
        engine,
        sender,
        settings(notification_batch_max_items=2),
        now=NOW,
    )

    assert outcome.action is BatchNotificationAction.SENT
    assert outcome.item_count == 2
    assert "PARTNER-3" in sender.messages[0].body
    assert "PARTNER-2" in sender.messages[0].body
    assert "PARTNER-1" not in sender.messages[0].body
    assert next(row for row in rows if row["id"] == 1)[
        "escalation_notified_at"
    ] is None
    assert {
        row["id"]
        for row in rows
        if row["escalation_notified_at"] == NOW
    } == {2, 3}

    selection_sql = next(
        str(statement.compile(dialect=postgresql.dialect()))
        for statement in engine.statements
        if isinstance(statement, Select) and "FROM webhook_events" in str(statement)
    )
    assert "LIMIT" in selection_sql


@pytest.mark.parametrize(
    ("processor", "row_factory", "row_attribute", "error_field"),
    [
        (
            process_crm_failure_batch,
            lambda: crm_row(1, "FAILED_ESCALATED"),
            "crm_rows",
            "last_crm_batch_error",
        ),
        (
            process_normalization_failure_batch,
            lambda: webhook_row(1, "NORMALIZATION_FAILED"),
            "webhook_rows",
            "last_normalization_batch_error",
        ),
    ],
)
def test_failed_send_marks_no_items_and_retries_only_after_failure_interval(
    processor: Any,
    row_factory: Any,
    row_attribute: str,
    error_field: str,
) -> None:
    engine = FakeNotificationEngine()
    rows = [row_factory()]
    setattr(engine, row_attribute, rows)
    stream_prefix = error_field.removesuffix("error")
    success_field = f"{stream_prefix}success_at"
    attempt_field = f"{stream_prefix}attempt_at"
    previous_success = NOW - timedelta(seconds=7_201)
    engine.runtime[success_field] = previous_success
    engine.runtime[attempt_field] = previous_success
    sender = FakeSender(
        engine,
        send_result(NotificationSendCategory.TEMPORARY_FAILURE, "safe failure"),
        send_result(NotificationSendCategory.SUCCESS),
    )

    failed = processor(engine, sender, settings(), now=NOW)
    assert engine.runtime[success_field] == previous_success
    too_early = processor(engine, sender, settings(), now=NOW + timedelta(seconds=299))
    retried = processor(engine, sender, settings(), now=NOW + timedelta(seconds=300))

    assert failed.action is BatchNotificationAction.SEND_FAILED
    assert too_early.action is BatchNotificationAction.NOT_DUE
    assert retried.action is BatchNotificationAction.SENT
    assert len(sender.messages) == 2
    assert rows[0]["escalation_notified_at"] == NOW + timedelta(seconds=300)
    assert engine.runtime[error_field] is None


@pytest.mark.parametrize(
    ("processor", "row_factory", "row_attribute", "success_field"),
    [
        (
            process_crm_failure_batch,
            lambda: crm_row(1, "FAILED_ESCALATED"),
            "crm_rows",
            "last_crm_batch_success_at",
        ),
        (
            process_normalization_failure_batch,
            lambda: webhook_row(1, "NORMALIZATION_FAILED"),
            "webhook_rows",
            "last_normalization_batch_success_at",
        ),
    ],
)
def test_successful_stream_waits_normal_interval(
    processor: Any,
    row_factory: Any,
    row_attribute: str,
    success_field: str,
) -> None:
    engine = FakeNotificationEngine()
    setattr(engine, row_attribute, [row_factory()])
    sender = FakeSender(engine)

    sent = processor(engine, sender, settings(), now=NOW)
    before_due = processor(engine, sender, settings(), now=NOW + timedelta(seconds=7_199))
    at_due = processor(engine, sender, settings(), now=NOW + timedelta(seconds=7_200))

    assert sent.action is BatchNotificationAction.SENT
    assert before_due.action is BatchNotificationAction.NOT_DUE
    assert at_due.action is BatchNotificationAction.EMPTY
    assert len(sender.messages) == 1
    assert engine.runtime[success_field] == NOW + timedelta(seconds=7_200)


def test_streams_are_due_independently() -> None:
    engine = FakeNotificationEngine(
        runtime=runtime_row(
            last_crm_batch_attempt_at=NOW,
            last_crm_batch_success_at=NOW,
            last_crm_batch_error=None,
        ),
        crm_rows=[crm_row(1, "FAILED_ESCALATED")],
        webhook_rows=[webhook_row(2, "NORMALIZATION_FAILED")],
    )
    sender = FakeSender(engine)

    crm = process_crm_failure_batch(engine, sender, settings(), now=NOW)
    normalization = process_normalization_failure_batch(
        engine,
        sender,
        settings(),
        now=NOW,
    )

    assert crm.action is BatchNotificationAction.NOT_DUE
    assert normalization.action is BatchNotificationAction.SENT
    assert len(sender.messages) == 1
    assert engine.crm_rows[0]["escalation_notified_at"] is None
    assert engine.webhook_rows[0]["escalation_notified_at"] == NOW


def test_crm_can_be_due_while_normalization_is_not_due() -> None:
    engine = FakeNotificationEngine(
        runtime=runtime_row(
            last_normalization_batch_attempt_at=NOW,
            last_normalization_batch_success_at=NOW,
            last_normalization_batch_error=None,
        ),
        crm_rows=[crm_row(1, "FAILED_ESCALATED")],
        webhook_rows=[webhook_row(2, "NORMALIZATION_FAILED")],
    )
    sender = FakeSender(engine)

    normalization = process_normalization_failure_batch(
        engine,
        sender,
        settings(),
        now=NOW,
    )
    crm = process_crm_failure_batch(engine, sender, settings(), now=NOW)

    assert normalization.action is BatchNotificationAction.NOT_DUE
    assert crm.action is BatchNotificationAction.SENT
    assert len(sender.messages) == 1
    assert engine.webhook_rows[0]["escalation_notified_at"] is None
    assert engine.crm_rows[0]["escalation_notified_at"] == NOW


@pytest.mark.parametrize(
    ("processor", "other_prefix"),
    [
        (process_crm_failure_batch, "last_normalization_batch_"),
        (process_normalization_failure_batch, "last_crm_batch_"),
    ],
)
def test_due_empty_batch_advances_only_its_stream_without_smtp(
    processor: Any,
    other_prefix: str,
) -> None:
    engine = FakeNotificationEngine()
    sender = FakeSender(engine)
    other_before = {
        key: value
        for key, value in engine.runtime.items()
        if key.startswith(other_prefix)
    }

    outcome = processor(engine, sender, settings(), now=NOW)

    assert outcome.action is BatchNotificationAction.EMPTY
    assert sender.messages == []
    assert {
        key: value
        for key, value in engine.runtime.items()
        if key.startswith(other_prefix)
    } == other_before


def test_success_commit_failure_preserves_at_least_once_resend_behavior() -> None:
    engine = FakeNotificationEngine(
        crm_rows=[crm_row(1, "FAILED_ESCALATED")],
        fail_result_persistence=True,
    )
    sender = FakeSender(
        engine,
        send_result(NotificationSendCategory.SUCCESS),
        send_result(NotificationSendCategory.SUCCESS),
    )

    with pytest.raises(RuntimeError, match="result persistence failure"):
        process_crm_failure_batch(engine, sender, settings(), now=NOW)

    assert engine.crm_rows[0]["escalation_notified_at"] is None
    assert engine.runtime["last_crm_batch_attempt_at"] == NOW
    assert engine.runtime["last_crm_batch_success_at"] is None

    outcome = process_crm_failure_batch(
        engine,
        sender,
        settings(),
        now=NOW + timedelta(seconds=300),
    )

    assert outcome.action is BatchNotificationAction.SENT
    assert len(sender.messages) == 2


def test_missing_runtime_singleton_fails_before_smtp() -> None:
    engine = FakeNotificationEngine(runtime=None)
    sender = FakeSender(engine)

    with pytest.raises(NotificationStateError, match="singleton id=1 is missing"):
        process_crm_failure_batch(engine, sender, settings(), now=NOW)

    assert sender.messages == []


def test_persisted_failure_diagnostic_is_bounded_and_secret_free() -> None:
    secret = "OBVIOUS-FAKE-SMTP-PASSWORD"
    engine = FakeNotificationEngine(crm_rows=[crm_row(1, "FAILED_ESCALATED")])
    sender = FakeSender(
        engine,
        send_result(
            NotificationSendCategory.PERMANENT_FAILURE,
            "safe" * 400,
        ),
    )

    outcome = process_crm_failure_batch(engine, sender, settings(), now=NOW)

    assert outcome.action is BatchNotificationAction.SEND_FAILED
    persisted = engine.runtime["last_crm_batch_error"]
    assert len(persisted) == 1_000
    assert secret not in persisted
