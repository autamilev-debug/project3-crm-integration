from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.dml import Update
from sqlalchemy.sql.selectable import Select

import project3_crm.crm_delivery as crm_delivery_module
from project3_crm.crm_delivery import (
    CrmDeliveryAction,
    CrmDeliveryConsistencyError,
    SendingGateStatus,
    apply_create_result,
    check_or_recover_sending,
    claim_one_crm_create,
    deliver_one_crm_create,
    process_one_crm_work,
    reconcile_one_unknown,
    select_one_due_unknown,
)
from project3_crm.integrations.hubspot import (
    CreateContactResult,
    CreateResultCategory,
    HubSpotContactInput,
    HubSpotDiagnostics,
    ReadContactResult,
    ReadResultCategory,
)


NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def test_process_one_crm_work_prioritizes_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def reconcile(*args: Any, **kwargs: Any) -> Any:
        calls.append("reconcile")
        return crm_delivery_module.CrmDeliveryOutcome(
            action=CrmDeliveryAction.SUCCESS,
            lead_id=1,
        )

    def deliver(*args: Any, **kwargs: Any) -> Any:
        calls.append("create")
        raise AssertionError("create must not run after reconciliation work")

    monkeypatch.setattr(crm_delivery_module, "reconcile_one_unknown", reconcile)
    monkeypatch.setattr(crm_delivery_module, "deliver_one_crm_create", deliver)

    outcome = process_one_crm_work(object(), object(), object(), now=NOW)

    assert outcome.action is CrmDeliveryAction.SUCCESS
    assert calls == ["reconcile"]


def test_process_one_crm_work_attempts_create_only_after_no_reconciliation_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def reconcile(*args: Any, **kwargs: Any) -> Any:
        calls.append("reconcile")
        return crm_delivery_module.CrmDeliveryOutcome(
            action=CrmDeliveryAction.NO_WORK
        )

    def deliver(*args: Any, **kwargs: Any) -> Any:
        calls.append("create")
        return crm_delivery_module.CrmDeliveryOutcome(
            action=CrmDeliveryAction.SUCCESS,
            lead_id=2,
        )

    monkeypatch.setattr(crm_delivery_module, "reconcile_one_unknown", reconcile)
    monkeypatch.setattr(crm_delivery_module, "deliver_one_crm_create", deliver)

    outcome = process_one_crm_work(object(), object(), object(), now=NOW)

    assert outcome.action is CrmDeliveryAction.SUCCESS
    assert calls == ["reconcile", "create"]


def test_process_one_crm_work_preserves_fresh_sending_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def reconcile(*args: Any, **kwargs: Any) -> Any:
        calls.append("reconcile")
        return crm_delivery_module.CrmDeliveryOutcome(
            action=CrmDeliveryAction.CRM_BLOCKED
        )

    def deliver(*args: Any, **kwargs: Any) -> Any:
        calls.append("create")
        raise AssertionError("create must not bypass the SENDING gate")

    monkeypatch.setattr(crm_delivery_module, "reconcile_one_unknown", reconcile)
    monkeypatch.setattr(crm_delivery_module, "deliver_one_crm_create", deliver)

    outcome = process_one_crm_work(object(), object(), object(), now=NOW)

    assert outcome.action is CrmDeliveryAction.CRM_BLOCKED
    assert calls == ["reconcile"]


class FakeResult:
    def __init__(self, *, mapping: Any = None, scalar: int | None = None) -> None:
        self.mapping = mapping
        self.scalar = scalar

    def mappings(self) -> FakeResult:
        return self

    def one_or_none(self) -> Any:
        return deepcopy(self.mapping)

    def scalar_one_or_none(self) -> int | None:
        return self.scalar


@dataclass
class FakeCrmEngine:
    rows: list[dict[str, Any]]
    runtime: dict[str, Any] = field(default_factory=dict)
    statements: list[Any] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    transaction_depth: int = 0
    fail_next_lead_update: bool = False

    def __post_init__(self) -> None:
        if not self.runtime:
            self.runtime = runtime_row()

    def begin(self) -> FakeTransaction:
        return FakeTransaction(self)


class FakeTransaction:
    def __init__(self, engine: FakeCrmEngine) -> None:
        self.engine = engine
        self.rows_snapshot: list[dict[str, Any]] = []
        self.runtime_snapshot: dict[str, Any] = {}

    def __enter__(self) -> FakeConnection:
        self.rows_snapshot = deepcopy(self.engine.rows)
        self.runtime_snapshot = deepcopy(self.engine.runtime)
        self.engine.events.append("begin")
        self.engine.transaction_depth += 1
        return FakeConnection(self.engine)

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        self.engine.transaction_depth -= 1
        if exc_type is not None:
            self.engine.rows = self.rows_snapshot
            self.engine.runtime = self.runtime_snapshot
            self.engine.events.append("rollback")
            return False
        self.engine.events.append("commit")
        return False


class FakeConnection:
    def __init__(self, engine: FakeCrmEngine) -> None:
        self.engine = engine

    def execute(self, statement: Any) -> FakeResult:
        self.engine.statements.append(statement)
        compiled = statement.compile(dialect=postgresql.dialect())
        sql = str(compiled)
        parameters = compiled.params

        if isinstance(statement, Select) and "FROM integration_runtime_state" in sql:
            self.engine.events.append("select_runtime")
            return FakeResult(mapping=self.engine.runtime)

        if isinstance(statement, Select) and "FROM normalized_leads" in sql:
            if "ORDER BY normalized_leads.last_attempt_at ASC" in sql:
                self.engine.events.append("select_sending_gate")
                candidates = [
                    row
                    for row in self.engine.rows
                    if row["delivery_status"] == "SENDING"
                ]
                candidates.sort(
                    key=lambda row: (
                        row["last_attempt_at"] is None,
                        row["last_attempt_at"] or NOW,
                        row["id"],
                    )
                )
                return FakeResult(mapping=candidates[0] if candidates else None)

            if "ORDER BY normalized_leads.next_reconciliation_at ASC" in sql:
                self.engine.events.append("select_due_unknown")
                current_time = parameters["next_reconciliation_at_1"]
                candidates = [
                    row
                    for row in self.engine.rows
                    if row["delivery_status"] == "UNKNOWN"
                    and row["next_reconciliation_at"] is not None
                    and row["next_reconciliation_at"] <= current_time
                ]
                candidates.sort(
                    key=lambda row: (row["next_reconciliation_at"], row["id"])
                )
                return FakeResult(mapping=candidates[0] if candidates else None)

            if "CASE WHEN" in sql:
                self.engine.events.append("select_candidate")
                current_time = parameters["next_retry_at_1"]
                candidates = [
                    row
                    for row in self.engine.rows
                    if row["delivery_status"] == "PENDING"
                    or (
                        row["delivery_status"] == "RETRY_PENDING"
                        and row["next_retry_at"] is not None
                        and row["next_retry_at"] <= current_time
                    )
                ]
                candidates.sort(
                    key=lambda row: (
                        row["next_retry_at"]
                        if row["delivery_status"] == "RETRY_PENDING"
                        else row["created_at"],
                        row["id"],
                    )
                )
                return FakeResult(mapping=candidates[0] if candidates else None)

            if "reconciliation_not_found_count" in sql:
                self.engine.events.append("select_expected_unknown")
                row = next(
                    (row for row in self.engine.rows if row["id"] == parameters["id_1"]),
                    None,
                )
                return FakeResult(mapping=row)

            if "automatic_retry_count" in sql:
                self.engine.events.append("select_expected_sending")
                row = next(
                    (row for row in self.engine.rows if row["id"] == parameters["id_1"]),
                    None,
                )
                return FakeResult(mapping=row)

            if "next_reconciliation_at <=" in sql:
                self.engine.events.append("select_due_unknown_gate")
                current_time = parameters["next_reconciliation_at_1"]
                due_unknown = next(
                    (
                        row["id"]
                        for row in self.engine.rows
                        if row["delivery_status"] == "UNKNOWN"
                        and row["next_reconciliation_at"] is not None
                        and row["next_reconciliation_at"] <= current_time
                    ),
                    None,
                )
                return FakeResult(scalar=due_unknown)

            raise AssertionError(f"Unexpected normalized_leads SELECT: {sql}")

        if isinstance(statement, Update) and "UPDATE normalized_leads" in sql:
            self.engine.events.append("update_lead")
            if self.engine.fail_next_lead_update:
                self.engine.fail_next_lead_update = False
                raise RuntimeError("fictional persistence failure")
            lead_id = parameters["id_1"]
            row = next((row for row in self.engine.rows if row["id"] == lead_id), None)
            expected_status = parameters.get("delivery_status_1")
            if row is None or (
                expected_status is not None
                and row["delivery_status"] != expected_status
            ):
                return FakeResult(scalar=None)
            if expected_status == "RETRY_PENDING":
                due_at = parameters.get("next_retry_at_1")
                if row["next_retry_at"] is None or row["next_retry_at"] > due_at:
                    return FakeResult(scalar=None)
            if "write_attempt_count +" in sql:
                row["write_attempt_count"] += parameters["write_attempt_count_1"]
            for name in row:
                if name in parameters:
                    row[name] = parameters[name]
            return FakeResult(scalar=lead_id)

        if isinstance(statement, Update) and "UPDATE integration_runtime_state" in sql:
            self.engine.events.append("update_runtime")
            for name in self.engine.runtime:
                if name in parameters:
                    self.engine.runtime[name] = parameters[name]
            return FakeResult(scalar=1)

        raise AssertionError(f"Unexpected SQL statement: {sql}")


