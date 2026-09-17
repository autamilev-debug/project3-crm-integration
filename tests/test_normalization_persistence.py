from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql.dml import Insert, Update
from sqlalchemy.sql.selectable import Select

import project3_crm.normalization.service as service_module
from project3_crm.normalization import (
    NormalizationFailure,
    NormalizationStateError,
    StoredWebhookEvent,
    integration_reference_for_id,
    normalize_webhook_event,
)


class FakeResult:
    def __init__(self, *, scalar: int | None = None, mapping: Any = None) -> None:
        self.scalar = scalar
        self.mapping = mapping

    def scalar_one(self) -> int:
        assert self.scalar is not None
        return self.scalar

    def mappings(self) -> "FakeResult":
        return self

    def one_or_none(self) -> Any:
        return deepcopy(self.mapping)


@dataclass
class FakeNormalizationEngine:
    webhook: dict[str, Any]
    next_sequence_id: int = 1
    normalized_by_event: dict[int, dict[str, Any]] = field(default_factory=dict)
    statements: list[Any] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    fail_parent_update_after_insert: bool = False

    def begin(self) -> "FakeTransaction":
        return FakeTransaction(self)


class FakeTransaction:
    def __init__(self, engine: FakeNormalizationEngine) -> None:
        self.engine = engine
        self.webhook_snapshot: dict[str, Any] = {}
        self.normalized_snapshot: dict[int, dict[str, Any]] = {}

    def __enter__(self) -> "FakeConnection":
        self.webhook_snapshot = deepcopy(self.engine.webhook)
        self.normalized_snapshot = deepcopy(self.engine.normalized_by_event)
        self.engine.events.append("begin")
        return FakeConnection(self.engine)

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        if exc_type is not None:
            self.engine.webhook = self.webhook_snapshot
            self.engine.normalized_by_event = self.normalized_snapshot
            self.engine.events.append("rollback")
            return False
        self.engine.events.append("commit")
        return False


class FakeConnection:
    def __init__(self, engine: FakeNormalizationEngine) -> None:
        self.engine = engine

    def execute(self, statement: Any) -> FakeResult:
        self.engine.statements.append(statement)
        sql = str(statement.compile(dialect=postgresql.dialect()))

        if isinstance(statement, Select) and "nextval(" in sql:
            self.engine.events.append("reserve_id")
            reserved = self.engine.next_sequence_id
            self.engine.next_sequence_id += 1
            return FakeResult(scalar=reserved)

        if isinstance(statement, Select) and "FROM webhook_events" in sql:
            self.engine.events.append("select_webhook")
            return FakeResult(mapping=self.engine.webhook)

        if isinstance(statement, Select) and "FROM normalized_leads" in sql:
            self.engine.events.append("select_normalized")
            return FakeResult(
                mapping=self.engine.normalized_by_event.get(self.engine.webhook["id"])
            )

        parameters = statement.compile(dialect=postgresql.dialect()).params
        if isinstance(statement, Insert):
            self.engine.events.append("insert_normalized")
            event_id = parameters["webhook_event_id"]
            if event_id in self.engine.normalized_by_event:
                raise SQLAlchemyError("simulated unique violation")
            self.engine.normalized_by_event[event_id] = deepcopy(parameters)
            return FakeResult()

        if isinstance(statement, Update):
            self.engine.events.append("update_webhook")
            if (
                self.engine.fail_parent_update_after_insert
                and self.engine.normalized_by_event
            ):
                raise SQLAlchemyError("simulated parent update failure")
            for key in (
                "event_status",
                "normalized_at",
                "completed_at",
                "last_error_category",
                "last_error",
                "updated_at",
            ):
                if key in parameters:
                    self.engine.webhook[key] = parameters[key]
            return FakeResult()

        raise AssertionError(f"Unexpected SQL statement: {sql}")


def stored_event(
    raw_payload: dict[str, Any],
    *,
    source: str = "website",
    event_id: str = "WEB-01001",
) -> StoredWebhookEvent:
    return StoredWebhookEvent(
        id=41,
        source=source,
        event_id=event_id,
        raw_payload=deepcopy(raw_payload),
    )


def fake_engine_for(
    event: StoredWebhookEvent,
    *,
    status: str = "PROCESSING",
    next_sequence_id: int = 1,
) -> FakeNormalizationEngine:
    return FakeNormalizationEngine(
        webhook={
            "id": event.id,
            "source": event.source,
            "event_id": event.event_id,
            "event_status": status,
            "raw_payload": deepcopy(event.raw_payload),
            "processing_started_at": datetime(2026, 9, 3, tzinfo=UTC),
            "normalized_at": None,
            "completed_at": None,
            "last_error_category": None,
            "last_error": None,
            "updated_at": datetime(2026, 9, 3, tzinfo=UTC),
        },
        next_sequence_id=next_sequence_id,
    )


