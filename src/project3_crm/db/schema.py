"""SQLAlchemy Core schema for the Project #3 PostgreSQL persistence layer."""

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    Sequence,
    SmallInteger,
    String,
    Table,
    UniqueConstraint,
    false,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB


SOURCES = ("website", "linkedin", "partner")
WEBHOOK_EVENT_STATUSES = (
    "RECEIVED",
    "PROCESSING",
    "NORMALIZED",
    "NORMALIZATION_FAILED",
)
DELIVERY_STATUSES = (
    "PENDING",
    "SENDING",
    "RETRY_PENDING",
    "UNKNOWN",
    "SUCCESS",
    "EXISTING_CONTACT",
    "FAILED_ESCALATED",
    "UNKNOWN_ESCALATED",
)
PAUSE_REASONS = ("AUTH", "CONFIG")

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)

webhook_events = Table(
    "webhook_events",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("source", String(32), nullable=False),
    Column("event_id", String(128), nullable=False),
    Column("raw_payload", JSONB, nullable=False),
    Column("event_status", String(32), nullable=False),
    Column("received_at", DateTime(timezone=True), nullable=False),
    Column("processing_started_at", DateTime(timezone=True)),
    Column("normalized_at", DateTime(timezone=True)),
    Column("completed_at", DateTime(timezone=True)),
    Column("last_error_category", String(64)),
    Column("last_error", String(1000)),
    Column("escalation_notified_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "source IN ('website', 'linkedin', 'partner')",
        name="source_allowed",
    ),
    CheckConstraint(
        "event_status IN "
        "('RECEIVED', 'PROCESSING', 'NORMALIZED', 'NORMALIZATION_FAILED')",
        name="event_status_allowed",
    ),
    CheckConstraint(
        "char_length(event_id) BETWEEN 1 AND 128",
        name="event_id_length",
    ),
    UniqueConstraint("source", "event_id", name="uq_webhook_events_source_event_id"),
)

Index(
    "ix_webhook_events_status_received_at",
    webhook_events.c.event_status,
    webhook_events.c.received_at,
)
Index(
    "ix_webhook_events_status_processing_started_at",
    webhook_events.c.event_status,
    webhook_events.c.processing_started_at,
)
Index(
    "ix_webhook_events_status_escalation_notified_at",
    webhook_events.c.event_status,
    webhook_events.c.escalation_notified_at,
)

normalized_leads_id_sequence = Sequence(
    "normalized_leads_id_seq",
    metadata=metadata,
)