def runtime_row(*, paused: bool = False) -> dict[str, Any]:
    return {
        "id": 1,
        "crm_delivery_paused": paused,
        "pause_reason": "CONFIG" if paused else None,
        "paused_at": NOW - timedelta(hours=1) if paused else None,
        "pause_alert_sent_at": NOW - timedelta(minutes=30) if paused else None,
        "pause_alert_last_error": "old alert error" if paused else None,
        "updated_at": NOW - timedelta(hours=1),
    }


def lead_row(
    lead_id: int = 1,
    status: str = "PENDING",
    **overrides: Any,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": lead_id,
        "webhook_event_id": 1000 + lead_id,
        "integration_reference": f"IR-{lead_id:05d}",
        "source": "website",
        "source_event_id": f"WEB-{lead_id}",
        "first_name": "Ada",
        "last_name": "Lovelace",
        "email": "ada@example.test",
        "phone": None,
        "company": "Analytical Engines Ltd",
        "job_title": "Mathematician",
        "city": "London",
        "country": "United Kingdom",
        "campaign": "fictional-campaign",
        "lead_source": "website",
        "source_metadata": {"private": "never outbound"},
        "delivery_status": status,
        "write_attempt_count": 0,
        "automatic_retry_count": 0,
        "next_retry_at": None,
        "last_attempt_at": None,
        "first_failure_at": None,
        "last_failure_at": None,
        "reconciliation_not_found_count": 4,
        "reconciliation_error_count": 2,
        "next_reconciliation_at": NOW + timedelta(days=1),
        "last_error_category": "OLD",
        "last_error_code": "OLD_CODE",
        "last_error": "old safe failure",
        "crm_record_id": None,
        "crm_correlation_id": "old-correlation",
        "sent_at": None,
        "failure_at": None,
        "last_technical_failure_at": None,
        "created_at": NOW - timedelta(hours=1),
        "updated_at": NOW - timedelta(hours=1),
    }
    row.update(overrides)
    return row


def delivery_settings() -> Any:
    return SimpleNamespace(
        worker_lease_seconds=900,
        crm_retry_delay_1_seconds=300,
        crm_retry_delay_2_seconds=1_800,
        crm_retry_delay_3_seconds=7_200,
        crm_reconcile_initial_delay_seconds=15,
        crm_reconcile_not_found_delay_2_seconds=60,
        crm_reconcile_not_found_delay_3_seconds=300,
        crm_reconcile_error_delay_1_seconds=300,
        crm_reconcile_error_delay_2_seconds=1_800,
    )


def create_result(
    category: CreateResultCategory,
    *,
    contact_id: str | None = None,
    retry_after: str | None = None,
    error_code: str | None = "FAKE_CODE",
    correlation_id: str | None = "corr-123",
) -> CreateContactResult:
    return CreateContactResult(
        category=category,
        contact_id=contact_id,
        diagnostics=HubSpotDiagnostics(
            description=f"safe {category.value}",
            http_status=429 if category is CreateResultCategory.RATE_LIMITED else 500,
            category="SAFE_PROVIDER_CATEGORY",
            error_code=error_code,
            correlation_id=correlation_id,
            retry_after=retry_after,
        ),
    )


def read_result(
    category: ReadResultCategory,
    *,
    contact_id: str | None = None,
    properties: dict[str, str | None] | None = None,
    retry_after: str | None = None,
) -> ReadContactResult:
    return ReadContactResult(
        category=category,
        contact_id=contact_id,
        properties=properties,
        diagnostics=HubSpotDiagnostics(
            description=f"safe {category.value}",
            http_status=200 if category is ReadResultCategory.FOUND else 404,
            category="SAFE_READ_CATEGORY",
            error_code="SAFE_READ_CODE",
            correlation_id="read-corr-123",
            retry_after=retry_after,
        ),
    )


class FakeAdapter:
    def __init__(
        self,
        result: CreateContactResult | None = None,
        *,
        engine: FakeCrmEngine | None = None,
        exception: Exception | None = None,
        reference_result: ReadContactResult | None = None,
        email_result: ReadContactResult | None = None,
    ) -> None:
        self.result = result
        self.engine = engine
        self.exception = exception
        self.reference_result = reference_result
        self.email_result = email_result
        self.create_calls: list[HubSpotContactInput] = []
        self.lookup_calls: list[tuple[str, str]] = []

    def create_contact(self, contact: HubSpotContactInput) -> CreateContactResult:
        if self.engine is not None:
            assert self.engine.transaction_depth == 0
            assert self.engine.events[-1] == "commit"
            row = next(row for row in self.engine.rows if row["id"] == 1)
            assert row["delivery_status"] == "SENDING"
            self.engine.events.append("create_contact")
        self.create_calls.append(contact)
        if self.exception is not None:
            raise self.exception
        assert self.result is not None
        return self.result

    def get_contact_by_integration_reference(self, value: str) -> ReadContactResult:
        if self.engine is not None:
            assert self.engine.transaction_depth == 0
            self.engine.events.append("reference_read")
        self.lookup_calls.append(("reference", value))
        assert self.reference_result is not None
        return self.reference_result

    def get_contact_by_email(self, value: str) -> ReadContactResult:
        if self.engine is not None:
            assert self.engine.transaction_depth == 0
            self.engine.events.append("email_read")
        self.lookup_calls.append(("email", value))
        assert self.email_result is not None
        return self.email_result


def test_pending_claim_commits_sending_state_and_explicit_contact_input() -> None:
    engine = FakeCrmEngine([lead_row(write_attempt_count=4)])

    claimed = claim_one_crm_create(engine, now=NOW)

    assert claimed is not None
    assert claimed.lead_id == 1
    assert claimed.contact.model_dump() == {
        "first_name": "Ada",
        "last_name": "Lovelace",
        "integration_reference": "IR-00001",
        "email": "ada@example.test",
        "phone": None,
        "company": "Analytical Engines Ltd",
        "job_title": "Mathematician",
        "city": "London",
        "country": "United Kingdom",
    }
    assert engine.rows[0]["delivery_status"] == "SENDING"
    assert engine.rows[0]["write_attempt_count"] == 5
    assert engine.rows[0]["last_attempt_at"] == NOW
    assert engine.rows[0]["next_retry_at"] is None
    assert engine.events[-1] == "commit"


def test_due_retry_pending_is_claimable_and_future_retry_is_not() -> None:
    due = lead_row(
        1,
        "RETRY_PENDING",
        next_retry_at=NOW - timedelta(seconds=1),
    )
    future = lead_row(
        2,
        "RETRY_PENDING",
        next_retry_at=NOW + timedelta(seconds=1),
    )
    engine = FakeCrmEngine([future, due])

    claimed = claim_one_crm_create(engine, now=NOW)

    assert claimed is not None and claimed.lead_id == 1
    assert due["delivery_status"] == "SENDING"
    assert future["delivery_status"] == "RETRY_PENDING"

    future_only = FakeCrmEngine([lead_row(2, "RETRY_PENDING", next_retry_at=NOW + timedelta(1))])
    assert claim_one_crm_create(future_only, now=NOW) is None


def test_claim_uses_due_or_created_time_then_id_and_claims_at_most_one() -> None:
    later_pending = lead_row(4, created_at=NOW - timedelta(minutes=5))
    tie_high = lead_row(3, created_at=NOW - timedelta(minutes=20))
    tie_low = lead_row(2, created_at=NOW - timedelta(minutes=20))
    earlier_retry = lead_row(
        1,
        "RETRY_PENDING",
        next_retry_at=NOW - timedelta(minutes=30),
    )
    engine = FakeCrmEngine([later_pending, tie_high, tie_low, earlier_retry])

    claimed = claim_one_crm_create(engine, now=NOW)

    assert claimed is not None and claimed.lead_id == 1
    assert sum(row["delivery_status"] == "SENDING" for row in engine.rows) == 1
    claim_sql = next(
        str(statement.compile(dialect=postgresql.dialect()))
        for statement in engine.statements
        if isinstance(statement, Select) and "CASE WHEN" in str(statement)
    )
    assert "CASE WHEN" in claim_sql
    assert "normalized_leads.id ASC" in claim_sql
    assert "LIMIT" in claim_sql
    assert "FOR UPDATE" in claim_sql


