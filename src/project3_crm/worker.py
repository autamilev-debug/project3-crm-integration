"""Durable one-worker normalization lifecycle backed by PostgreSQL."""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import Engine, select, update

from project3_crm.config import Settings, get_settings
from project3_crm.crm_delivery import (
    CrmDeliveryAction,
    CrmDeliveryOutcome,
    process_one_crm_work,
)
from project3_crm.db.database import check_database_readiness, get_database_engine
from project3_crm.db.schema import (
    integration_runtime_state,
    normalized_leads,
    webhook_events,
)
from project3_crm.integrations.hubspot import HubSpotAdapter
from project3_crm.logging_config import configure_logging
from project3_crm.notifications.service import (
    AlertNotificationAction,
    AlertNotificationOutcome,
    BatchNotificationAction,
    BatchNotificationOutcome,
    NotificationSender,
    process_cluster_technical_alert,
    process_crm_failure_batch,
    process_crm_pause_alert,
    process_normalization_failure_batch,
)
from project3_crm.notifications.smtp import SmtpNotificationSender
from project3_crm.normalization.service import (
    StoredWebhookEvent,
    normalize_webhook_event,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StaleProcessingRecovery:
    webhook_event_id: int
    action: Literal["RESET_TO_RECEIVED", "REPAIRED_NORMALIZED"]


class WorkerStartupReadinessError(RuntimeError):
    """The worker cannot safely start against the configured database."""


def _require_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Worker timestamps must be timezone-aware")
    return value.astimezone(UTC)


def check_worker_startup_readiness(engine: Engine) -> None:
    """Verify PostgreSQL reachability and the migrated singleton runtime row."""

    try:
        check_database_readiness(engine)
        with engine.connect() as connection:
            runtime_id = connection.execute(
                select(integration_runtime_state.c.id).where(
                    integration_runtime_state.c.id == 1
                )
            ).scalar_one_or_none()
    except Exception:
        raise WorkerStartupReadinessError(
            "worker database readiness check failed"
        ) from None

    if runtime_id != 1:
        raise WorkerStartupReadinessError(
            "worker runtime singleton is not initialized"
        )


def recover_one_stale_processing(
    engine: Engine,
    worker_lease_seconds: int,
    *,
    now: datetime | None = None,
) -> StaleProcessingRecovery | None:
    """Transactionally repair at most one oldest stale PROCESSING event."""

    current_time = _require_aware_utc(now or datetime.now(UTC))
    lease_cutoff = current_time - timedelta(seconds=worker_lease_seconds)
    recovery: StaleProcessingRecovery | None = None

    with engine.begin() as connection:
        stale = connection.execute(
            select(
                webhook_events.c.id,
                webhook_events.c.processing_started_at,
            )
            .where(
                webhook_events.c.event_status == "PROCESSING",
                webhook_events.c.processing_started_at.is_not(None),
                webhook_events.c.processing_started_at < lease_cutoff,
            )
            .order_by(
                webhook_events.c.processing_started_at.asc(),
                webhook_events.c.id.asc(),
            )
            .limit(1)
            .with_for_update()
        ).mappings().one_or_none()
        if stale is None:
            return None

        normalized_exists = connection.execute(
            select(normalized_leads.c.id)
            .where(normalized_leads.c.webhook_event_id == stale["id"])
            .limit(1)
        ).scalar_one_or_none()

        if normalized_exists is None:
            connection.execute(
                update(webhook_events)
                .where(
                    webhook_events.c.id == stale["id"],
                    webhook_events.c.event_status == "PROCESSING",
                )
                .values(
                    event_status="RECEIVED",
                    processing_started_at=None,
                    completed_at=None,
                    updated_at=current_time,
                )
            )
            recovery = StaleProcessingRecovery(
                webhook_event_id=stale["id"],
                action="RESET_TO_RECEIVED",
            )
        else:
            connection.execute(
                update(webhook_events)
                .where(
                    webhook_events.c.id == stale["id"],
                    webhook_events.c.event_status == "PROCESSING",
                )
                .values(
                    event_status="NORMALIZED",
                    normalized_at=current_time,
                    completed_at=current_time,
                    last_error_category=None,
                    last_error=None,
                    updated_at=current_time,
                )
            )
            recovery = StaleProcessingRecovery(
                webhook_event_id=stale["id"],
                action="REPAIRED_NORMALIZED",
            )

    return recovery


def claim_one_received(
    engine: Engine,
    *,
    now: datetime | None = None,
) -> StoredWebhookEvent | None:
    """Claim and commit at most one oldest RECEIVED webhook event."""

    current_time = _require_aware_utc(now or datetime.now(UTC))
    claimed: StoredWebhookEvent | None = None

    with engine.begin() as connection:
        candidate = connection.execute(
            select(
                webhook_events.c.id,
                webhook_events.c.source,
                webhook_events.c.event_id,
                webhook_events.c.raw_payload,
            )
            .where(webhook_events.c.event_status == "RECEIVED")
            .order_by(
                webhook_events.c.received_at.asc(),
                webhook_events.c.id.asc(),
            )
            .limit(1)
            .with_for_update()
        ).mappings().one_or_none()
        if candidate is None:
            return None

        claimed_id = connection.execute(
            update(webhook_events)
            .where(
                webhook_events.c.id == candidate["id"],
                webhook_events.c.event_status == "RECEIVED",
            )
            .values(
                event_status="PROCESSING",
                processing_started_at=current_time,
                completed_at=None,
                updated_at=current_time,
            )
            .returning(webhook_events.c.id)
        ).scalar_one_or_none()
        if claimed_id is not None:
            claimed = StoredWebhookEvent(
                id=candidate["id"],
                source=candidate["source"],
                event_id=candidate["event_id"],
                raw_payload=dict(candidate["raw_payload"]),
            )

    return claimed


def run_worker_cycle(
    engine: Engine,
    adapter: HubSpotAdapter,
    settings: Settings,
    sender: NotificationSender,
    *,
    normalize: Callable[[Engine, StoredWebhookEvent], object] = normalize_webhook_event,
    crm_work: Callable[
        [Engine, HubSpotAdapter, Settings], CrmDeliveryOutcome
    ] = process_one_crm_work,
    pause_alert: Callable[..., AlertNotificationOutcome] = process_crm_pause_alert,
    now: datetime | None = None,
) -> bool:
    """Run one bounded normalization phase followed by one CRM-work phase."""

    current_time = _require_aware_utc(now or datetime.now(UTC))
    recovery = recover_one_stale_processing(
        engine,
        settings.worker_lease_seconds,
        now=current_time,
    )
    claimed = claim_one_received(engine, now=current_time)
    if claimed is not None:
        normalize(engine, claimed)
    crm_outcome = crm_work(engine, adapter, settings, now=current_time)
    if crm_outcome.action is CrmDeliveryAction.GLOBAL_PAUSE_ACTIVATED:
        try:
            immediate_outcome = pause_alert(
                engine,
                sender,
                settings,
                now=current_time,
            )
        except Exception:
            logger.error("Immediate CRM pause alert processing failed unexpectedly")
        else:
            _log_notification_outcome("CRM pause alert", immediate_outcome.action)
    crm_did_work = crm_outcome.action not in {
        CrmDeliveryAction.NO_WORK,
        CrmDeliveryAction.CRM_BLOCKED,
    }
    return recovery is not None or claimed is not None or crm_did_work


def _log_notification_outcome(
    label: str,
    action: AlertNotificationAction | BatchNotificationAction,
) -> None:
    if action in {AlertNotificationAction.SENT, BatchNotificationAction.SENT}:
        logger.info("%s sent", label)
    elif action in {
        AlertNotificationAction.SEND_FAILED,
        BatchNotificationAction.SEND_FAILED,
    }:
        logger.error("%s send failed", label)
    elif action is AlertNotificationAction.STALE_EPISODE:
        logger.warning("%s episode changed before state persistence", label)


def run_notification_check(
    engine: Engine,
    sender: NotificationSender,
    settings: Settings,
    *,
    pause_alert: Callable[..., AlertNotificationOutcome] = process_crm_pause_alert,
    cluster_alert: Callable[
        ..., AlertNotificationOutcome
    ] = process_cluster_technical_alert,
    crm_failure_batch: Callable[
        ..., BatchNotificationOutcome
    ] = process_crm_failure_batch,
    normalization_failure_batch: Callable[
        ..., BatchNotificationOutcome
    ] = process_normalization_failure_batch,
    now: datetime | None = None,
) -> bool:
    """Run each bounded notification operation once, isolating all four streams."""

    current_time = _require_aware_utc(now or datetime.now(UTC))
    useful_work = False
    operations = (
        ("CRM pause alert", pause_alert),
        ("CRM technical cluster alert", cluster_alert),
        ("CRM terminal failure batch", crm_failure_batch),
        ("Normalization failure batch", normalization_failure_batch),
    )
    idle_actions = {
        AlertNotificationAction.NO_ALERT,
        BatchNotificationAction.NOT_DUE,
    }

    for label, operation in operations:
        try:
            outcome = operation(engine, sender, settings, now=current_time)
            _log_notification_outcome(label, outcome.action)
            if outcome.action not in idle_actions:
                useful_work = True
        except Exception:
            logger.error("%s processing failed unexpectedly", label)
            continue

    return useful_work


def run_worker_loop(
    engine: Engine,
    adapter: HubSpotAdapter,
    settings: Settings,
    sender: NotificationSender,
    *,
    cycle: Callable[
        [Engine, HubSpotAdapter, Settings, NotificationSender], bool
    ] = run_worker_cycle,
    notification_check: Callable[
        [Engine, NotificationSender, Settings], bool
    ] = run_notification_check,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    utcnow: Callable[[], datetime] = lambda: datetime.now(UTC),
    should_stop: Callable[[], bool] = lambda: False,
) -> None:
    """Run normalization cycles until interrupted or the injected stop says to exit."""

    next_notification_check_monotonic = float("-inf")
    while True:
        try:
            if should_stop():
                return

            notification_work = False
            current_monotonic = monotonic()
            if current_monotonic >= next_notification_check_monotonic:
                try:
                    notification_work = notification_check(
                        engine,
                        sender,
                        settings,
                        now=_require_aware_utc(utcnow()),
                    )
                except Exception:
                    logger.error("Periodic notification check failed unexpectedly")
                finally:
                    next_notification_check_monotonic = (
                        current_monotonic
                        + settings.notification_failure_retry_seconds
                    )

            useful_work = cycle(engine, adapter, settings, sender)
            if not useful_work and not notification_work:
                sleep(settings.worker_poll_interval_seconds)
        except KeyboardInterrupt:
            logger.info("Worker shutdown requested")
            return
        except Exception:
            logger.error("Worker cycle failed; retrying after backoff")
            try:
                sleep(settings.worker_error_backoff_seconds)
            except KeyboardInterrupt:
                logger.info("Worker shutdown requested")
                return


def main() -> None:
    """Run the standalone worker process."""

    configure_logging()
    settings = get_settings()
    engine = get_database_engine()
    try:
        try:
            check_worker_startup_readiness(engine)
        except WorkerStartupReadinessError:
            logger.error("Worker startup readiness failed")
            raise

        logger.info("Worker started")
        adapter = HubSpotAdapter(settings)
        try:
            sender = SmtpNotificationSender(settings)
            run_worker_loop(engine, adapter, settings, sender)
        finally:
            adapter.close()
    finally:
        engine.dispose()
        logger.info("Worker stopped")


if __name__ == "__main__":
    main()
