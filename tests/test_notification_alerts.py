from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.dml import Update
from sqlalchemy.sql.selectable import Select

from project3_crm.config import Settings
from project3_crm.notifications.service import (
    AlertNotificationAction,
    NotificationStateError,
    process_cluster_technical_alert,
    process_crm_pause_alert,
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
        "hubspot_service_key": "OBVIOUS-FAKE-HUBSPOT-KEY",
        "smtp_host": "smtp.example.test",
        "smtp_username": "fake-smtp-user",
        "smtp_password": "OBVIOUS-FAKE-SMTP-PASSWORD",
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
        "updated_at": NOW - timedelta(days=1),
    }
    row.update(overrides)
    return row


def cluster_row(
    row_id: int,
    *,
    failure_at: datetime | None,
    alerted_at: datetime | None = None,
    pending_at: datetime | None = None,
    status: str = "UNKNOWN",
) -> dict[str, Any]:
    return {
        "id": row_id,
        "integration_reference": f"IR-{row_id:05d}",
        "delivery_status": status,
        "last_technical_failure_at": failure_at,
        "cluster_alerted_at": alerted_at,
        "cluster_alert_pending_at": pending_at,
        "last_error_category": "SAFE_TECHNICAL_CATEGORY",
        "email": "private.person@example.test",
        "phone": "private-phone",
        "first_name": "Private",
        "last_name": "Person",
        "source_metadata": {"private": "must-not-appear"},
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
class FakeAlertEngine:
    runtime: dict[str, Any] | None = field(default_factory=runtime_row)
    rows: list[dict[str, Any]] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    statements: list[Any] = field(default_factory=list)
    transaction_depth: int = 0

    def begin(self) -> FakeTransaction:
        return FakeTransaction(self)


class FakeTransaction:
    def __init__(self, engine: FakeAlertEngine) -> None:
        self.engine = engine
        self.snapshot: tuple[Any, Any] | None = None

    def __enter__(self) -> FakeConnection:
        self.snapshot = (deepcopy(self.engine.runtime), deepcopy(self.engine.rows))
        self.engine.transaction_depth += 1
        self.engine.events.append("begin")
        return FakeConnection(self.engine)

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        self.engine.transaction_depth -= 1
        if exc_type is not None:
            assert self.snapshot is not None
            self.engine.runtime, self.engine.rows = self.snapshot
            self.engine.events.append("rollback")
            return False
        self.engine.events.append("commit")
        return False


class FakeConnection:
    def __init__(self, engine: FakeAlertEngine) -> None:
        self.engine = engine

    def execute(self, statement: Any) -> FakeResult:
        self.engine.statements.append(statement)
        compiled = statement.compile(dialect=postgresql.dialect())
        sql = str(compiled)
        parameters = compiled.params

        if isinstance(statement, Select) and "integration_runtime_state" in sql:
            return FakeResult(mapping=self.engine.runtime)

        if isinstance(statement, Select) and "FROM normalized_leads" in sql:
            if "cluster_alert_pending_at IS NOT NULL" in sql:
                pending = [row for row in self.engine.rows if row["cluster_alert_pending_at"] is not None]
                pending.sort(key=lambda row: (row["cluster_alert_pending_at"], row["id"]))
                selected = pending[0] if pending else None
                return FakeResult(
                    mapping=(
                        {"cluster_alert_pending_at": selected["cluster_alert_pending_at"]}
                        if selected
                        else None
                    )
                )

            if "last_technical_failure_at >=" in sql:
                cutoff = parameters["last_technical_failure_at_1"]
                rows = [
                    row
                    for row in self.engine.rows
                    if row["last_technical_failure_at"] is not None
                    and row["last_technical_failure_at"] >= cutoff
                    and row["cluster_alert_pending_at"] is None
                    and (
                        row["cluster_alerted_at"] is None
                        or row["last_technical_failure_at"] > row["cluster_alerted_at"]
                    )
                ]
                rows.sort(key=lambda row: (row["last_technical_failure_at"], row["id"]))
                return FakeResult(rows=rows)

            pending_at = next(
                value
                for key, value in parameters.items()
                if key.startswith("cluster_alert_pending_at_")
            )
            rows = [
                row
                for row in self.engine.rows
                if row["cluster_alert_pending_at"] == pending_at
            ]
            id_values = next(
                (
                    value
                    for value in parameters.values()
                    if isinstance(value, list)
                    and all(isinstance(item, int) for item in value)
                ),
                None,
            )
            if id_values is not None:
                rows = [row for row in rows if row["id"] in id_values]
            rows.sort(key=lambda row: row["id"])
            if "integration_reference" not in sql:
                rows = [{"id": row["id"]} for row in rows]
            return FakeResult(rows=rows)

        if isinstance(statement, Update) and "integration_runtime_state" in sql:
            assert self.engine.runtime is not None
            for key in self.engine.runtime:
                if key in parameters:
                    self.engine.runtime[key] = parameters[key]
            return FakeResult()

        if isinstance(statement, Update) and "UPDATE normalized_leads" in sql:
            selected_ids = next(
                value
                for value in parameters.values()
                if isinstance(value, list)
                and all(isinstance(item, int) for item in value)
            )
            if "cluster_alerted_at" in parameters:
                expected_pending = next(
                    value
                    for key, value in parameters.items()
                    if key.startswith("cluster_alert_pending_at_")
                )
                for row in self.engine.rows:
                    if (
                        row["id"] in selected_ids
                        and row["cluster_alert_pending_at"] == expected_pending
                    ):
                        row["cluster_alerted_at"] = parameters["cluster_alerted_at"]
                        row["cluster_alert_pending_at"] = parameters[
                            "cluster_alert_pending_at"
                        ]
            else:
                pending_at = parameters["cluster_alert_pending_at"]
                for row in self.engine.rows:
                    if row["id"] in selected_ids and row["cluster_alert_pending_at"] is None:
                        row["cluster_alert_pending_at"] = pending_at
            return FakeResult()

        raise AssertionError(f"unexpected SQL: {sql}")


class FakeSender:
    def __init__(
        self,
        engine: FakeAlertEngine,
        *results: NotificationSendResult,
        during_send: Callable[[], None] | None = None,
    ) -> None:
        self.engine = engine
        self.results = list(results) or [send_result(NotificationSendCategory.SUCCESS)]
        self.during_send = during_send
        self.messages: list[NotificationMessage] = []

    def send(self, notification: NotificationMessage) -> NotificationSendResult:
        assert self.engine.transaction_depth == 0
        self.engine.events.append("smtp_send")
        self.messages.append(notification)
        if self.during_send is not None:
            self.during_send()
        return self.results.pop(0)


def send_result(
    category: NotificationSendCategory,
    description: str = "safe SMTP diagnostic",
) -> NotificationSendResult:
    return NotificationSendResult(category, description, 450)


def test_unpaused_or_already_alerted_pause_sends_nothing() -> None:
    unpaused = FakeAlertEngine()
    alerted = FakeAlertEngine(
        runtime=runtime_row(
            crm_delivery_paused=True,
            pause_reason="AUTH",
            paused_at=NOW,
            pause_alert_sent_at=NOW,
        )
    )

    unpaused_sender = FakeSender(unpaused)
    alerted_sender = FakeSender(alerted)
    assert process_crm_pause_alert(unpaused, unpaused_sender, settings(), now=NOW).action is AlertNotificationAction.NO_ALERT
    assert process_crm_pause_alert(alerted, alerted_sender, settings(), now=NOW).action is AlertNotificationAction.NO_ALERT
    assert unpaused_sender.messages == []
    assert alerted_sender.messages == []


@pytest.mark.parametrize("reason", ["AUTH", "CONFIG"])
def test_pause_alert_success_marks_same_episode_only(reason: str) -> None:
    paused_at = NOW - timedelta(minutes=5)
    engine = FakeAlertEngine(
        runtime=runtime_row(
            crm_delivery_paused=True,
            pause_reason=reason,
            paused_at=paused_at,
            pause_alert_last_error="old safe failure",
        )
    )
    sender = FakeSender(engine)

    outcome = process_crm_pause_alert(engine, sender, settings(), now=NOW)

    assert outcome.action is AlertNotificationAction.SENT
    assert len(sender.messages) == 1
    message = sender.messages[0]
    assert message.to == ("automation@example.test",)
    assert message.cc == ("ops@example.test",)
    assert f"pause_reason: {reason}" in message.body
    assert engine.runtime["crm_delivery_paused"] is True
    assert engine.runtime["pause_reason"] == reason
    assert engine.runtime["paused_at"] == paused_at
    assert engine.runtime["pause_alert_sent_at"] == NOW
    assert engine.runtime["pause_alert_last_error"] is None
    assert engine.events == ["begin", "commit", "smtp_send", "begin", "commit"]


def test_pause_alert_failure_preserves_pause_and_stores_safe_bounded_error() -> None:
    engine = FakeAlertEngine(
        runtime=runtime_row(
            crm_delivery_paused=True,
            pause_reason="AUTH",
            paused_at=NOW - timedelta(minutes=5),
        )
    )
    sender = FakeSender(
        engine,
        send_result(NotificationSendCategory.TEMPORARY_FAILURE, "safe" * 400),
    )

    outcome = process_crm_pause_alert(engine, sender, settings(), now=NOW)

    assert outcome.action is AlertNotificationAction.SEND_FAILED
    assert engine.runtime["crm_delivery_paused"] is True
    assert engine.runtime["pause_alert_sent_at"] is None
    assert len(engine.runtime["pause_alert_last_error"]) == 1_000
    assert outcome.safe_diagnostic == engine.runtime["pause_alert_last_error"]


@pytest.mark.parametrize("replacement", ["unpaused", "new_episode"])
def test_stale_pause_result_cannot_recreate_or_mark_another_episode(
    replacement: str,
) -> None:
    captured_at = NOW - timedelta(minutes=5)
    engine = FakeAlertEngine(
        runtime=runtime_row(
            crm_delivery_paused=True,
            pause_reason="AUTH",
            paused_at=captured_at,
        )
    )

    def replace_episode() -> None:
        if replacement == "unpaused":
            engine.runtime.update(
                crm_delivery_paused=False,
                pause_reason=None,
                paused_at=None,
                pause_alert_sent_at=None,
                pause_alert_last_error=None,
            )
        else:
            engine.runtime.update(
                crm_delivery_paused=True,
                pause_reason="CONFIG",
                paused_at=NOW,
                pause_alert_sent_at=None,
                pause_alert_last_error=None,
            )

    sender = FakeSender(engine, during_send=replace_episode)

    outcome = process_crm_pause_alert(engine, sender, settings(), now=NOW)

    assert outcome.action is AlertNotificationAction.STALE_EPISODE
    assert engine.runtime["pause_alert_sent_at"] is None
    if replacement == "unpaused":
        assert engine.runtime["crm_delivery_paused"] is False
    else:
        assert engine.runtime["pause_reason"] == "CONFIG"
        assert engine.runtime["paused_at"] == NOW


def test_pause_alert_requires_runtime_singleton() -> None:
    engine = FakeAlertEngine(runtime=None)
    sender = FakeSender(engine)

    with pytest.raises(NotificationStateError, match="singleton id=1 is missing"):
        process_crm_pause_alert(engine, sender, settings(), now=NOW)

    assert sender.messages == []


def qualifying_rows(count: int, *, start_id: int = 1) -> list[dict[str, Any]]:
    return [
        cluster_row(
            row_id,
            failure_at=NOW - timedelta(seconds=count - index),
        )
        for index, row_id in enumerate(range(start_id, start_id + count))
    ]


def test_cluster_below_threshold_sends_nothing_and_mutates_nothing() -> None:
    engine = FakeAlertEngine(rows=qualifying_rows(4))
    original = deepcopy(engine.rows)
    sender = FakeSender(engine)

    outcome = process_cluster_technical_alert(engine, sender, settings(), now=NOW)

    assert outcome.action is AlertNotificationAction.NO_ALERT
    assert sender.messages == []
    assert engine.rows == original


@pytest.mark.parametrize("count", [5, 7])
def test_cluster_threshold_freezes_all_qualifying_leads_before_one_send(
    count: int,
) -> None:
    engine = FakeAlertEngine(rows=qualifying_rows(count))

    def assert_frozen() -> None:
        assert all(row["cluster_alert_pending_at"] == NOW for row in engine.rows)
        assert engine.events[-1] == "smtp_send"
        assert "commit" in engine.events[:-1]

    sender = FakeSender(engine, during_send=assert_frozen)

    outcome = process_cluster_technical_alert(engine, sender, settings(), now=NOW)

    assert outcome.action is AlertNotificationAction.SENT
    assert outcome.item_count == count
    assert len(sender.messages) == 1
    assert all(row["cluster_alert_pending_at"] is None for row in engine.rows)
    assert all(row["cluster_alerted_at"] == NOW for row in engine.rows)
    body = sender.messages[0].body
    assert f"affected_lead_count: {count}" in body
    assert "private.person@example.test" not in body
    assert "private-phone" not in body
    assert "Private" not in body
    assert "must-not-appear" not in body
    assert "OBVIOUS-FAKE-SMTP-PASSWORD" not in body
    assert "OBVIOUS-FAKE-HUBSPOT-KEY" not in body


def test_cluster_qualification_uses_timestamp_window_and_prior_alert_only() -> None:
    rows = qualifying_rows(5)
    rows.extend(
        [
            cluster_row(10, failure_at=NOW - timedelta(seconds=601)),
            cluster_row(11, failure_at=NOW, alerted_at=NOW),
            cluster_row(12, failure_at=NOW, alerted_at=NOW + timedelta(seconds=1)),
        ]
    )
    excluded_before = deepcopy(rows[5:])
    engine = FakeAlertEngine(rows=rows)
    sender = FakeSender(engine)

    outcome = process_cluster_technical_alert(engine, sender, settings(), now=NOW)

    assert outcome.action is AlertNotificationAction.SENT
    assert outcome.item_count == 5
    assert all(engine.rows[index]["cluster_alerted_at"] == NOW for index in range(5))
    assert engine.rows[5:] == excluded_before


def test_oldest_existing_pending_episode_retries_before_new_detection() -> None:
    oldest = NOW - timedelta(hours=2)
    newer = NOW - timedelta(hours=1)
    old_pending = cluster_row(
        20,
        failure_at=NOW - timedelta(days=1),
        pending_at=oldest,
        status="SUCCESS",
    )
    newer_pending = cluster_row(21, failure_at=NOW, pending_at=newer)
    engine = FakeAlertEngine(rows=[*qualifying_rows(5), newer_pending, old_pending])
    sender = FakeSender(engine)

    outcome = process_cluster_technical_alert(engine, sender, settings(), now=NOW)

    assert outcome.action is AlertNotificationAction.SENT
    assert outcome.item_count == 1
    assert len(sender.messages) == 1
    assert "normalized_lead_ids: 20" in sender.messages[0].body
    assert old_pending["cluster_alert_pending_at"] is None
    assert old_pending["cluster_alerted_at"] == oldest
    assert newer_pending["cluster_alert_pending_at"] == newer
    assert all(row["cluster_alert_pending_at"] is None for row in engine.rows[:5])


def test_failed_cluster_send_keeps_episode_and_next_call_retries_it() -> None:
    engine = FakeAlertEngine(rows=qualifying_rows(5))
    sender = FakeSender(
        engine,
        send_result(NotificationSendCategory.TEMPORARY_FAILURE),
        send_result(NotificationSendCategory.SUCCESS),
    )

    failed = process_cluster_technical_alert(engine, sender, settings(), now=NOW)
    assert failed.action is AlertNotificationAction.SEND_FAILED
    assert all(row["cluster_alert_pending_at"] == NOW for row in engine.rows)
    assert all(row["cluster_alerted_at"] is None for row in engine.rows)

    retried = process_cluster_technical_alert(
        engine,
        sender,
        settings(),
        now=NOW + timedelta(hours=1),
    )
    assert retried.action is AlertNotificationAction.SENT
    assert len(sender.messages) == 2
    assert all(row["cluster_alerted_at"] == NOW for row in engine.rows)


def test_new_failure_during_smtp_remains_newer_than_frozen_alert_timestamp() -> None:
    engine = FakeAlertEngine(rows=qualifying_rows(5))
    newer_failure = NOW + timedelta(seconds=5)

    def record_new_failure() -> None:
        engine.rows[0]["last_technical_failure_at"] = newer_failure

    sender = FakeSender(engine, during_send=record_new_failure)

    outcome = process_cluster_technical_alert(engine, sender, settings(), now=NOW)

    assert outcome.action is AlertNotificationAction.SENT
    assert engine.rows[0]["cluster_alerted_at"] == NOW
    assert engine.rows[0]["last_technical_failure_at"] == newer_failure
    assert engine.rows[0]["last_technical_failure_at"] > engine.rows[0]["cluster_alerted_at"]


def test_changed_cluster_membership_returns_stale_without_overwriting() -> None:
    engine = FakeAlertEngine(rows=qualifying_rows(5))

    def remove_membership() -> None:
        engine.rows[0]["cluster_alert_pending_at"] = None
        engine.rows[0]["cluster_alerted_at"] = NOW + timedelta(seconds=1)

    sender = FakeSender(engine, during_send=remove_membership)

    outcome = process_cluster_technical_alert(engine, sender, settings(), now=NOW)

    assert outcome.action is AlertNotificationAction.STALE_EPISODE
    assert engine.rows[0]["cluster_alerted_at"] == NOW + timedelta(seconds=1)
    assert all(row["cluster_alert_pending_at"] == NOW for row in engine.rows[1:])


def test_cluster_detection_sql_has_no_delivery_status_filter() -> None:
    engine = FakeAlertEngine(rows=qualifying_rows(4))
    sender = FakeSender(engine)

    process_cluster_technical_alert(engine, sender, settings(), now=NOW)

    detection_sql = next(
        str(statement.compile(dialect=postgresql.dialect()))
        for statement in engine.statements
        if isinstance(statement, Select)
        and "last_technical_failure_at >=" in str(statement)
    )
    assert "delivery_status" not in detection_sql
    assert "last_error" not in detection_sql.split("FROM", 1)[1]