def test_paused_or_unresolved_sending_claims_nothing() -> None:
    paused_engine = FakeCrmEngine([lead_row()], runtime=runtime_row(paused=True))
    sending_engine = FakeCrmEngine([lead_row(1, "SENDING"), lead_row(2)])

    assert claim_one_crm_create(paused_engine, now=NOW) is None
    assert claim_one_crm_create(sending_engine, now=NOW) is None
    assert paused_engine.rows[0]["delivery_status"] == "PENDING"
    assert sending_engine.rows[1]["delivery_status"] == "PENDING"


def test_success_commits_claim_before_http_and_preserves_failure_history() -> None:
    first_failure = NOW - timedelta(days=2)
    last_failure = NOW - timedelta(days=1)
    technical_failure = NOW - timedelta(hours=3)
    engine = FakeCrmEngine(
        [
            lead_row(
                first_failure_at=first_failure,
                last_failure_at=last_failure,
                last_technical_failure_at=technical_failure,
                automatic_retry_count=2,
            )
        ]
    )
    adapter = FakeAdapter(
        create_result(CreateResultCategory.SUCCESS, contact_id="contact-123"),
        engine=engine,
    )

    outcome = deliver_one_crm_create(engine, adapter, delivery_settings(), now=NOW)

    row = engine.rows[0]
    assert outcome.action is CrmDeliveryAction.SUCCESS
    assert row["delivery_status"] == "SUCCESS"
    assert row["crm_record_id"] == "contact-123"
    assert row["sent_at"] == NOW
    assert row["next_retry_at"] is None
    assert row["next_reconciliation_at"] is None
    assert row["last_error_category"] is None
    assert row["last_error_code"] is None
    assert row["last_error"] is None
    assert row["first_failure_at"] == first_failure
    assert row["last_failure_at"] == last_failure
    assert row["last_technical_failure_at"] == technical_failure
    assert row["automatic_retry_count"] == 2
    assert row["write_attempt_count"] == 1
    assert engine.events.index("commit") < engine.events.index("create_contact")


@pytest.mark.parametrize(
    ("starting_count", "expected_status", "expected_count", "expected_delay"),
    [
        (0, "RETRY_PENDING", 1, 300),
        (1, "RETRY_PENDING", 2, 1_800),
        (2, "RETRY_PENDING", 3, 7_200),
        (3, "FAILED_ESCALATED", 3, None),
    ],
)
@pytest.mark.parametrize(
    "category",
    [CreateResultCategory.RETRYABLE_FAILURE, CreateResultCategory.RATE_LIMITED],
)
def test_safe_retry_budget_has_no_retry_four(
    starting_count: int,
    expected_status: str,
    expected_count: int,
    expected_delay: int | None,
    category: CreateResultCategory,
) -> None:
    engine = FakeCrmEngine([lead_row(automatic_retry_count=starting_count)])
    adapter = FakeAdapter(create_result(category), engine=engine)

    outcome = deliver_one_crm_create(engine, adapter, delivery_settings(), now=NOW)

    row = engine.rows[0]
    assert row["delivery_status"] == expected_status
    assert row["automatic_retry_count"] == expected_count
    assert row["write_attempt_count"] == 1
    assert row["first_failure_at"] == NOW
    assert row["last_failure_at"] == NOW
    assert row["last_technical_failure_at"] == NOW
    assert row["last_error_category"] == category.value
    assert row["last_error_code"] == "FAKE_CODE"
    assert row["crm_correlation_id"] == "corr-123"
    if expected_delay is None:
        assert outcome.action is CrmDeliveryAction.FAILED_ESCALATED
        assert row["failure_at"] == NOW
        assert row["next_retry_at"] is None
    else:
        assert outcome.action is CrmDeliveryAction.RETRY_PENDING
        assert row["next_retry_at"] == NOW + timedelta(seconds=expected_delay)
        assert row["failure_at"] is None
    assert row["next_reconciliation_at"] is None


@pytest.mark.parametrize(
    ("retry_after", "expected"),
    [
        ("900", NOW + timedelta(seconds=900)),
        ("60", NOW + timedelta(seconds=300)),
        (format_datetime(NOW + timedelta(minutes=20), usegmt=True), NOW + timedelta(minutes=20)),
        ("not-a-valid-delay", NOW + timedelta(seconds=300)),
    ],
)
def test_retry_after_uses_later_valid_provider_or_configured_time(
    retry_after: str,
    expected: datetime,
) -> None:
    engine = FakeCrmEngine([lead_row()])
    adapter = FakeAdapter(
        create_result(CreateResultCategory.RATE_LIMITED, retry_after=retry_after),
        engine=engine,
    )

    deliver_one_crm_create(engine, adapter, delivery_settings(), now=NOW)

    assert engine.rows[0]["next_retry_at"] == expected


def test_unknown_enters_reconciliation_without_consuming_retry_budget() -> None:
    engine = FakeCrmEngine(
        [lead_row(automatic_retry_count=2, next_retry_at=NOW - timedelta(minutes=1))]
    )
    adapter = FakeAdapter(create_result(CreateResultCategory.UNKNOWN), engine=engine)

    outcome = deliver_one_crm_create(engine, adapter, delivery_settings(), now=NOW)

    row = engine.rows[0]
    assert outcome.action is CrmDeliveryAction.UNKNOWN
    assert row["delivery_status"] == "UNKNOWN"
    assert row["automatic_retry_count"] == 2
    assert row["reconciliation_not_found_count"] == 0
    assert row["reconciliation_error_count"] == 0
    assert row["next_reconciliation_at"] == NOW + timedelta(seconds=15)
    assert row["next_retry_at"] is None
    assert row["first_failure_at"] == NOW
    assert row["last_failure_at"] == NOW
    assert row["last_technical_failure_at"] == NOW


@pytest.mark.parametrize(
    ("category", "pause_reason"),
    [
        (CreateResultCategory.AUTH_FAILURE, "AUTH"),
        (CreateResultCategory.CONFIG_FAILURE, "CONFIG"),
    ],
)
def test_auth_and_config_return_to_pending_and_activate_global_pause(
    category: CreateResultCategory,
    pause_reason: str,
) -> None:
    historical_technical = NOW - timedelta(days=1)
    engine = FakeCrmEngine(
        [
            lead_row(
                automatic_retry_count=2,
                last_technical_failure_at=historical_technical,
            )
        ],
        runtime={
            **runtime_row(),
            "pause_alert_sent_at": NOW - timedelta(hours=2),
            "pause_alert_last_error": "old alert error",
        },
    )
    adapter = FakeAdapter(create_result(category), engine=engine)

    outcome = deliver_one_crm_create(engine, adapter, delivery_settings(), now=NOW)

    row = engine.rows[0]
    assert outcome.action is CrmDeliveryAction.GLOBAL_PAUSE_ACTIVATED
    assert row["delivery_status"] == "PENDING"
    assert row["automatic_retry_count"] == 2
    assert row["write_attempt_count"] == 1
    assert row["next_retry_at"] is None
    assert row["last_technical_failure_at"] == historical_technical
    assert row["last_failure_at"] == NOW
    assert engine.runtime["crm_delivery_paused"] is True
    assert engine.runtime["pause_reason"] == pause_reason
    assert engine.runtime["paused_at"] == NOW
    assert engine.runtime["pause_alert_sent_at"] is None
    assert engine.runtime["pause_alert_last_error"] is None

    blocked_adapter = FakeAdapter(create_result(CreateResultCategory.SUCCESS, contact_id="x"))
    blocked = deliver_one_crm_create(
        engine, blocked_adapter, delivery_settings(), now=NOW + timedelta(minutes=1)
    )
    assert blocked.action is CrmDeliveryAction.NO_WORK
    assert blocked_adapter.create_calls == []


def test_unclassified_permanent_failure_is_terminal_technical_failure() -> None:
    engine = FakeCrmEngine([lead_row()])
    adapter = FakeAdapter(
        create_result(CreateResultCategory.UNCLASSIFIED_PERMANENT_FAILURE),
        engine=engine,
    )

    outcome = deliver_one_crm_create(engine, adapter, delivery_settings(), now=NOW)

    row = engine.rows[0]
    assert outcome.action is CrmDeliveryAction.FAILED_ESCALATED
    assert row["delivery_status"] == "FAILED_ESCALATED"
    assert row["failure_at"] == NOW
    assert row["last_technical_failure_at"] == NOW
    assert row["next_retry_at"] is None
    assert row["next_reconciliation_at"] is None