normalized_leads = Table(
    "normalized_leads",
    metadata,
    Column(
        "id",
        BigInteger,
        normalized_leads_id_sequence,
        server_default=normalized_leads_id_sequence.next_value(),
        primary_key=True,
    ),
    Column("webhook_event_id", BigInteger, nullable=False),
    Column("integration_reference", String(32), nullable=False),
    Column("source", String(32), nullable=False),
    Column("source_event_id", String(128), nullable=False),
    Column("submitted_at", DateTime(timezone=True)),
    Column("first_name", String(100)),
    Column("last_name", String(100)),
    Column("email", String(254)),
    Column("phone", String(64)),
    Column("company", String(200)),
    Column("job_title", String(150)),
    Column("country", String(100)),
    Column("city", String(100)),
    Column("campaign", String(200)),
    Column("lead_source", String(64)),
    Column(
        "source_metadata",
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    ),
    Column("delivery_status", String(32), nullable=False),
    Column(
        "write_attempt_count",
        Integer,
        nullable=False,
        server_default=text("0"),
    ),
    Column(
        "automatic_retry_count",
        Integer,
        nullable=False,
        server_default=text("0"),
    ),
    Column("next_retry_at", DateTime(timezone=True)),
    Column("last_attempt_at", DateTime(timezone=True)),
    Column("first_failure_at", DateTime(timezone=True)),
    Column("last_failure_at", DateTime(timezone=True)),
    Column(
        "reconciliation_not_found_count",
        Integer,
        nullable=False,
        server_default=text("0"),
    ),
    Column(
        "reconciliation_error_count",
        Integer,
        nullable=False,
        server_default=text("0"),
    ),
    Column("next_reconciliation_at", DateTime(timezone=True)),
    Column("last_error_category", String(64)),
    Column("last_error_code", String(128)),
    Column("last_error", String(1000)),
    Column("crm_record_id", String(64)),
    Column("crm_correlation_id", String(128)),
    Column("sent_at", DateTime(timezone=True)),
    Column("failure_at", DateTime(timezone=True)),
    Column("escalation_notified_at", DateTime(timezone=True)),
    Column("last_technical_failure_at", DateTime(timezone=True)),
    Column("cluster_alert_pending_at", DateTime(timezone=True)),
    Column("cluster_alerted_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["webhook_event_id"],
        ["webhook_events.id"],
        name="fk_normalized_leads_webhook_event_id_webhook_events",
        ondelete="RESTRICT",
    ),
    UniqueConstraint(
        "webhook_event_id",
        name="uq_normalized_leads_webhook_event_id",
    ),
    UniqueConstraint(
        "integration_reference",
        name="uq_normalized_leads_integration_reference",
    ),
    CheckConstraint(
        "source IN ('website', 'linkedin', 'partner')",
        name="source_allowed",
    ),
    CheckConstraint(
        "delivery_status IN "
        "('PENDING', 'SENDING', 'RETRY_PENDING', 'UNKNOWN', 'SUCCESS', "
        "'EXISTING_CONTACT', 'FAILED_ESCALATED', 'UNKNOWN_ESCALATED')",
        name="delivery_status_allowed",
    ),
    CheckConstraint("write_attempt_count >= 0", name="write_attempt_count_nonnegative"),
    CheckConstraint(
        "automatic_retry_count BETWEEN 0 AND 3",
        name="automatic_retry_count_range",
    ),
    CheckConstraint(
        "reconciliation_not_found_count >= 0",
        name="reconciliation_not_found_count_nonnegative",
    ),
    CheckConstraint(
        "reconciliation_error_count >= 0",
        name="reconciliation_error_count_nonnegative",
    ),
    CheckConstraint(
        "integration_reference ~ '^IR-[0-9]{5,}$'",
        name="integration_reference_format",
    ),
    CheckConstraint(
        "delivery_status <> 'SUCCESS' "
        "OR (crm_record_id IS NOT NULL AND sent_at IS NOT NULL)",
        name="success_fields",
    ),
    CheckConstraint(
        "delivery_status <> 'EXISTING_CONTACT' OR crm_record_id IS NOT NULL",
        name="existing_contact_record_id",
    ),
    CheckConstraint(
        "delivery_status <> 'RETRY_PENDING' OR next_retry_at IS NOT NULL",
        name="retry_pending_due_at",
    ),
    CheckConstraint(
        "delivery_status <> 'UNKNOWN' OR next_reconciliation_at IS NOT NULL",
        name="unknown_reconciliation_due_at",
    ),
    CheckConstraint(
        "delivery_status NOT IN ('FAILED_ESCALATED', 'UNKNOWN_ESCALATED') "
        "OR failure_at IS NOT NULL",
        name="terminal_failure_at",
    ),
)

Index(
    "ix_normalized_leads_status_next_retry_at",
    normalized_leads.c.delivery_status,
    normalized_leads.c.next_retry_at,
)
Index(
    "ix_normalized_leads_status_next_reconciliation_at",
    normalized_leads.c.delivery_status,
    normalized_leads.c.next_reconciliation_at,
)
Index(
    "ix_normalized_leads_status_last_attempt_at",
    normalized_leads.c.delivery_status,
    normalized_leads.c.last_attempt_at,
)
Index(
    "ix_normalized_leads_status_escalation_notified_at",
    normalized_leads.c.delivery_status,
    normalized_leads.c.escalation_notified_at,
)
Index(
    "ix_normalized_leads_last_technical_failure_at",
    normalized_leads.c.last_technical_failure_at,
)

integration_runtime_state = Table(
    "integration_runtime_state",
    metadata,
    Column("id", SmallInteger, primary_key=True, autoincrement=False),
    Column(
        "crm_delivery_paused",
        Boolean,
        nullable=False,
        server_default=false(),
    ),
    Column("pause_reason", String(16)),
    Column("paused_at", DateTime(timezone=True)),
    Column("pause_alert_sent_at", DateTime(timezone=True)),
    Column("pause_alert_last_error", String(1000)),
    Column("last_crm_batch_attempt_at", DateTime(timezone=True)),
    Column("last_crm_batch_success_at", DateTime(timezone=True)),
    Column("last_crm_batch_error", String(1000)),
    Column("last_normalization_batch_attempt_at", DateTime(timezone=True)),
    Column("last_normalization_batch_success_at", DateTime(timezone=True)),
    Column("last_normalization_batch_error", String(1000)),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("id = 1", name="singleton_id"),
    CheckConstraint(
        "pause_reason IS NULL OR pause_reason IN ('AUTH', 'CONFIG')",
        name="pause_reason_allowed",
    ),
    CheckConstraint(
        "(crm_delivery_paused = false "
        "AND pause_reason IS NULL AND paused_at IS NULL) "
        "OR (crm_delivery_paused = true "
        "AND pause_reason IS NOT NULL AND paused_at IS NOT NULL)",
        name="pause_state_consistent",
    ),
)
