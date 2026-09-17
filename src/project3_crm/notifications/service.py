"""Durable bounded processing for independent batch-notification streams."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from sqlalchemy import Engine, select, update
from sqlalchemy.engine import Connection

from project3_crm.config import Settings
from project3_crm.db.schema import (
    integration_runtime_state,
    normalized_leads,
    webhook_events,
)
from project3_crm.notifications.smtp import (
    ClusterTechnicalAlert,
    CrmTerminalFailure,
    NormalizationFailure,
    NotificationMessage,
    NotificationSendCategory,
    NotificationSendResult,
    build_cluster_technical_alert,
    build_crm_pause_alert,
    build_crm_terminal_failure_batch,
    build_normalization_failure_batch,
)


class NotificationStateError(RuntimeError):
    """Required durable notification state is missing."""


class BatchNotificationAction(StrEnum):
    NOT_DUE = "NOT_DUE"
    EMPTY = "EMPTY"
    SENT = "SENT"
    SEND_FAILED = "SEND_FAILED"


@dataclass(frozen=True)
class BatchNotificationOutcome:
    action: BatchNotificationAction
    item_count: int = 0
    send_category: NotificationSendCategory | None = None


class AlertNotificationAction(StrEnum):
    NO_ALERT = "NO_ALERT"
    SENT = "SENT"
    SEND_FAILED = "SEND_FAILED"
    STALE_EPISODE = "STALE_EPISODE"


@dataclass(frozen=True)
class AlertNotificationOutcome:
    action: AlertNotificationAction
    item_count: int = 0
    send_category: NotificationSendCategory | None = None
    safe_diagnostic: str | None = None


class NotificationSender(Protocol):
    def send(self, notification: NotificationMessage) -> NotificationSendResult: ...


def _require_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Notification timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _runtime_state(
    connection: Connection,
    attempt_column: object,
    success_column: object,
    error_column: object,
) -> dict[str, object]:
    runtime = connection.execute(
        select(attempt_column, success_column, error_column)
        .where(integration_runtime_state.c.id == 1)
        .with_for_update()
    ).mappings().one_or_none()
    if runtime is None:
        raise NotificationStateError("integration runtime singleton id=1 is missing")
    return dict(runtime)


def _stream_is_due(
    *,
    last_attempt_at: datetime | None,
    last_success_at: datetime | None,
    last_error: str | None,
    now: datetime,
    settings: Settings,
) -> bool:
    if last_attempt_at is None:
        return True
    incomplete_attempt = last_success_at is None or last_attempt_at > last_success_at
    if last_error is not None or incomplete_attempt:
        return now >= last_attempt_at + timedelta(
            seconds=settings.notification_failure_retry_seconds
        )
    return now >= last_success_at + timedelta(
        seconds=settings.notification_batch_interval_seconds
    )


def _safe_send_error(result: NotificationSendResult) -> str:
    parts = [result.category.value, result.description]
    if result.smtp_status_code is not None:
        parts.append(f"smtp_status={result.smtp_status_code}")
    return "; ".join(parts)[:1_000]


def _update_stream_runtime(
    connection: Connection,
    *,
    attempt_field: str,
    attempt_time: datetime,
    success_field: str | None = None,
    success_time: datetime | None = None,
    error_field: str | None = None,
    error: str | None = None,
) -> None:
    values: dict[str, object] = {
        attempt_field: attempt_time,
        "updated_at": attempt_time,
    }
    if success_field is not None:
        values[success_field] = success_time
    if error_field is not None:
        values[error_field] = error
    connection.execute(
        update(integration_runtime_state)
        .where(integration_runtime_state.c.id == 1)
        .values(**values)
    )


def process_crm_failure_batch(
    engine: Engine,
    sender: NotificationSender,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> BatchNotificationOutcome:
    """Process at most one due CRM terminal-failure email batch."""

    attempt_time = _require_aware_utc(now or datetime.now(UTC))
    failures: tuple[CrmTerminalFailure, ...]
    selected_ids: tuple[int, ...]

    with engine.begin() as connection:
        runtime = _runtime_state(
            connection,
            integration_runtime_state.c.last_crm_batch_attempt_at,
            integration_runtime_state.c.last_crm_batch_success_at,
            integration_runtime_state.c.last_crm_batch_error,
        )
        if not _stream_is_due(
            last_attempt_at=runtime["last_crm_batch_attempt_at"],
            last_success_at=runtime["last_crm_batch_success_at"],
            last_error=runtime["last_crm_batch_error"],
            now=attempt_time,
            settings=settings,
        ):
            return BatchNotificationOutcome(BatchNotificationAction.NOT_DUE)

        rows = connection.execute(
            select(
                normalized_leads.c.id,
                normalized_leads.c.integration_reference,
                normalized_leads.c.source,
                normalized_leads.c.source_event_id,
                normalized_leads.c.delivery_status,
                normalized_leads.c.write_attempt_count,
                normalized_leads.c.first_failure_at,
                normalized_leads.c.last_failure_at,
                normalized_leads.c.last_attempt_at,
                normalized_leads.c.last_error_category,
                normalized_leads.c.last_error_code,
                normalized_leads.c.last_error,
                normalized_leads.c.crm_correlation_id,
            )
            .where(
                normalized_leads.c.delivery_status.in_(
                    ("FAILED_ESCALATED", "UNKNOWN_ESCALATED")
                ),
                normalized_leads.c.escalation_notified_at.is_(None),
            )
            .order_by(normalized_leads.c.failure_at.asc(), normalized_leads.c.id.asc())
            .limit(settings.notification_batch_max_items)
        ).mappings().all()
        selected_ids = tuple(int(row["id"]) for row in rows)
        failures = tuple(
            CrmTerminalFailure(
                integration_reference=row["integration_reference"],
                source=row["source"],
                source_event_id=row["source_event_id"],
                normalized_lead_id=row["id"],
                delivery_status=row["delivery_status"],
                write_attempt_count=row["write_attempt_count"],
                first_failure_at=row["first_failure_at"],
                last_failure_at=row["last_failure_at"],
                last_attempt_at=row["last_attempt_at"],
                error_category=row["last_error_category"],
                error_code=row["last_error_code"],
                error=row["last_error"],
                crm_correlation_id=row["crm_correlation_id"],
            )
            for row in rows
        )
        if not failures:
            _update_stream_runtime(
                connection,
                attempt_field="last_crm_batch_attempt_at",
                attempt_time=attempt_time,
                success_field="last_crm_batch_success_at",
                success_time=attempt_time,
                error_field="last_crm_batch_error",
                error=None,
            )
            return BatchNotificationOutcome(BatchNotificationAction.EMPTY)
        _update_stream_runtime(
            connection,
            attempt_field="last_crm_batch_attempt_at",
            attempt_time=attempt_time,
        )

    result = sender.send(build_crm_terminal_failure_batch(failures, settings))

    with engine.begin() as connection:
        if result.category is NotificationSendCategory.SUCCESS:
            connection.execute(
                update(normalized_leads)
                .where(
                    normalized_leads.c.id.in_(selected_ids),
                    normalized_leads.c.delivery_status.in_(
                        ("FAILED_ESCALATED", "UNKNOWN_ESCALATED")
                    ),
                    normalized_leads.c.escalation_notified_at.is_(None),
                )
                .values(escalation_notified_at=attempt_time)
            )
            _update_stream_runtime(
                connection,
                attempt_field="last_crm_batch_attempt_at",
                attempt_time=attempt_time,
                success_field="last_crm_batch_success_at",
                success_time=attempt_time,
                error_field="last_crm_batch_error",
                error=None,
            )
            action = BatchNotificationAction.SENT
        else:
            _update_stream_runtime(
                connection,
                attempt_field="last_crm_batch_attempt_at",
                attempt_time=attempt_time,
                error_field="last_crm_batch_error",
                error=_safe_send_error(result),
            )
            action = BatchNotificationAction.SEND_FAILED

    return BatchNotificationOutcome(action, len(failures), result.category)


def process_normalization_failure_batch(
    engine: Engine,
    sender: NotificationSender,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> BatchNotificationOutcome:
    """Process at most one due normalization-failure email batch."""

    attempt_time = _require_aware_utc(now or datetime.now(UTC))
    failures: tuple[NormalizationFailure, ...]
    selected_ids: tuple[int, ...]

    with engine.begin() as connection:
        runtime = _runtime_state(
            connection,
            integration_runtime_state.c.last_normalization_batch_attempt_at,
            integration_runtime_state.c.last_normalization_batch_success_at,
            integration_runtime_state.c.last_normalization_batch_error,
        )
        if not _stream_is_due(
            last_attempt_at=runtime["last_normalization_batch_attempt_at"],
            last_success_at=runtime["last_normalization_batch_success_at"],
            last_error=runtime["last_normalization_batch_error"],
            now=attempt_time,
            settings=settings,
        ):
            return BatchNotificationOutcome(BatchNotificationAction.NOT_DUE)

        rows = connection.execute(
            select(
                webhook_events.c.id,
                webhook_events.c.source,
                webhook_events.c.event_id,
                webhook_events.c.received_at,
                webhook_events.c.completed_at,
                webhook_events.c.last_error_category,
                webhook_events.c.last_error,
            )
            .where(
                webhook_events.c.event_status == "NORMALIZATION_FAILED",
                webhook_events.c.escalation_notified_at.is_(None),
            )
            .order_by(webhook_events.c.completed_at.asc(), webhook_events.c.id.asc())
            .limit(settings.notification_batch_max_items)
        ).mappings().all()
        selected_ids = tuple(int(row["id"]) for row in rows)
        failures = tuple(
            NormalizationFailure(
                webhook_event_id=row["id"],
                source=row["source"],
                source_event_id=row["event_id"],
                received_at=row["received_at"],
                completed_at=row["completed_at"],
                error_category=row["last_error_category"],
                error=row["last_error"],
            )
            for row in rows
        )
        if not failures:
            _update_stream_runtime(
                connection,
                attempt_field="last_normalization_batch_attempt_at",
                attempt_time=attempt_time,
                success_field="last_normalization_batch_success_at",
                success_time=attempt_time,
                error_field="last_normalization_batch_error",
                error=None,
            )
            return BatchNotificationOutcome(BatchNotificationAction.EMPTY)
        _update_stream_runtime(
            connection,
            attempt_field="last_normalization_batch_attempt_at",
            attempt_time=attempt_time,
        )

    result = sender.send(build_normalization_failure_batch(failures, settings))

    with engine.begin() as connection:
        if result.category is NotificationSendCategory.SUCCESS:
            connection.execute(
                update(webhook_events)
                .where(
                    webhook_events.c.id.in_(selected_ids),
                    webhook_events.c.event_status == "NORMALIZATION_FAILED",
                    webhook_events.c.escalation_notified_at.is_(None),
                )
                .values(escalation_notified_at=attempt_time)
            )
            _update_stream_runtime(
                connection,
                attempt_field="last_normalization_batch_attempt_at",
                attempt_time=attempt_time,
                success_field="last_normalization_batch_success_at",
                success_time=attempt_time,
                error_field="last_normalization_batch_error",
                error=None,
            )
            action = BatchNotificationAction.SENT
        else:
            _update_stream_runtime(
                connection,
                attempt_field="last_normalization_batch_attempt_at",
                attempt_time=attempt_time,
                error_field="last_normalization_batch_error",
                error=_safe_send_error(result),
            )
            action = BatchNotificationAction.SEND_FAILED

    return BatchNotificationOutcome(action, len(failures), result.category)


def process_crm_pause_alert(
    engine: Engine,
    sender: NotificationSender,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> AlertNotificationOutcome:
    """Attempt one alert for the currently active unsent CRM pause episode."""

    result_time = _require_aware_utc(now or datetime.now(UTC))
    with engine.begin() as connection:
        runtime = connection.execute(
            select(
                integration_runtime_state.c.crm_delivery_paused,
                integration_runtime_state.c.pause_reason,
                integration_runtime_state.c.paused_at,
                integration_runtime_state.c.pause_alert_sent_at,
            )
            .where(integration_runtime_state.c.id == 1)
            .with_for_update()
        ).mappings().one_or_none()
        if runtime is None:
            raise NotificationStateError(
                "integration runtime singleton id=1 is missing"
            )
        if (
            not runtime["crm_delivery_paused"]
            or runtime["pause_alert_sent_at"] is not None
        ):
            return AlertNotificationOutcome(AlertNotificationAction.NO_ALERT)
        pause_reason = runtime["pause_reason"]
        paused_at = runtime["paused_at"]
        if pause_reason not in {"AUTH", "CONFIG"} or not isinstance(
            paused_at, datetime
        ):
            raise NotificationStateError("active CRM pause episode is inconsistent")

    send_result = sender.send(build_crm_pause_alert(pause_reason, paused_at, settings))

    with engine.begin() as connection:
        current = connection.execute(
            select(
                integration_runtime_state.c.crm_delivery_paused,
                integration_runtime_state.c.pause_reason,
                integration_runtime_state.c.paused_at,
                integration_runtime_state.c.pause_alert_sent_at,
            )
            .where(integration_runtime_state.c.id == 1)
            .with_for_update()
        ).mappings().one_or_none()
        if current is None:
            raise NotificationStateError(
                "integration runtime singleton id=1 is missing"
            )
        same_episode = (
            current["crm_delivery_paused"] is True
            and current["pause_reason"] == pause_reason
            and current["paused_at"] == paused_at
            and current["pause_alert_sent_at"] is None
        )
        if not same_episode:
            return AlertNotificationOutcome(
                AlertNotificationAction.STALE_EPISODE,
                send_category=send_result.category,
                safe_diagnostic="captured CRM pause episode is no longer current",
            )

        if send_result.category is NotificationSendCategory.SUCCESS:
            connection.execute(
                update(integration_runtime_state)
                .where(
                    integration_runtime_state.c.id == 1,
                    integration_runtime_state.c.crm_delivery_paused.is_(True),
                    integration_runtime_state.c.pause_reason == pause_reason,
                    integration_runtime_state.c.paused_at == paused_at,
                    integration_runtime_state.c.pause_alert_sent_at.is_(None),
                )
                .values(
                    pause_alert_sent_at=result_time,
                    pause_alert_last_error=None,
                    updated_at=result_time,
                )
            )
            action = AlertNotificationAction.SENT
            diagnostic = None
        else:
            diagnostic = _safe_send_error(send_result)
            connection.execute(
                update(integration_runtime_state)
                .where(
                    integration_runtime_state.c.id == 1,
                    integration_runtime_state.c.crm_delivery_paused.is_(True),
                    integration_runtime_state.c.pause_reason == pause_reason,
                    integration_runtime_state.c.paused_at == paused_at,
                    integration_runtime_state.c.pause_alert_sent_at.is_(None),
                )
                .values(
                    pause_alert_last_error=diagnostic,
                    updated_at=result_time,
                )
            )
            action = AlertNotificationAction.SEND_FAILED

    return AlertNotificationOutcome(
        action,
        item_count=1,
        send_category=send_result.category,
        safe_diagnostic=diagnostic,
    )


@dataclass(frozen=True)
class _ClusterEpisode:
    pending_at: datetime
    lead_ids: tuple[int, ...]
    alert: ClusterTechnicalAlert


def _cluster_alert_from_rows(
    rows: list[dict[str, object]],
    pending_at: datetime,
    settings: Settings,
) -> _ClusterEpisode:
    lead_ids = tuple(int(row["id"]) for row in rows)
    references = tuple(str(row["integration_reference"]) for row in rows)
    categories = tuple(
        dict.fromkeys(
            str(row["last_error_category"])
            for row in rows
            if row["last_error_category"] is not None
        )
    )
    return _ClusterEpisode(
        pending_at=pending_at,
        lead_ids=lead_ids,
        alert=ClusterTechnicalAlert(
            affected_lead_count=len(lead_ids),
            alert_window_seconds=settings.cluster_failure_window_seconds,
            normalized_lead_ids=lead_ids,
            integration_references=references,
            technical_categories=categories,
        ),
    )


def _prepare_cluster_episode(
    connection: Connection,
    settings: Settings,
    now: datetime,
) -> _ClusterEpisode | None:
    oldest_pending = connection.execute(
        select(normalized_leads.c.cluster_alert_pending_at)
        .where(normalized_leads.c.cluster_alert_pending_at.is_not(None))
        .order_by(
            normalized_leads.c.cluster_alert_pending_at.asc(),
            normalized_leads.c.id.asc(),
        )
        .limit(1)
        .with_for_update()
    ).mappings().one_or_none()
    if oldest_pending is not None:
        pending_at = oldest_pending["cluster_alert_pending_at"]
        if not isinstance(pending_at, datetime):
            raise NotificationStateError("pending cluster episode is inconsistent")
        pending_rows = connection.execute(
            select(
                normalized_leads.c.id,
                normalized_leads.c.integration_reference,
                normalized_leads.c.last_error_category,
            )
            .where(normalized_leads.c.cluster_alert_pending_at == pending_at)
            .order_by(normalized_leads.c.id.asc())
        ).mappings().all()
        if not pending_rows:
            return None
        return _cluster_alert_from_rows(pending_rows, pending_at, settings)

    cutoff = now - timedelta(seconds=settings.cluster_failure_window_seconds)
    qualifying_rows = connection.execute(
        select(
            normalized_leads.c.id,
            normalized_leads.c.integration_reference,
            normalized_leads.c.last_error_category,
            normalized_leads.c.last_technical_failure_at,
        )
        .where(
            normalized_leads.c.last_technical_failure_at >= cutoff,
            normalized_leads.c.cluster_alert_pending_at.is_(None),
            (
                normalized_leads.c.cluster_alerted_at.is_(None)
                | (
                    normalized_leads.c.last_technical_failure_at
                    > normalized_leads.c.cluster_alerted_at
                )
            ),
        )
        .order_by(
            normalized_leads.c.last_technical_failure_at.asc(),
            normalized_leads.c.id.asc(),
        )
    ).mappings().all()
    if len(qualifying_rows) < settings.cluster_failure_threshold:
        return None

    lead_ids = tuple(int(row["id"]) for row in qualifying_rows)
    connection.execute(
        update(normalized_leads)
        .where(
            normalized_leads.c.id.in_(lead_ids),
            normalized_leads.c.cluster_alert_pending_at.is_(None),
        )
        .values(cluster_alert_pending_at=now)
    )
    return _cluster_alert_from_rows(qualifying_rows, now, settings)


def process_cluster_technical_alert(
    engine: Engine,
    sender: NotificationSender,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> AlertNotificationOutcome:
    """Detect or retry at most one frozen clustered technical-failure alert."""

    current_time = _require_aware_utc(now or datetime.now(UTC))
    with engine.begin() as connection:
        episode = _prepare_cluster_episode(connection, settings, current_time)
        if episode is None:
            return AlertNotificationOutcome(AlertNotificationAction.NO_ALERT)

    send_result = sender.send(build_cluster_technical_alert(episode.alert, settings))

    with engine.begin() as connection:
        current_rows = connection.execute(
            select(normalized_leads.c.id)
            .where(
                normalized_leads.c.id.in_(episode.lead_ids),
                normalized_leads.c.cluster_alert_pending_at == episode.pending_at,
            )
            .order_by(normalized_leads.c.id.asc())
        ).mappings().all()
        current_ids = tuple(int(row["id"]) for row in current_rows)
        if current_ids != tuple(sorted(episode.lead_ids)):
            return AlertNotificationOutcome(
                AlertNotificationAction.STALE_EPISODE,
                item_count=len(current_ids),
                send_category=send_result.category,
                safe_diagnostic="captured cluster episode is no longer current",
            )

        if send_result.category is NotificationSendCategory.SUCCESS:
            connection.execute(
                update(normalized_leads)
                .where(
                    normalized_leads.c.id.in_(episode.lead_ids),
                    normalized_leads.c.cluster_alert_pending_at
                    == episode.pending_at,
                )
                .values(
                    cluster_alerted_at=episode.pending_at,
                    cluster_alert_pending_at=None,
                )
            )
            action = AlertNotificationAction.SENT
            diagnostic = None
        else:
            action = AlertNotificationAction.SEND_FAILED
            diagnostic = _safe_send_error(send_result)

    return AlertNotificationOutcome(
        action,
        item_count=len(episode.lead_ids),
        send_category=send_result.category,
        safe_diagnostic=diagnostic,
    )