@pytest.mark.parametrize(
    ("properties", "expected_error_category"),
    [
        (
            {
                "integration_reference": "IR-00001",
                "email": "ada@example.test",
            },
            "CRM_REFERENCE_COLLISION",
        ),
        (
            {
                "integration_reference": "IR-99999",
                "email": "returned-private@example.test",
            },
            "CRM_DATA_INTEGRITY",
        ),
    ],
)
def test_409_reference_found_always_activates_config_pause_without_email_lookup(
    properties: dict[str, str | None],
    expected_error_category: str,
) -> None:
    engine = FakeCrmEngine([lead_row(automatic_retry_count=2)])
    adapter = FakeAdapter(
        create_result(CreateResultCategory.CONFLICT_REQUIRES_LOOKUP),
        engine=engine,
        reference_result=read_result(
            ReadResultCategory.FOUND,
            contact_id="active-reference-contact",
            properties=properties,
        ),
    )

    outcome = deliver_one_crm_create(engine, adapter, delivery_settings(), now=NOW)

    row = engine.rows[0]
    assert outcome.action is CrmDeliveryAction.GLOBAL_PAUSE_ACTIVATED
    assert row["delivery_status"] == "PENDING"
    assert row["automatic_retry_count"] == 2
    assert row["last_technical_failure_at"] is None
    assert row["last_error_category"] == expected_error_category
    assert "returned-private@example.test" not in row["last_error"]
    assert engine.runtime["pause_reason"] == "CONFIG"
    assert adapter.lookup_calls == [("reference", "IR-00001")]
    assert len(adapter.create_calls) == 1


@pytest.mark.parametrize(
    ("read_category", "pause_reason"),
    [
        (ReadResultCategory.AUTH_FAILURE, "AUTH"),
        (ReadResultCategory.CONFIG_FAILURE, "CONFIG"),
    ],
)
def test_409_reference_auth_or_config_stops_before_email_lookup(
    read_category: ReadResultCategory,
    pause_reason: str,
) -> None:
    engine = FakeCrmEngine([lead_row(automatic_retry_count=1)])
    adapter = FakeAdapter(
        create_result(CreateResultCategory.CONFLICT_REQUIRES_LOOKUP),
        engine=engine,
        reference_result=read_result(read_category),
    )

    outcome = deliver_one_crm_create(engine, adapter, delivery_settings(), now=NOW)

    assert outcome.action is CrmDeliveryAction.GLOBAL_PAUSE_ACTIVATED
    assert engine.rows[0]["delivery_status"] == "PENDING"
    assert engine.rows[0]["automatic_retry_count"] == 1
    assert engine.rows[0]["last_technical_failure_at"] is None
    assert engine.runtime["pause_reason"] == pause_reason
    assert adapter.lookup_calls == [("reference", "IR-00001")]


def test_409_reference_not_found_then_exact_email_found_is_existing_contact() -> None:
    first_failure = NOW - timedelta(days=3)
    last_failure = NOW - timedelta(days=2)
    technical_failure = NOW - timedelta(days=1)
    engine = FakeCrmEngine(
        [
            lead_row(
                automatic_retry_count=2,
                first_failure_at=first_failure,
                last_failure_at=last_failure,
                last_technical_failure_at=technical_failure,
            )
        ]
    )
    adapter = FakeAdapter(
        create_result(CreateResultCategory.CONFLICT_REQUIRES_LOOKUP),
        engine=engine,
        reference_result=read_result(ReadResultCategory.ACTIVE_NOT_FOUND),
        email_result=read_result(
            ReadResultCategory.FOUND,
            contact_id="existing-contact-123",
            properties={"email": "ada@example.test"},
        ),
    )

    outcome = deliver_one_crm_create(engine, adapter, delivery_settings(), now=NOW)

    row = engine.rows[0]
    assert outcome.action is CrmDeliveryAction.EXISTING_CONTACT
    assert row["delivery_status"] == "EXISTING_CONTACT"
    assert row["crm_record_id"] == "existing-contact-123"
    assert row["sent_at"] is None
    assert row["failure_at"] is None
    assert row["next_retry_at"] is None
    assert row["next_reconciliation_at"] is None
    assert row["last_error_category"] is None
    assert row["last_error_code"] is None
    assert row["last_error"] is None
    assert row["first_failure_at"] == first_failure
    assert row["last_failure_at"] == last_failure
    assert row["last_technical_failure_at"] == technical_failure
    assert row["automatic_retry_count"] == 2
    assert row["write_attempt_count"] == 1
    assert adapter.lookup_calls == [
        ("reference", "IR-00001"),
        ("email", "ada@example.test"),
    ]
    assert engine.events.index("reference_read") < engine.events.index("email_read")
    assert len(adapter.create_calls) == 1


@pytest.mark.parametrize(
    ("contact_id", "properties"),
    [
        ("existing-contact-123", {}),
        ("existing-contact-123", {"email": None}),
        ("existing-contact-123", {"email": "other-private@example.test"}),
        (None, {"email": "ada@example.test"}),
        ("   ", {"email": "ada@example.test"}),
    ],
)
def test_409_unsafe_email_found_activates_config_data_integrity_pause(
    contact_id: str | None,
    properties: dict[str, str | None],
) -> None:
    engine = FakeCrmEngine([lead_row()])
    adapter = FakeAdapter(
        create_result(CreateResultCategory.CONFLICT_REQUIRES_LOOKUP),
        engine=engine,
        reference_result=read_result(ReadResultCategory.ACTIVE_NOT_FOUND),
        email_result=read_result(
            ReadResultCategory.FOUND,
            contact_id=contact_id,
            properties=properties,
        ),
    )

    outcome = deliver_one_crm_create(engine, adapter, delivery_settings(), now=NOW)

    row = engine.rows[0]
    assert outcome.action is CrmDeliveryAction.GLOBAL_PAUSE_ACTIVATED
    assert row["delivery_status"] == "PENDING"
    assert row["last_error_category"] == "CRM_DATA_INTEGRITY"
    assert row["last_technical_failure_at"] is None
    assert "other-private@example.test" not in row["last_error"]
    assert engine.runtime["pause_reason"] == "CONFIG"


@pytest.mark.parametrize(
    ("read_category", "pause_reason"),
    [
        (ReadResultCategory.AUTH_FAILURE, "AUTH"),
        (ReadResultCategory.CONFIG_FAILURE, "CONFIG"),
    ],
)
def test_409_email_auth_or_config_activates_pause(
    read_category: ReadResultCategory,
    pause_reason: str,
) -> None:
    engine = FakeCrmEngine([lead_row(automatic_retry_count=3)])
    adapter = FakeAdapter(
        create_result(CreateResultCategory.CONFLICT_REQUIRES_LOOKUP),
        engine=engine,
        reference_result=read_result(ReadResultCategory.ACTIVE_NOT_FOUND),
        email_result=read_result(read_category),
    )

    outcome = deliver_one_crm_create(engine, adapter, delivery_settings(), now=NOW)

    assert outcome.action is CrmDeliveryAction.GLOBAL_PAUSE_ACTIVATED
    assert engine.rows[0]["delivery_status"] == "PENDING"
    assert engine.rows[0]["automatic_retry_count"] == 3
    assert engine.rows[0]["last_technical_failure_at"] is None
    assert engine.runtime["pause_reason"] == pause_reason
    assert adapter.lookup_calls == [
        ("reference", "IR-00001"),
        ("email", "ada@example.test"),
    ]


@pytest.mark.parametrize(
    "read_category",
    [
        ReadResultCategory.ACTIVE_NOT_FOUND,
        ReadResultCategory.RETRYABLE_READ_FAILURE,
        ReadResultCategory.INDETERMINATE_READ,
    ],
)
def test_409_email_not_found_or_indeterminate_is_terminal(
    read_category: ReadResultCategory,
) -> None:
    engine = FakeCrmEngine([lead_row()])
    adapter = FakeAdapter(
        create_result(CreateResultCategory.CONFLICT_REQUIRES_LOOKUP),
        engine=engine,
        reference_result=read_result(ReadResultCategory.ACTIVE_NOT_FOUND),
        email_result=read_result(read_category),
    )

    outcome = deliver_one_crm_create(engine, adapter, delivery_settings(), now=NOW)

    row = engine.rows[0]
    assert outcome.action is CrmDeliveryAction.FAILED_ESCALATED
    assert row["delivery_status"] == "FAILED_ESCALATED"
    assert row["failure_at"] == NOW
    assert row["last_technical_failure_at"] == NOW
    assert row["next_retry_at"] is None
    assert row["next_reconciliation_at"] is None
    assert len(adapter.create_calls) == 1


