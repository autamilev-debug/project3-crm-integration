from sqlalchemy import CheckConstraint, DateTime, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB

from project3_crm.db.schema import (
    DELIVERY_STATUSES,
    WEBHOOK_EVENT_STATUSES,
    integration_runtime_state,
    metadata,
    normalized_leads,
    normalized_leads_id_sequence,
    webhook_events,
)


def _unique_column_sets(table: object) -> set[tuple[str, ...]]:
    return {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }


def _check_constraint_sql(table: object) -> str:
    return "\n".join(
        str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    )


def _index_columns(table: object) -> dict[str, tuple[str, ...]]:
    return {
        index.name: tuple(column.name for column in index.columns)
        for index in table.indexes
    }


def test_metadata_contains_only_the_three_frozen_tables() -> None:
    assert set(metadata.tables) == {
        "webhook_events",
        "normalized_leads",
        "integration_runtime_state",
    }


def test_webhook_events_schema_and_idempotency_constraint() -> None:
    assert set(webhook_events.c.keys()) == {
        "id",
        "source",
        "event_id",
        "raw_payload",
        "event_status",
        "received_at",
        "processing_started_at",
        "normalized_at",
        "completed_at",
        "last_error_category",
        "last_error",
        "escalation_notified_at",
        "created_at",
        "updated_at",
    }
    assert isinstance(webhook_events.c.raw_payload.type, JSONB)
    assert webhook_events.c.completed_at.nullable is True
    assert ("source", "event_id") in _unique_column_sets(webhook_events)

    checks = _check_constraint_sql(webhook_events)
    for status in WEBHOOK_EVENT_STATUSES:
        assert f"'{status}'" in checks
    assert "char_length(event_id) BETWEEN 1 AND 128" in checks


def test_normalized_lead_relational_and_reference_invariants() -> None:
    assert set(normalized_leads.c.keys()) == {
        "id",
        "webhook_event_id",
        "integration_reference",
        "source",
        "source_event_id",
        "submitted_at",
        "first_name",
        "last_name",
        "email",
        "phone",
        "company",
        "job_title",
        "country",
        "city",
        "campaign",
        "lead_source",
        "source_metadata",
        "delivery_status",
        "write_attempt_count",
        "automatic_retry_count",
        "next_retry_at",
        "last_attempt_at",
        "first_failure_at",
        "last_failure_at",
        "reconciliation_not_found_count",
        "reconciliation_error_count",
        "next_reconciliation_at",
        "last_error_category",
        "last_error_code",
        "last_error",
        "crm_record_id",
        "crm_correlation_id",
        "sent_at",
        "failure_at",
        "escalation_notified_at",
        "last_technical_failure_at",
        "cluster_alert_pending_at",
        "cluster_alerted_at",
        "created_at",
        "updated_at",
    }
    assert isinstance(normalized_leads.c.source_metadata.type, JSONB)
    assert ("webhook_event_id",) in _unique_column_sets(normalized_leads)
    assert ("integration_reference",) in _unique_column_sets(normalized_leads)

    foreign_keys = [
        constraint
        for constraint in normalized_leads.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    ]
    assert len(foreign_keys) == 1
    foreign_key = foreign_keys[0]
    assert tuple(foreign_key.column_keys) == ("webhook_event_id",)
    assert next(iter(foreign_key.elements)).target_fullname == "webhook_events.id"
    assert foreign_key.ondelete == "RESTRICT"

    assert normalized_leads_id_sequence.name == "normalized_leads_id_seq"
    assert normalized_leads.c.id.default is normalized_leads_id_sequence


def test_normalized_lead_status_and_counter_constraints() -> None:
    checks = _check_constraint_sql(normalized_leads)
    for status in DELIVERY_STATUSES:
        assert f"'{status}'" in checks

    assert "write_attempt_count >= 0" in checks
    assert "automatic_retry_count BETWEEN 0 AND 3" in checks
    assert "reconciliation_not_found_count >= 0" in checks
    assert "reconciliation_error_count >= 0" in checks
    assert "integration_reference ~ '^IR-[0-9]{5,}$'" in checks
    assert "crm_record_id IS NOT NULL AND sent_at IS NOT NULL" in checks
    assert "next_retry_at IS NOT NULL" in checks
    assert "next_reconciliation_at IS NOT NULL" in checks
    assert "failure_at IS NOT NULL" in checks


def test_real_moment_columns_are_timezone_aware() -> None:
    webhook_timestamps = {
        "received_at",
        "processing_started_at",
        "normalized_at",
        "completed_at",
        "escalation_notified_at",
        "created_at",
        "updated_at",
    }
    normalized_timestamps = {
        "submitted_at",
        "next_retry_at",
        "last_attempt_at",
        "first_failure_at",
        "last_failure_at",
        "next_reconciliation_at",
        "sent_at",
        "failure_at",
        "escalation_notified_at",
        "last_technical_failure_at",
        "cluster_alert_pending_at",
        "cluster_alerted_at",
        "created_at",
        "updated_at",
    }
    runtime_timestamps = {
        "paused_at",
        "pause_alert_sent_at",
        "last_crm_batch_attempt_at",
        "last_crm_batch_success_at",
        "last_normalization_batch_attempt_at",
        "last_normalization_batch_success_at",
        "updated_at",
    }

    for table, column_names in (
        (webhook_events, webhook_timestamps),
        (normalized_leads, normalized_timestamps),
        (integration_runtime_state, runtime_timestamps),
    ):
        for column_name in column_names:
            column_type = table.c[column_name].type
            assert isinstance(column_type, DateTime)
            assert column_type.timezone is True


def test_runtime_singleton_and_independent_scheduler_fields() -> None:
    assert set(integration_runtime_state.c.keys()) == {
        "id",
        "crm_delivery_paused",
        "pause_reason",
        "paused_at",
        "pause_alert_sent_at",
        "pause_alert_last_error",
        "last_crm_batch_attempt_at",
        "last_crm_batch_success_at",
        "last_crm_batch_error",
        "last_normalization_batch_attempt_at",
        "last_normalization_batch_success_at",
        "last_normalization_batch_error",
        "updated_at",
    }
    assert integration_runtime_state.c.id.autoincrement is False
    checks = _check_constraint_sql(integration_runtime_state)
    assert "id = 1" in checks
    assert "pause_reason IN ('AUTH', 'CONFIG')" in checks
    assert "crm_delivery_paused = false" in checks
    assert "crm_delivery_paused = true" in checks


def test_indexes_match_frozen_worker_and_notification_access_patterns() -> None:
    assert _index_columns(webhook_events) == {
        "ix_webhook_events_status_received_at": ("event_status", "received_at"),
        "ix_webhook_events_status_processing_started_at": (
            "event_status",
            "processing_started_at",
        ),
        "ix_webhook_events_status_escalation_notified_at": (
            "event_status",
            "escalation_notified_at",
        ),
    }
    assert _index_columns(normalized_leads) == {
        "ix_normalized_leads_status_next_retry_at": (
            "delivery_status",
            "next_retry_at",
        ),
        "ix_normalized_leads_status_next_reconciliation_at": (
            "delivery_status",
            "next_reconciliation_at",
        ),
        "ix_normalized_leads_status_last_attempt_at": (
            "delivery_status",
            "last_attempt_at",
        ),
        "ix_normalized_leads_status_escalation_notified_at": (
            "delivery_status",
            "escalation_notified_at",
        ),
        "ix_normalized_leads_last_technical_failure_at": (
            "last_technical_failure_at",
        ),
    }
