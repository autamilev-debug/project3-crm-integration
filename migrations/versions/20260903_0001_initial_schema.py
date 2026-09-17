"""Create the initial Project #3 PostgreSQL schema.

Revision ID: 20260903_0001
Revises:
Create Date: 2026-09-03
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260903_0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the three frozen tables and seed the singleton runtime row."""

    op.create_table(
        "webhook_events",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("raw_payload", postgresql.JSONB(), nullable=False),
        sa.Column("event_status", sa.String(length=32), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processing_started_at", sa.DateTime(timezone=True)),
        sa.Column("normalized_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_category", sa.String(length=64)),
        sa.Column("last_error", sa.String(length=1000)),
        sa.Column("escalation_notified_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source IN ('website', 'linkedin', 'partner')",
            name=op.f("ck_webhook_events_source_allowed"),
        ),
        sa.CheckConstraint(
            "event_status IN "
            "('RECEIVED', 'PROCESSING', 'NORMALIZED', 'NORMALIZATION_FAILED')",
            name=op.f("ck_webhook_events_event_status_allowed"),
        ),
        sa.CheckConstraint(
            "char_length(event_id) BETWEEN 1 AND 128",
            name=op.f("ck_webhook_events_event_id_length"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_webhook_events"),
        sa.UniqueConstraint(
            "source",
            "event_id",
            name="uq_webhook_events_source_event_id",
        ),
    )
    op.create_index(
        "ix_webhook_events_status_received_at",
        "webhook_events",
        ["event_status", "received_at"],
    )
    op.create_index(
        "ix_webhook_events_status_processing_started_at",
        "webhook_events",
        ["event_status", "processing_started_at"],
    )
    op.create_index(
        "ix_webhook_events_status_escalation_notified_at",
        "webhook_events",
        ["event_status", "escalation_notified_at"],
    )

    op.execute(sa.schema.CreateSequence(sa.Sequence("normalized_leads_id_seq")))
    op.create_table(
        "normalized_leads",
        sa.Column(
            "id",
            sa.BigInteger(),
            server_default=sa.text(
                "nextval('normalized_leads_id_seq'::regclass)"
            ),
            nullable=False,
        ),
        sa.Column("webhook_event_id", sa.BigInteger(), nullable=False),
        sa.Column("integration_reference", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("source_event_id", sa.String(length=128), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        sa.Column("first_name", sa.String(length=100)),
        sa.Column("last_name", sa.String(length=100)),
        sa.Column("email", sa.String(length=254)),
        sa.Column("phone", sa.String(length=64)),
        sa.Column("company", sa.String(length=200)),
        sa.Column("job_title", sa.String(length=150)),
        sa.Column("country", sa.String(length=100)),
        sa.Column("city", sa.String(length=100)),
        sa.Column("campaign", sa.String(length=200)),
        sa.Column("lead_source", sa.String(length=64)),
        sa.Column(
            "source_metadata",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "delivery_status",
            sa.String(length=32),
            nullable=False,
        ),
        sa.Column(
            "write_attempt_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "automatic_retry_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("next_retry_at", sa.DateTime(timezone=True)),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("first_failure_at", sa.DateTime(timezone=True)),
        sa.Column("last_failure_at", sa.DateTime(timezone=True)),
        sa.Column(
            "reconciliation_not_found_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "reconciliation_error_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("next_reconciliation_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_category", sa.String(length=64)),
        sa.Column("last_error_code", sa.String(length=128)),
        sa.Column("last_error", sa.String(length=1000)),
        sa.Column("crm_record_id", sa.String(length=64)),
        sa.Column("crm_correlation_id", sa.String(length=128)),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("failure_at", sa.DateTime(timezone=True)),
        sa.Column("escalation_notified_at", sa.DateTime(timezone=True)),
        sa.Column("last_technical_failure_at", sa.DateTime(timezone=True)),
        sa.Column("cluster_alert_pending_at", sa.DateTime(timezone=True)),
        sa.Column("cluster_alerted_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source IN ('website', 'linkedin', 'partner')",
            name=op.f("ck_normalized_leads_source_allowed"),
        ),
        sa.CheckConstraint(
            "delivery_status IN "
            "('PENDING', 'SENDING', 'RETRY_PENDING', 'UNKNOWN', 'SUCCESS', "
            "'EXISTING_CONTACT', 'FAILED_ESCALATED', 'UNKNOWN_ESCALATED')",
            name=op.f("ck_normalized_leads_delivery_status_allowed"),
        ),
        sa.CheckConstraint(
            "write_attempt_count >= 0",
            name=op.f("ck_normalized_leads_write_attempt_count_nonnegative"),
        ),
        sa.CheckConstraint(
            "automatic_retry_count BETWEEN 0 AND 3",
            name=op.f("ck_normalized_leads_automatic_retry_count_range"),
        ),
        sa.CheckConstraint(
            "reconciliation_not_found_count >= 0",
            name=op.f(
                "ck_normalized_leads_reconciliation_not_found_count_nonnegative"
            ),
        ),
        sa.CheckConstraint(
            "reconciliation_error_count >= 0",
            name=op.f(
                "ck_normalized_leads_reconciliation_error_count_nonnegative"
            ),
        ),
        sa.CheckConstraint(
            "integration_reference ~ '^IR-[0-9]{5,}$'",
            name=op.f("ck_normalized_leads_integration_reference_format"),
        ),
        sa.CheckConstraint(
            "delivery_status <> 'SUCCESS' "
            "OR (crm_record_id IS NOT NULL AND sent_at IS NOT NULL)",
            name=op.f("ck_normalized_leads_success_fields"),
        ),
        sa.CheckConstraint(
            "delivery_status <> 'EXISTING_CONTACT' OR crm_record_id IS NOT NULL",
            name=op.f("ck_normalized_leads_existing_contact_record_id"),
        ),
        sa.CheckConstraint(
            "delivery_status <> 'RETRY_PENDING' OR next_retry_at IS NOT NULL",
            name=op.f("ck_normalized_leads_retry_pending_due_at"),
        ),
        sa.CheckConstraint(
            "delivery_status <> 'UNKNOWN' OR next_reconciliation_at IS NOT NULL",
            name=op.f("ck_normalized_leads_unknown_reconciliation_due_at"),
        ),
        sa.CheckConstraint(
            "delivery_status NOT IN ('FAILED_ESCALATED', 'UNKNOWN_ESCALATED') "
            "OR failure_at IS NOT NULL",
            name=op.f("ck_normalized_leads_terminal_failure_at"),
        ),
        sa.ForeignKeyConstraint(
            ["webhook_event_id"],
            ["webhook_events.id"],
            name="fk_normalized_leads_webhook_event_id_webhook_events",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_normalized_leads"),
        sa.UniqueConstraint(
            "integration_reference",
            name="uq_normalized_leads_integration_reference",
        ),
        sa.UniqueConstraint(
            "webhook_event_id",
            name="uq_normalized_leads_webhook_event_id",
        ),
    )
    op.create_index(
        "ix_normalized_leads_status_next_retry_at",
        "normalized_leads",
        ["delivery_status", "next_retry_at"],
    )
    op.create_index(
        "ix_normalized_leads_status_next_reconciliation_at",
        "normalized_leads",
        ["delivery_status", "next_reconciliation_at"],
    )
    op.create_index(
        "ix_normalized_leads_status_last_attempt_at",
        "normalized_leads",
        ["delivery_status", "last_attempt_at"],
    )
    op.create_index(
        "ix_normalized_leads_status_escalation_notified_at",
        "normalized_leads",
        ["delivery_status", "escalation_notified_at"],
    )
    op.create_index(
        "ix_normalized_leads_last_technical_failure_at",
        "normalized_leads",
        ["last_technical_failure_at"],
    )

    op.create_table(
        "integration_runtime_state",
        sa.Column("id", sa.SmallInteger(), autoincrement=False, nullable=False),
        sa.Column(
            "crm_delivery_paused",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column("pause_reason", sa.String(length=16)),
        sa.Column("paused_at", sa.DateTime(timezone=True)),
        sa.Column("pause_alert_sent_at", sa.DateTime(timezone=True)),
        sa.Column("pause_alert_last_error", sa.String(length=1000)),
        sa.Column("last_crm_batch_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("last_crm_batch_success_at", sa.DateTime(timezone=True)),
        sa.Column("last_crm_batch_error", sa.String(length=1000)),
        sa.Column(
            "last_normalization_batch_attempt_at",
            sa.DateTime(timezone=True),
        ),
        sa.Column(
            "last_normalization_batch_success_at",
            sa.DateTime(timezone=True),
        ),
        sa.Column("last_normalization_batch_error", sa.String(length=1000)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "id = 1",
            name=op.f("ck_integration_runtime_state_singleton_id"),
        ),
        sa.CheckConstraint(
            "pause_reason IS NULL OR pause_reason IN ('AUTH', 'CONFIG')",
            name=op.f("ck_integration_runtime_state_pause_reason_allowed"),
        ),
        sa.CheckConstraint(
            "(crm_delivery_paused = false "
            "AND pause_reason IS NULL AND paused_at IS NULL) "
            "OR (crm_delivery_paused = true "
            "AND pause_reason IS NOT NULL AND paused_at IS NOT NULL)",
            name=op.f("ck_integration_runtime_state_pause_state_consistent"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_integration_runtime_state"),
    )
    op.execute(
        sa.text(
            "INSERT INTO integration_runtime_state "
            "(id, crm_delivery_paused, updated_at) "
            "VALUES (1, false, CURRENT_TIMESTAMP) "
            "ON CONFLICT (id) DO NOTHING"
        )
    )


def downgrade() -> None:
    """Remove the initial Project #3 schema."""

    op.drop_table("integration_runtime_state")
    op.drop_index(
        "ix_normalized_leads_last_technical_failure_at",
        table_name="normalized_leads",
    )
    op.drop_index(
        "ix_normalized_leads_status_escalation_notified_at",
        table_name="normalized_leads",
    )
    op.drop_index(
        "ix_normalized_leads_status_last_attempt_at",
        table_name="normalized_leads",
    )
    op.drop_index(
        "ix_normalized_leads_status_next_reconciliation_at",
        table_name="normalized_leads",
    )
    op.drop_index(
        "ix_normalized_leads_status_next_retry_at",
        table_name="normalized_leads",
    )
    op.drop_table("normalized_leads")
    op.execute(sa.schema.DropSequence(sa.Sequence("normalized_leads_id_seq")))
    op.drop_index(
        "ix_webhook_events_status_escalation_notified_at",
        table_name="webhook_events",
    )
    op.drop_index(
        "ix_webhook_events_status_processing_started_at",
        table_name="webhook_events",
    )
    op.drop_index(
        "ix_webhook_events_status_received_at",
        table_name="webhook_events",
    )
    op.drop_table("webhook_events")
