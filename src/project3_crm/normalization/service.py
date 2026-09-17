"""One-event normalization orchestration and atomic PostgreSQL persistence."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import Engine, insert, select, update

from project3_crm.db.schema import (
    normalized_leads,
    normalized_leads_id_sequence,
    webhook_events,
)
from project3_crm.normalization.adapters import (
    NormalizationFailure,
    normalize_payload,
)
from project3_crm.normalization.models import NormalizedLeadData


@dataclass(frozen=True)
class StoredWebhookEvent:
    id: int
    source: str
    event_id: str
    raw_payload: dict[str, Any]


@dataclass(frozen=True)
class NormalizationOutcome:
    status: Literal["NORMALIZED", "NORMALIZATION_FAILED"]
    normalized_lead_id: int | None = None
    integration_reference: str | None = None
    diagnostic: str | None = None
    already_processed: bool = False


class NormalizationStateError(RuntimeError):
    """The event is not in a state that this operation may process."""


def integration_reference_for_id(normalized_lead_id: int) -> str:
    return f"IR-{normalized_lead_id:05d}"


def _insert_normalized_lead(
    connection: Any,
    event: StoredWebhookEvent,
    data: NormalizedLeadData,
    now: datetime,
) -> tuple[int, str]:
    normalized_lead_id = connection.execute(
        select(normalized_leads_id_sequence.next_value())
    ).scalar_one()
    integration_reference = integration_reference_for_id(normalized_lead_id)
    connection.execute(
        insert(normalized_leads).values(
            id=normalized_lead_id,
            webhook_event_id=event.id,
            integration_reference=integration_reference,
            source=data.source,
            source_event_id=data.source_event_id,
            submitted_at=data.submitted_at,
            first_name=data.first_name,
            last_name=data.last_name,
            email=data.email,
            phone=data.phone,
            company=data.company,
            job_title=data.job_title,
            country=data.country,
            city=data.city,
            campaign=data.campaign,
            lead_source=data.lead_source,
            source_metadata=data.source_metadata,
            delivery_status="PENDING",
            created_at=now,
            updated_at=now,
        )
    )
    connection.execute(
        update(webhook_events)
        .where(webhook_events.c.id == event.id)
        .values(
            event_status="NORMALIZED",
            normalized_at=now,
            completed_at=now,
            last_error_category=None,
            last_error=None,
            updated_at=now,
        )
    )
    return normalized_lead_id, integration_reference


def _mark_normalization_failed(
    connection: Any,
    event_id: int,
    failure: NormalizationFailure,
    now: datetime,
) -> None:
    connection.execute(
        update(webhook_events)
        .where(webhook_events.c.id == event_id)
        .values(
            event_status="NORMALIZATION_FAILED",
            completed_at=now,
            last_error_category=failure.category,
            last_error=failure.diagnostic[:1_000],
            updated_at=now,
        )
    )


def normalize_webhook_event(
    engine: Engine,
    event: StoredWebhookEvent,
) -> NormalizationOutcome:
    """Normalize one worker-claimed event and commit its final state atomically."""

    failure: NormalizationFailure | None = None
    data: NormalizedLeadData | None = None
    try:
        data = normalize_payload(event.source, event.event_id, event.raw_payload)
    except NormalizationFailure as error:
        failure = error

    now = datetime.now(UTC)
    with engine.begin() as connection:
        current = connection.execute(
            select(
                webhook_events.c.source,
                webhook_events.c.event_id,
                webhook_events.c.event_status,
                webhook_events.c.last_error,
            )
            .where(webhook_events.c.id == event.id)
            .with_for_update()
        ).mappings().one_or_none()
        if current is None:
            raise NormalizationStateError("Webhook event does not exist")
        if current["source"] != event.source or current["event_id"] != event.event_id:
            raise NormalizationStateError("Webhook event identity changed")

        existing = connection.execute(
            select(
                normalized_leads.c.id,
                normalized_leads.c.integration_reference,
            ).where(normalized_leads.c.webhook_event_id == event.id)
        ).mappings().one_or_none()
        if existing is not None:
            if current["event_status"] == "PROCESSING":
                connection.execute(
                    update(webhook_events)
                    .where(webhook_events.c.id == event.id)
                    .values(
                        event_status="NORMALIZED",
                        normalized_at=now,
                        completed_at=now,
                        last_error_category=None,
                        last_error=None,
                        updated_at=now,
                    )
                )
            elif current["event_status"] != "NORMALIZED":
                raise NormalizationStateError(
                    "Normalized row conflicts with webhook event status"
                )
            return NormalizationOutcome(
                status="NORMALIZED",
                normalized_lead_id=existing["id"],
                integration_reference=existing["integration_reference"],
                already_processed=True,
            )

        if current["event_status"] == "NORMALIZATION_FAILED":
            return NormalizationOutcome(
                status="NORMALIZATION_FAILED",
                diagnostic=current["last_error"],
                already_processed=True,
            )
        if current["event_status"] != "PROCESSING":
            raise NormalizationStateError(
                "Webhook event must be PROCESSING before normalization"
            )

        if failure is not None:
            _mark_normalization_failed(connection, event.id, failure, now)
            return NormalizationOutcome(
                status="NORMALIZATION_FAILED",
                diagnostic=failure.diagnostic,
            )

        if data is None:  # pragma: no cover - defensive invariant
            raise RuntimeError("Normalization produced neither data nor a failure")
        lead_id, integration_reference = _insert_normalized_lead(
            connection,
            event,
            data,
            now,
        )
        return NormalizationOutcome(
            status="NORMALIZED",
            normalized_lead_id=lead_id,
            integration_reference=integration_reference,
        )