def test_409_reference_not_found_without_email_is_terminal_without_email_lookup() -> None:
    engine = FakeCrmEngine([lead_row(email=None, phone="+359 888 111 222")])
    adapter = FakeAdapter(
        create_result(CreateResultCategory.CONFLICT_REQUIRES_LOOKUP),
        engine=engine,
        reference_result=read_result(ReadResultCategory.ACTIVE_NOT_FOUND),
    )

    outcome = deliver_one_crm_create(engine, adapter, delivery_settings(), now=NOW)

    assert outcome.action is CrmDeliveryAction.FAILED_ESCALATED
    assert engine.rows[0]["delivery_status"] == "FAILED_ESCALATED"
    assert engine.rows[0]["last_technical_failure_at"] == NOW
    assert adapter.lookup_calls == [("reference", "IR-00001")]


@pytest.mark.parametrize(
    "read_category",
    [
        ReadResultCategory.RETRYABLE_READ_FAILURE,
        ReadResultCategory.INDETERMINATE_READ,
    ],
)
def test_409_reference_indeterminate_is_terminal_without_email_lookup(
    read_category: ReadResultCategory,
) -> None:
    engine = FakeCrmEngine([lead_row()])
    adapter = FakeAdapter(
        create_result(CreateResultCategory.CONFLICT_REQUIRES_LOOKUP),
        engine=engine,
        reference_result=read_result(read_category),
    )

    outcome = deliver_one_crm_create(engine, adapter, delivery_settings(), now=NOW)

    assert outcome.action is CrmDeliveryAction.FAILED_ESCALATED
    assert engine.rows[0]["delivery_status"] == "FAILED_ESCALATED"
    assert engine.rows[0]["last_technical_failure_at"] == NOW
    assert adapter.lookup_calls == [("reference", "IR-00001")]


@pytest.mark.parametrize(
    ("properties", "expected_error_category"),
    [
        (
            {
                "integration_reference": "IR-00001",
                "email": "ada@example.test",
            },
            "CRM_REFERENCE_COLLISION",
        ),
        (
            {
                "integration_reference": "IR-99999",
                "email": "private-mismatch@example.test",
            },
            "CRM_DATA_INTEGRITY",
        ),
    ],
)
def test_400_reference_found_activates_config_pause_and_never_reads_email(
    properties: dict[str, str | None],
    expected_error_category: str,
) -> None:
    engine = FakeCrmEngine([lead_row()])
    adapter = FakeAdapter(
        create_result(CreateResultCategory.BAD_REQUEST_REQUIRES_SAFE_CLASSIFICATION),
        engine=engine,
        reference_result=read_result(
            ReadResultCategory.FOUND,
            contact_id="active-reference-contact",
            properties=properties,
        ),
    )

    outcome = deliver_one_crm_create(engine, adapter, delivery_settings(), now=NOW)

    row = engine.rows[0]
    assert outcome.action is CrmDeliveryAction.GLOBAL_PAUSE_ACTIVATED
    assert row["delivery_status"] == "PENDING"
    assert row["last_error_category"] == expected_error_category
    assert row["last_technical_failure_at"] is None
    assert "private-mismatch@example.test" not in row["last_error"]
    assert engine.runtime["pause_reason"] == "CONFIG"
    assert adapter.lookup_calls == [("reference", "IR-00001")]


@pytest.mark.parametrize(
    ("read_category", "pause_reason"),
    [
        (ReadResultCategory.AUTH_FAILURE, "AUTH"),
        (ReadResultCategory.CONFIG_FAILURE, "CONFIG"),
    ],
)
def test_400_reference_auth_or_config_activates_pause_without_email_lookup(
    read_category: ReadResultCategory,
    pause_reason: str,
) -> None:
    engine = FakeCrmEngine([lead_row(automatic_retry_count=2)])
    adapter = FakeAdapter(
        create_result(CreateResultCategory.BAD_REQUEST_REQUIRES_SAFE_CLASSIFICATION),
        engine=engine,
        reference_result=read_result(read_category),
    )

    outcome = deliver_one_crm_create(engine, adapter, delivery_settings(), now=NOW)

    assert outcome.action is CrmDeliveryAction.GLOBAL_PAUSE_ACTIVATED
    assert engine.rows[0]["delivery_status"] == "PENDING"
    assert engine.rows[0]["automatic_retry_count"] == 2
    assert engine.rows[0]["last_technical_failure_at"] is None
    assert engine.runtime["pause_reason"] == pause_reason
    assert adapter.lookup_calls == [("reference", "IR-00001")]


@pytest.mark.parametrize(
    "read_category",
    [
        ReadResultCategory.ACTIVE_NOT_FOUND,
        ReadResultCategory.RETRYABLE_READ_FAILURE,
        ReadResultCategory.INDETERMINATE_READ,
    ],
)
def test_400_not_found_or_indeterminate_is_terminal_without_email_lookup(
    read_category: ReadResultCategory,
) -> None:
    engine = FakeCrmEngine([lead_row()])
    adapter = FakeAdapter(
        create_result(CreateResultCategory.BAD_REQUEST_REQUIRES_SAFE_CLASSIFICATION),
        engine=engine,
        reference_result=read_result(read_category),
    )

    outcome = deliver_one_crm_create(engine, adapter, delivery_settings(), now=NOW)

    row = engine.rows[0]
    assert outcome.action is CrmDeliveryAction.FAILED_ESCALATED
    assert row["delivery_status"] == "FAILED_ESCALATED"
    assert row["failure_at"] == NOW
    assert row["last_technical_failure_at"] == NOW
    assert row["next_retry_at"] is None
    assert row["next_reconciliation_at"] is None
    assert adapter.lookup_calls == [("reference", "IR-00001")]
    assert len(adapter.create_calls) == 1


def test_unexpected_exception_during_followup_read_leaves_sending() -> None:
    class RaisingReadAdapter(FakeAdapter):
        def get_contact_by_integration_reference(
            self,
            value: str,
        ) -> ReadContactResult:
            self.lookup_calls.append(("reference", value))
            raise RuntimeError("fictional read crash")

    engine = FakeCrmEngine([lead_row()])
    adapter = RaisingReadAdapter(
        create_result(CreateResultCategory.CONFLICT_REQUIRES_LOOKUP),
        engine=engine,
    )

    with pytest.raises(RuntimeError, match="fictional read crash"):
        deliver_one_crm_create(engine, adapter, delivery_settings(), now=NOW)

    assert engine.rows[0]["delivery_status"] == "SENDING"
    assert engine.rows[0]["write_attempt_count"] == 1
    assert len(adapter.create_calls) == 1
    assert adapter.lookup_calls == [("reference", "IR-00001")]


def test_unexpected_exception_after_claim_leaves_sending_without_duplicate() -> None:
    engine = FakeCrmEngine([lead_row()])
    adapter = FakeAdapter(engine=engine, exception=RuntimeError("fictional failure"))

    with pytest.raises(RuntimeError, match="fictional failure"):
        deliver_one_crm_create(engine, adapter, delivery_settings(), now=NOW)

    assert engine.rows[0]["delivery_status"] == "SENDING"
    assert engine.rows[0]["write_attempt_count"] == 1
    assert len(adapter.create_calls) == 1

    blocked = deliver_one_crm_create(
        engine,
        FakeAdapter(create_result(CreateResultCategory.SUCCESS, contact_id="x")),
        delivery_settings(),
        now=NOW + timedelta(seconds=1),
    )
    assert blocked.action is CrmDeliveryAction.NO_WORK


def test_result_transition_rejects_unexpected_current_state() -> None:
    engine = FakeCrmEngine([lead_row(status="PENDING")])

    with pytest.raises(CrmDeliveryConsistencyError, match="expected SENDING"):
        apply_create_result(
            engine,
            1,
            create_result(CreateResultCategory.SUCCESS, contact_id="contact-1"),
            delivery_settings(),
            now=NOW,
        )

    assert engine.rows[0]["delivery_status"] == "PENDING"


def test_success_result_without_valid_contact_id_leaves_sending() -> None:
    engine = FakeCrmEngine([lead_row(status="SENDING")])

    with pytest.raises(CrmDeliveryConsistencyError, match="valid Contact ID"):
        apply_create_result(
            engine,
            1,
            create_result(CreateResultCategory.SUCCESS, contact_id="  "),
            delivery_settings(),
            now=NOW,
        )

    assert engine.rows[0]["delivery_status"] == "SENDING"