@pytest.mark.parametrize(
    ("lead_id", "expected"),
    [
        (1, "IR-00001"),
        (57, "IR-00057"),
        (99_999, "IR-99999"),
        (100_000, "IR-100000"),
    ],
)
def test_integration_reference_uses_minimum_five_digit_padding(
    lead_id: int,
    expected: str,
) -> None:
    assert integration_reference_for_id(lead_id) == expected


def test_success_persists_normalized_lead_and_parent_atomically(
    normalization_payloads: dict[str, dict[str, Any]],
) -> None:
    event = stored_event(normalization_payloads["website"])
    original_raw = deepcopy(event.raw_payload)
    engine = fake_engine_for(event, next_sequence_id=57)

    outcome = normalize_webhook_event(engine, event)

    assert outcome.status == "NORMALIZED"
    assert outcome.normalized_lead_id == 57
    assert outcome.integration_reference == "IR-00057"
    assert outcome.already_processed is False
    row = engine.normalized_by_event[event.id]
    assert row["id"] == 57
    assert row["webhook_event_id"] == event.id
    assert row["integration_reference"] == "IR-00057"
    assert row["source"] == "website"
    assert row["source_event_id"] == "WEB-01001"
    assert row["email"] == "anna@example.com"
    assert row["delivery_status"] == "PENDING"
    assert row["source_metadata"]["form_id"] == "sales_contact_bg"
    assert engine.webhook["event_status"] == "NORMALIZED"
    assert engine.webhook["normalized_at"].tzinfo is not None
    assert engine.webhook["completed_at"] == engine.webhook["normalized_at"]
    assert engine.webhook["last_error_category"] is None
    assert engine.webhook["last_error"] is None
    assert engine.webhook["raw_payload"] == original_raw
    assert engine.events == [
        "begin",
        "select_webhook",
        "select_normalized",
        "reserve_id",
        "insert_normalized",
        "update_webhook",
        "commit",
    ]


@pytest.mark.parametrize(
    ("reserved_id", "expected_reference"),
    [(1, "IR-00001"), (100_000, "IR-100000")],
)
def test_reserved_sequence_id_is_persisted_in_reference_without_truncation(
    reserved_id: int,
    expected_reference: str,
    normalization_payloads: dict[str, dict[str, Any]],
) -> None:
    event = stored_event(normalization_payloads["website"])
    engine = fake_engine_for(event, next_sequence_id=reserved_id)

    outcome = normalize_webhook_event(engine, event)

    row = engine.normalized_by_event[event.id]
    assert row["id"] == reserved_id
    assert row["integration_reference"] == expected_reference
    assert outcome.integration_reference == expected_reference


def test_postgresql_statements_reserve_sequence_insert_and_update_in_one_transaction(
    normalization_payloads: dict[str, dict[str, Any]],
) -> None:
    event = stored_event(normalization_payloads["website"])
    engine = fake_engine_for(event)

    normalize_webhook_event(engine, event)

    sql = [str(statement.compile(dialect=postgresql.dialect())) for statement in engine.statements]
    assert any("nextval('normalized_leads_id_seq')" in statement for statement in sql)
    assert any("INSERT INTO normalized_leads" in statement for statement in sql)
    assert any("UPDATE webhook_events" in statement for statement in sql)
    assert engine.events[0] == "begin"
    assert engine.events[-1] == "commit"


def test_failed_normalization_creates_no_lead_and_preserves_sanitized_failure(
    normalization_payloads: dict[str, dict[str, Any]],
) -> None:
    payload = normalization_payloads["website"]
    rejected_email = "private.person@invalid"
    rejected_phone = "private-phone-value"
    payload["email"] = rejected_email
    payload["phone"] = rejected_phone
    event = stored_event(payload)
    original_raw = deepcopy(event.raw_payload)
    engine = fake_engine_for(event)

    outcome = normalize_webhook_event(engine, event)

    assert outcome.status == "NORMALIZATION_FAILED"
    assert engine.normalized_by_event == {}
    assert engine.webhook["event_status"] == "NORMALIZATION_FAILED"
    assert engine.webhook["last_error_category"] == "CONTACT_VALIDATION"
    assert "contact.email: invalid_email" in engine.webhook["last_error"]
    assert "contact.phone: invalid_phone" in engine.webhook["last_error"]
    assert rejected_email not in engine.webhook["last_error"]
    assert rejected_phone not in engine.webhook["last_error"]
    assert len(engine.webhook["last_error"]) <= 1_000
    assert engine.webhook["normalized_at"] is None
    assert engine.webhook["completed_at"].tzinfo is not None
    assert engine.webhook["raw_payload"] == original_raw
    assert "reserve_id" not in engine.events
    assert engine.events[-1] == "commit"


