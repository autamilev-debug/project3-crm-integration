"""Minimal PostgreSQL persistence for authenticated raw webhook events."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection
from sqlalchemy.dialects.postgresql import insert

from project3_crm.db.schema import webhook_events


def persist_webhook_event(
    connection: Connection,
    *,
    source: str,
    event_id: str,
    raw_payload: dict[str, Any],
) -> bool:
    """Insert one raw event and return whether this delivery created the row."""

    now = datetime.now(UTC)
    statement = (
        insert(webhook_events)
        .values(
            source=source,
            event_id=event_id,
            raw_payload=raw_payload,
            event_status="RECEIVED",
            received_at=now,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(
            constraint="uq_webhook_events_source_event_id",
        )
        .returning(webhook_events.c.id)
    )

    return connection.execute(statement).scalar_one_or_none() is not None