def test_sending_gate_is_clear_without_sending_row() -> None:
    engine = FakeCrmEngine([lead_row(status="PENDING")])

    outcome = check_or_recover_sending(engine, 900, now=NOW)

    assert outcome.status is SendingGateStatus.CLEAR
    assert outcome.lead_id is None
    gate_sql = str(engine.statements[0].compile(dialect=postgresql.dialect()))
    assert "ORDER BY normalized_leads.last_attempt_at ASC" in gate_sql
    assert "normalized_leads.id ASC" in gate_sql
    assert "LIMIT" in gate_sql
    assert "FOR UPDATE" in gate_sql


@pytest.mark.parametrize(
    ("age_seconds", "expected_status"),
    [
        (899, SendingGateStatus.BLOCKED),
        (900, SendingGateStatus.BLOCKED),
        (901, SendingGateStatus.RECOVERED_TO_UNKNOWN),
    ],
)
def test_sending_gate_uses_strict_older_than_lease_boundary(
    age_seconds: int,
    expected_status: SendingGateStatus,
) -> None:
    engine = FakeCrmEngine(
        [
            lead_row(
                status="SENDING",
                last_attempt_at=NOW - timedelta(seconds=age_seconds),
            )
        ]
    )

    outcome = check_or_recover_sending(engine, 900, now=NOW)

    assert outcome.status is expected_status
    expected_delivery = (
        "UNKNOWN"
        if expected_status is SendingGateStatus.RECOVERED_TO_UNKNOWN
        else "SENDING"
    )
    assert engine.rows[0]["delivery_status"] == expected_delivery


def test_stale_sending_becomes_immediately_due_unknown_without_counter_changes() -> None:
    first_failure = NOW - timedelta(days=1)
    engine = FakeCrmEngine(
        [
            lead_row(
                status="SENDING",
                last_attempt_at=NOW - timedelta(seconds=901),
                write_attempt_count=4,
                automatic_retry_count=2,
                reconciliation_not_found_count=2,
                reconciliation_error_count=2,
                next_retry_at=NOW + timedelta(hours=1),
                first_failure_at=first_failure,
            )
        ]
    )

    outcome = check_or_recover_sending(engine, 900, now=NOW)

    row = engine.rows[0]
    assert outcome.status is SendingGateStatus.RECOVERED_TO_UNKNOWN
    assert outcome.lead_id == 1
    assert row["delivery_status"] == "UNKNOWN"
    assert row["next_reconciliation_at"] == NOW
    assert row["reconciliation_not_found_count"] == 0
    assert row["reconciliation_error_count"] == 0
    assert row["next_retry_at"] is None
    assert row["last_technical_failure_at"] == NOW
    assert row["last_error_category"] == "STALE_SENDING_UNKNOWN"
    assert len(row["last_error"]) <= 1_000
    assert row["first_failure_at"] == first_failure
    assert row["last_failure_at"] == NOW
    assert row["automatic_retry_count"] == 2
    assert row["write_attempt_count"] == 4


def test_sending_gate_recovers_only_deterministic_oldest_row() -> None:
    oldest_high_id = lead_row(
        3,
        "SENDING",
        last_attempt_at=NOW - timedelta(minutes=30),
    )
    oldest_low_id = lead_row(
        2,
        "SENDING",
        last_attempt_at=NOW - timedelta(minutes=30),
    )
    newer = lead_row(
        1,
        "SENDING",
        last_attempt_at=NOW - timedelta(minutes=20),
    )
    engine = FakeCrmEngine([newer, oldest_high_id, oldest_low_id])

    outcome = check_or_recover_sending(engine, 900, now=NOW)

    assert outcome.lead_id == 2
    assert sum(row["delivery_status"] == "UNKNOWN" for row in engine.rows) == 1
    assert oldest_low_id["delivery_status"] == "UNKNOWN"


def test_fresh_sending_blocks_create_and_reconciliation_http() -> None:
    engine = FakeCrmEngine(
        [
            lead_row(1, "SENDING", last_attempt_at=NOW - timedelta(minutes=1)),
            lead_row(2, "PENDING"),
            lead_row(3, "UNKNOWN", next_reconciliation_at=NOW),
        ]
    )
    adapter = FakeAdapter(
        create_result(CreateResultCategory.SUCCESS, contact_id="never-used"),
        engine=engine,
        reference_result=read_result(ReadResultCategory.ACTIVE_NOT_FOUND),
    )

    create_outcome = deliver_one_crm_create(
        engine, adapter, delivery_settings(), now=NOW
    )
    reconcile_outcome = reconcile_one_unknown(
        engine, adapter, delivery_settings(), now=NOW
    )

    assert create_outcome.action is CrmDeliveryAction.NO_WORK
    assert reconcile_outcome.action is CrmDeliveryAction.CRM_BLOCKED
    assert adapter.create_calls == []
    assert adapter.lookup_calls == []


def test_stale_recovery_blocks_pending_create_and_prioritizes_reconciliation() -> None:
    stale = lead_row(
        1,
        "SENDING",
        last_attempt_at=NOW - timedelta(minutes=16),
    )
    pending = lead_row(2, "PENDING")
    engine = FakeCrmEngine([pending, stale])
    create_adapter = FakeAdapter(
        create_result(CreateResultCategory.SUCCESS, contact_id="must-not-create"),
        engine=engine,
    )

    outcome = deliver_one_crm_create(
        engine, create_adapter, delivery_settings(), now=NOW
    )

    assert outcome.action is CrmDeliveryAction.NO_WORK
    assert stale["delivery_status"] == "UNKNOWN"
    assert pending["delivery_status"] == "PENDING"
    assert create_adapter.create_calls == []

    read_adapter = FakeAdapter(
        engine=engine,
        reference_result=read_result(ReadResultCategory.ACTIVE_NOT_FOUND),
    )
    reconcile_one_unknown(engine, read_adapter, delivery_settings(), now=NOW)
    assert read_adapter.lookup_calls == [("reference", "IR-00001")]


def test_existing_due_unknown_blocks_a_later_pending_create() -> None:
    engine = FakeCrmEngine(
        [
            lead_row(1, "UNKNOWN", next_reconciliation_at=NOW),
            lead_row(2, "PENDING"),
        ]
    )
    adapter = FakeAdapter(
        create_result(CreateResultCategory.SUCCESS, contact_id="must-not-create"),
        engine=engine,
    )

    outcome = deliver_one_crm_create(engine, adapter, delivery_settings(), now=NOW)

    assert outcome.action is CrmDeliveryAction.NO_WORK
    assert engine.rows[1]["delivery_status"] == "PENDING"
    assert adapter.create_calls == []


def test_unknown_selection_is_due_deterministic_and_bounded() -> None:
    future = lead_row(
        4,
        "UNKNOWN",
        next_reconciliation_at=NOW + timedelta(seconds=1),
    )
    tie_high = lead_row(
        3,
        "UNKNOWN",
        next_reconciliation_at=NOW - timedelta(minutes=5),
    )
    tie_low = lead_row(
        2,
        "UNKNOWN",
        next_reconciliation_at=NOW - timedelta(minutes=5),
    )
    later_due = lead_row(
        1,
        "UNKNOWN",
        next_reconciliation_at=NOW - timedelta(minutes=1),
    )
    engine = FakeCrmEngine([future, tie_high, later_due, tie_low])

    selected = select_one_due_unknown(engine, 900, now=NOW)

    assert selected is not None and selected.lead_id == 2
    assert all(row["delivery_status"] == "UNKNOWN" for row in engine.rows)
    selection_sql = next(
        str(statement.compile(dialect=postgresql.dialect()))
        for statement in engine.statements
        if isinstance(statement, Select)
        and "ORDER BY normalized_leads.next_reconciliation_at ASC" in str(statement)
    )
    assert "normalized_leads.id ASC" in selection_sql
    assert "LIMIT" in selection_sql

    future_only = FakeCrmEngine([future])
    assert select_one_due_unknown(future_only, 900, now=NOW) is None


def test_global_pause_prevents_unknown_read_and_preserves_row() -> None:
    original = lead_row(
        1,
        "UNKNOWN",
        next_reconciliation_at=NOW,
        reconciliation_not_found_count=2,
        reconciliation_error_count=1,
    )
    engine = FakeCrmEngine([deepcopy(original)], runtime=runtime_row(paused=True))
    adapter = FakeAdapter(
        engine=engine,
        reference_result=read_result(ReadResultCategory.ACTIVE_NOT_FOUND),
    )

    outcome = reconcile_one_unknown(engine, adapter, delivery_settings(), now=NOW)

    assert outcome.action is CrmDeliveryAction.NO_WORK
    assert adapter.lookup_calls == []
    assert engine.rows[0] == original