def test_provider_validation_failure_persists_no_rejected_values(
    normalization_payloads: dict[str, dict[str, Any]],
) -> None:
    payload = normalization_payloads["website"]
    rejected_email = "private.person@example.com"
    payload["email"] = rejected_email
    payload["first_name"] = {"private": "name-value"}
    event = stored_event(payload)
    engine = fake_engine_for(event)

    outcome = normalize_webhook_event(engine, event)

    assert outcome.status == "NORMALIZATION_FAILED"
    assert engine.webhook["last_error_category"] == "PROVIDER_VALIDATION"
    assert "provider.first_name: string_type" in engine.webhook["last_error"]
    assert rejected_email not in engine.webhook["last_error"]
    assert "name-value" not in engine.webhook["last_error"]
    assert engine.normalized_by_event == {}


def test_same_event_normalized_again_reuses_existing_row_and_reference(
    normalization_payloads: dict[str, dict[str, Any]],
) -> None:
    event = stored_event(normalization_payloads["website"])
    engine = fake_engine_for(event, next_sequence_id=99_999)

    first = normalize_webhook_event(engine, event)
    second = normalize_webhook_event(engine, event)

    assert first.integration_reference == "IR-99999"
    assert second.status == "NORMALIZED"
    assert second.integration_reference == "IR-99999"
    assert second.normalized_lead_id == 99_999
    assert second.already_processed is True
    assert len(engine.normalized_by_event) == 1
    assert engine.next_sequence_id == 100_000


def test_existing_normalized_row_repairs_processing_parent_without_new_reference(
    normalization_payloads: dict[str, dict[str, Any]],
) -> None:
    event = stored_event(normalization_payloads["website"])
    engine = fake_engine_for(event, status="PROCESSING", next_sequence_id=2)
    engine.normalized_by_event[event.id] = {
        "id": 1,
        "integration_reference": "IR-00001",
    }

    outcome = normalize_webhook_event(engine, event)

    assert outcome.already_processed is True
    assert outcome.integration_reference == "IR-00001"
    assert engine.webhook["event_status"] == "NORMALIZED"
    assert engine.webhook["normalized_at"].tzinfo is not None
    assert engine.webhook["completed_at"] == engine.webhook["normalized_at"]
    assert engine.next_sequence_id == 2


def test_normalization_failed_event_is_not_automatically_reprocessed(
    normalization_payloads: dict[str, dict[str, Any]],
) -> None:
    event = stored_event(normalization_payloads["website"])
    engine = fake_engine_for(event, status="NORMALIZATION_FAILED")
    engine.webhook["last_error"] = "contact.email: invalid_email"

    outcome = normalize_webhook_event(engine, event)

    assert outcome.status == "NORMALIZATION_FAILED"
    assert outcome.already_processed is True
    assert engine.normalized_by_event == {}
    assert engine.next_sequence_id == 1


def test_event_must_be_worker_claimed_before_normalization(
    normalization_payloads: dict[str, dict[str, Any]],
) -> None:
    event = stored_event(normalization_payloads["website"])
    engine = fake_engine_for(event, status="RECEIVED")

    with pytest.raises(NormalizationStateError, match="PROCESSING"):
        normalize_webhook_event(engine, event)

    assert engine.normalized_by_event == {}
    assert engine.events[-1] == "rollback"


def test_parent_update_failure_rolls_back_normalized_row_but_sequence_gap_is_allowed(
    normalization_payloads: dict[str, dict[str, Any]],
) -> None:
    event = stored_event(normalization_payloads["website"])
    engine = fake_engine_for(event, next_sequence_id=100_000)
    engine.fail_parent_update_after_insert = True

    with pytest.raises(SQLAlchemyError):
        normalize_webhook_event(engine, event)

    assert engine.normalized_by_event == {}
    assert engine.webhook["event_status"] == "PROCESSING"
    assert engine.next_sequence_id == 100_001
    assert engine.events[-1] == "rollback"


def test_persisted_diagnostic_is_capped_at_one_thousand_characters(
    normalization_payloads: dict[str, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = stored_event(normalization_payloads["website"])
    engine = fake_engine_for(event)

    def fail_with_long_sanitized_diagnostic(*args: Any, **kwargs: Any) -> Any:
        raise NormalizationFailure("PROVIDER_VALIDATION", "safe-code;" * 200)

    monkeypatch.setattr(
        service_module,
        "normalize_payload",
        fail_with_long_sanitized_diagnostic,
    )

    normalize_webhook_event(engine, event)

    assert len(engine.webhook["last_error"]) == 1_000