def test_unknown_found_exact_becomes_success_and_preserves_history() -> None:
    first_failure = NOW - timedelta(days=3)
    last_failure = NOW - timedelta(days=2)
    technical_failure = NOW - timedelta(days=1)
    engine = FakeCrmEngine(
        [
            lead_row(
                1,
                "UNKNOWN",
                next_reconciliation_at=NOW,
                reconciliation_not_found_count=2,
                reconciliation_error_count=2,
                automatic_retry_count=2,
                write_attempt_count=4,
                first_failure_at=first_failure,
                last_failure_at=last_failure,
                last_technical_failure_at=technical_failure,
            )
        ]
    )
    adapter = FakeAdapter(
        engine=engine,
        reference_result=read_result(
            ReadResultCategory.FOUND,
            contact_id="reconciled-contact-1",
            properties={
                "integration_reference": "IR-00001",
                "email": "ada@example.test",
            },
        ),
    )

    outcome = reconcile_one_unknown(engine, adapter, delivery_settings(), now=NOW)

    row = engine.rows[0]
    assert outcome.action is CrmDeliveryAction.SUCCESS
    assert row["delivery_status"] == "SUCCESS"
    assert row["crm_record_id"] == "reconciled-contact-1"
    assert row["sent_at"] == NOW
    assert row["reconciliation_not_found_count"] == 0
    assert row["reconciliation_error_count"] == 0
    assert row["next_reconciliation_at"] is None
    assert row["next_retry_at"] is None
    assert row["last_error_category"] is None
    assert row["last_error_code"] is None
    assert row["last_error"] is None
    assert row["first_failure_at"] == first_failure
    assert row["last_failure_at"] == last_failure
    assert row["last_technical_failure_at"] == technical_failure
    assert row["automatic_retry_count"] == 2
    assert row["write_attempt_count"] == 4
    assert adapter.create_calls == []
    assert adapter.lookup_calls == [("reference", "IR-00001")]


@pytest.mark.parametrize(
    "properties",
    [
        {"integration_reference": "IR-99999", "email": "ada@example.test"},
        {
            "integration_reference": "IR-00001",
            "email": "returned-private@example.test",
        },
        {"email": "ada@example.test"},
        {"integration_reference": "IR-00001"},
    ],
)
def test_unknown_found_mismatch_remains_unknown_and_activates_config_pause(
    properties: dict[str, str | None],
) -> None:
    prior_technical = NOW - timedelta(days=1)
    engine = FakeCrmEngine(
        [
            lead_row(
                1,
                "UNKNOWN",
                next_reconciliation_at=NOW,
                reconciliation_not_found_count=2,
                reconciliation_error_count=1,
                last_technical_failure_at=prior_technical,
            )
        ]
    )
    adapter = FakeAdapter(
        engine=engine,
        reference_result=read_result(
            ReadResultCategory.FOUND,
            contact_id="contact-mismatch",
            properties=properties,
        ),
    )

    outcome = reconcile_one_unknown(engine, adapter, delivery_settings(), now=NOW)

    row = engine.rows[0]
    assert outcome.action is CrmDeliveryAction.GLOBAL_PAUSE_ACTIVATED
    assert row["delivery_status"] == "UNKNOWN"
    assert row["reconciliation_not_found_count"] == 2
    assert row["reconciliation_error_count"] == 1
    assert row["next_reconciliation_at"] == NOW
    assert row["last_technical_failure_at"] == prior_technical
    assert row["last_error_category"] == "CRM_DATA_INTEGRITY"
    assert "returned-private@example.test" not in row["last_error"]
    assert engine.runtime["pause_reason"] == "CONFIG"


@pytest.mark.parametrize(
    ("starting_count", "expected_status", "expected_due"),
    [
        (0, "UNKNOWN", NOW + timedelta(seconds=60)),
        (1, "UNKNOWN", NOW + timedelta(seconds=300)),
        (2, "UNKNOWN_ESCALATED", None),
    ],
)
def test_active_not_found_schedule_and_third_check_escalation(
    starting_count: int,
    expected_status: str,
    expected_due: datetime | None,
) -> None:
    engine = FakeCrmEngine(
        [
            lead_row(
                1,
                "UNKNOWN",
                next_reconciliation_at=NOW,
                reconciliation_not_found_count=starting_count,
                reconciliation_error_count=2,
            )
        ]
    )
    adapter = FakeAdapter(
        engine=engine,
        reference_result=read_result(ReadResultCategory.ACTIVE_NOT_FOUND),
    )

    outcome = reconcile_one_unknown(engine, adapter, delivery_settings(), now=NOW)

    row = engine.rows[0]
    assert row["delivery_status"] == expected_status
    assert row["reconciliation_not_found_count"] == starting_count + 1
    assert row["reconciliation_error_count"] == 0
    assert row["next_reconciliation_at"] == expected_due
    assert row["next_retry_at"] is None
    assert adapter.create_calls == []
    if starting_count == 2:
        assert outcome.action is CrmDeliveryAction.UNKNOWN_ESCALATED
        assert row["failure_at"] == NOW
        assert row["last_technical_failure_at"] == NOW
        later = reconcile_one_unknown(
            engine,
            adapter,
            delivery_settings(),
            now=NOW + timedelta(days=1),
        )
        assert later.action is CrmDeliveryAction.NO_WORK
        assert len(adapter.lookup_calls) == 1
    else:
        assert outcome.action is CrmDeliveryAction.UNKNOWN
        assert row["failure_at"] is None


@pytest.mark.parametrize(
    "read_category",
    [
        ReadResultCategory.RETRYABLE_READ_FAILURE,
        ReadResultCategory.INDETERMINATE_READ,
    ],
)
@pytest.mark.parametrize(
    ("starting_count", "expected_status", "expected_due"),
    [
        (0, "UNKNOWN", NOW + timedelta(seconds=300)),
        (1, "UNKNOWN", NOW + timedelta(seconds=1_800)),
        (2, "UNKNOWN_ESCALATED", None),
    ],
)
def test_reconciliation_error_schedule_and_third_error_escalation(
    read_category: ReadResultCategory,
    starting_count: int,
    expected_status: str,
    expected_due: datetime | None,
) -> None:
    engine = FakeCrmEngine(
        [
            lead_row(
                1,
                "UNKNOWN",
                next_reconciliation_at=NOW,
                reconciliation_not_found_count=2,
                reconciliation_error_count=starting_count,
            )
        ]
    )
    adapter = FakeAdapter(
        engine=engine,
        reference_result=read_result(read_category),
    )

    outcome = reconcile_one_unknown(engine, adapter, delivery_settings(), now=NOW)

    row = engine.rows[0]
    assert row["delivery_status"] == expected_status
    assert row["reconciliation_not_found_count"] == 2
    assert row["reconciliation_error_count"] == starting_count + 1
    assert row["next_reconciliation_at"] == expected_due
    assert row["last_technical_failure_at"] == NOW
    assert adapter.create_calls == []
    if starting_count == 2:
        assert outcome.action is CrmDeliveryAction.UNKNOWN_ESCALATED
        assert row["failure_at"] == NOW
    else:
        assert outcome.action is CrmDeliveryAction.UNKNOWN


def test_completed_not_found_resets_prior_error_counter() -> None:
    engine = FakeCrmEngine(
        [
            lead_row(
                1,
                "UNKNOWN",
                next_reconciliation_at=NOW,
                reconciliation_not_found_count=0,
                reconciliation_error_count=1,
            )
        ]
    )
    adapter = FakeAdapter(
        engine=engine,
        reference_result=read_result(ReadResultCategory.ACTIVE_NOT_FOUND),
    )

    reconcile_one_unknown(engine, adapter, delivery_settings(), now=NOW)

    assert engine.rows[0]["reconciliation_not_found_count"] == 1
    assert engine.rows[0]["reconciliation_error_count"] == 0


def test_completed_found_resets_prior_error_counter() -> None:
    engine = FakeCrmEngine(
        [
            lead_row(
                1,
                "UNKNOWN",
                next_reconciliation_at=NOW,
                reconciliation_error_count=2,
            )
        ]
    )
    adapter = FakeAdapter(
        engine=engine,
        reference_result=read_result(
            ReadResultCategory.FOUND,
            contact_id="contact-1",
            properties={
                "integration_reference": "IR-00001",
                "email": "ada@example.test",
            },
        ),
    )

    reconcile_one_unknown(engine, adapter, delivery_settings(), now=NOW)

    assert engine.rows[0]["delivery_status"] == "SUCCESS"
    assert engine.rows[0]["reconciliation_error_count"] == 0


@pytest.mark.parametrize(
    ("retry_after", "expected"),
    [
        ("900", NOW + timedelta(seconds=900)),
        ("60", NOW + timedelta(seconds=300)),
        (format_datetime(NOW + timedelta(minutes=20), usegmt=True), NOW + timedelta(minutes=20)),
        ("not-a-valid-delay", NOW + timedelta(seconds=300)),
    ],
)
def test_reconciliation_retry_after_uses_later_valid_time(
    retry_after: str,
    expected: datetime,
) -> None:
    engine = FakeCrmEngine(
        [
            lead_row(
                1,
                "UNKNOWN",
                next_reconciliation_at=NOW,
                reconciliation_error_count=0,
            )
        ]
    )
    adapter = FakeAdapter(
        engine=engine,
        reference_result=read_result(
            ReadResultCategory.RETRYABLE_READ_FAILURE,
            retry_after=retry_after,
        ),
    )

    reconcile_one_unknown(engine, adapter, delivery_settings(), now=NOW)

    assert engine.rows[0]["next_reconciliation_at"] == expected


@pytest.mark.parametrize(
    ("read_category", "pause_reason"),
    [
        (ReadResultCategory.AUTH_FAILURE, "AUTH"),
        (ReadResultCategory.CONFIG_FAILURE, "CONFIG"),
    ],
)
def test_reconciliation_auth_config_preserves_unknown_evidence_and_pauses(
    read_category: ReadResultCategory,
    pause_reason: str,
) -> None:
    prior_technical = NOW - timedelta(days=1)
    due_at = NOW - timedelta(seconds=1)
    engine = FakeCrmEngine(
        [
            lead_row(
                1,
                "UNKNOWN",
                next_reconciliation_at=due_at,
                reconciliation_not_found_count=2,
                reconciliation_error_count=1,
                last_technical_failure_at=prior_technical,
            )
        ]
    )
    adapter = FakeAdapter(
        engine=engine,
        reference_result=read_result(read_category),
    )

    outcome = reconcile_one_unknown(engine, adapter, delivery_settings(), now=NOW)

    row = engine.rows[0]
    assert outcome.action is CrmDeliveryAction.GLOBAL_PAUSE_ACTIVATED
    assert row["delivery_status"] == "UNKNOWN"
    assert row["reconciliation_not_found_count"] == 2
    assert row["reconciliation_error_count"] == 1
    assert row["next_reconciliation_at"] == due_at
    assert row["last_technical_failure_at"] == prior_technical
    assert engine.runtime["crm_delivery_paused"] is True
    assert engine.runtime["pause_reason"] == pause_reason


@pytest.mark.parametrize(
    ("existing_reason", "new_category"),
    [
        ("AUTH", ReadResultCategory.CONFIG_FAILURE),
        ("CONFIG", ReadResultCategory.AUTH_FAILURE),
    ],
)
def test_existing_pause_context_is_preserved_if_pause_appears_during_read(
    existing_reason: str,
    new_category: ReadResultCategory,
) -> None:
    existing_paused_at = NOW - timedelta(hours=2)
    existing_alert_sent = NOW - timedelta(hours=1)
    engine = FakeCrmEngine(
        [lead_row(1, "UNKNOWN", next_reconciliation_at=NOW)]
    )

    class PauseDuringReadAdapter(FakeAdapter):
        def get_contact_by_integration_reference(
            self,
            value: str,
        ) -> ReadContactResult:
            self.lookup_calls.append(("reference", value))
            assert self.engine is not None and self.engine.transaction_depth == 0
            self.engine.runtime.update(
                crm_delivery_paused=True,
                pause_reason=existing_reason,
                paused_at=existing_paused_at,
                pause_alert_sent_at=existing_alert_sent,
                pause_alert_last_error="preserve-me",
            )
            assert self.reference_result is not None
            return self.reference_result

    adapter = PauseDuringReadAdapter(
        engine=engine,
        reference_result=read_result(new_category),
    )

    reconcile_one_unknown(engine, adapter, delivery_settings(), now=NOW)

    assert engine.runtime["pause_reason"] == existing_reason
    assert engine.runtime["paused_at"] == existing_paused_at
    assert engine.runtime["pause_alert_sent_at"] == existing_alert_sent
    assert engine.runtime["pause_alert_last_error"] == "preserve-me"


def test_unexpected_reconciliation_exception_consumes_no_evidence() -> None:
    original = lead_row(
        1,
        "UNKNOWN",
        next_reconciliation_at=NOW,
        reconciliation_not_found_count=1,
        reconciliation_error_count=1,
    )
    engine = FakeCrmEngine([deepcopy(original)])

    class RaisingAdapter(FakeAdapter):
        def get_contact_by_integration_reference(
            self,
            value: str,
        ) -> ReadContactResult:
            self.lookup_calls.append(("reference", value))
            raise RuntimeError("fictional reconciliation crash")

    adapter = RaisingAdapter(engine=engine)

    with pytest.raises(RuntimeError, match="fictional reconciliation crash"):
        reconcile_one_unknown(engine, adapter, delivery_settings(), now=NOW)

    assert engine.rows[0] == original
    assert adapter.lookup_calls == [("reference", "IR-00001")]
    assert adapter.create_calls == []


def test_reconciliation_persistence_rollback_consumes_no_evidence_and_read_repeats() -> None:
    original = lead_row(
        1,
        "UNKNOWN",
        next_reconciliation_at=NOW,
        reconciliation_not_found_count=0,
        reconciliation_error_count=0,
    )
    engine = FakeCrmEngine([deepcopy(original)], fail_next_lead_update=True)
    adapter = FakeAdapter(
        engine=engine,
        reference_result=read_result(ReadResultCategory.ACTIVE_NOT_FOUND),
    )

    with pytest.raises(RuntimeError, match="fictional persistence failure"):
        reconcile_one_unknown(engine, adapter, delivery_settings(), now=NOW)

    assert engine.rows[0] == original
    assert adapter.lookup_calls == [("reference", "IR-00001")]

    reconcile_one_unknown(engine, adapter, delivery_settings(), now=NOW)
    assert adapter.lookup_calls == [
        ("reference", "IR-00001"),
        ("reference", "IR-00001"),
    ]
    assert engine.rows[0]["reconciliation_not_found_count"] == 1


def test_process_one_crm_work_reconciles_due_unknown_before_pending_create() -> None:
    unknown = lead_row(1, "UNKNOWN", next_reconciliation_at=NOW)
    pending = lead_row(2, "PENDING")
    engine = FakeCrmEngine([unknown, pending])
    adapter = FakeAdapter(
        result=create_result(CreateResultCategory.SUCCESS, contact_id="new-contact"),
        engine=engine,
        reference_result=read_result(
            ReadResultCategory.FOUND,
            contact_id="reconciled-contact",
            properties={
                "integration_reference": "IR-00001",
                "email": "ada@example.test",
            },
        ),
    )

    outcome = process_one_crm_work(engine, adapter, delivery_settings(), now=NOW)

    assert outcome.action is CrmDeliveryAction.SUCCESS
    assert adapter.lookup_calls == [("reference", "IR-00001")]
    assert adapter.create_calls == []
    assert engine.rows[0]["delivery_status"] == "SUCCESS"
    assert engine.rows[1]["delivery_status"] == "PENDING"


def test_process_one_crm_work_creates_at_most_one_pending_lead() -> None:
    engine = FakeCrmEngine([lead_row(1), lead_row(2)])
    adapter = FakeAdapter(
        result=create_result(CreateResultCategory.SUCCESS, contact_id="contact-1"),
        engine=engine,
    )

    outcome = process_one_crm_work(engine, adapter, delivery_settings(), now=NOW)

    assert outcome.action is CrmDeliveryAction.SUCCESS
    assert len(adapter.create_calls) == 1
    assert engine.rows[0]["delivery_status"] == "SUCCESS"
    assert engine.rows[1]["delivery_status"] == "PENDING"


def test_process_one_crm_work_pause_blocks_reads_and_creates() -> None:
    engine = FakeCrmEngine(
        [lead_row(1, "UNKNOWN", next_reconciliation_at=NOW), lead_row(2)],
        runtime=runtime_row(paused=True),
    )
    adapter = FakeAdapter(
        result=create_result(CreateResultCategory.SUCCESS, contact_id="contact-1"),
        engine=engine,
        reference_result=read_result(ReadResultCategory.ACTIVE_NOT_FOUND),
    )

    outcome = process_one_crm_work(engine, adapter, delivery_settings(), now=NOW)

    assert outcome.action is CrmDeliveryAction.NO_WORK
    assert adapter.lookup_calls == []
    assert adapter.create_calls == []
    assert [row["delivery_status"] for row in engine.rows] == ["UNKNOWN", "PENDING"]
