"""Durable state transitions for one CREATE_ONLY HubSpot delivery attempt."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from enum import StrEnum

from sqlalchemy import Engine, and_, case, or_, select, update
from sqlalchemy.engine import Connection

from project3_crm.config import Settings
from project3_crm.db.schema import integration_runtime_state, normalized_leads
from project3_crm.integrations.hubspot import (
    CreateContactResult,
    CreateResultCategory,
    HubSpotAdapter,
    HubSpotContactInput,
    HubSpotDiagnostics,
    ReadContactResult,
    ReadResultCategory,
)


class CrmDeliveryConsistencyError(RuntimeError):
    """Durable CRM state did not match the expected transition precondition."""


class CrmDeliveryAction(StrEnum):
    NO_WORK = "NO_WORK"
    CRM_BLOCKED = "CRM_BLOCKED"
    GLOBAL_PAUSE_ACTIVATED = "GLOBAL_PAUSE_ACTIVATED"
    SUCCESS = "SUCCESS"
    RETRY_PENDING = "RETRY_PENDING"
    UNKNOWN = "UNKNOWN"
    UNKNOWN_ESCALATED = "UNKNOWN_ESCALATED"
    EXISTING_CONTACT = "EXISTING_CONTACT"
    FAILED_ESCALATED = "FAILED_ESCALATED"


@dataclass(frozen=True)
class ClaimedCrmLead:
    lead_id: int
    contact: HubSpotContactInput


@dataclass(frozen=True)
class CrmDeliveryOutcome:
    action: CrmDeliveryAction
    lead_id: int | None = None
    adapter_category: CreateResultCategory | ReadResultCategory | None = None


class SendingGateStatus(StrEnum):
    CLEAR = "CLEAR"
    BLOCKED = "BLOCKED"
    RECOVERED_TO_UNKNOWN = "RECOVERED_TO_UNKNOWN"


@dataclass(frozen=True)
class SendingGateOutcome:
    status: SendingGateStatus
    lead_id: int | None = None


@dataclass(frozen=True)
class ReconciliationLead:
    lead_id: int
    integration_reference: str
    email: str | None


def _require_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("CRM delivery timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _runtime_state(connection: Connection) -> dict[str, object]:
    row = connection.execute(
        select(
            integration_runtime_state.c.crm_delivery_paused,
            integration_runtime_state.c.pause_reason,
            integration_runtime_state.c.paused_at,
        )
        .where(integration_runtime_state.c.id == 1)
        .with_for_update()
    ).mappings().one_or_none()
    if row is None:
        raise CrmDeliveryConsistencyError(
            "integration_runtime_state singleton id=1 is missing"
        )
    return dict(row)


def claim_one_crm_create(
    engine: Engine,
    *,
    worker_lease_seconds: int = 900,
    now: datetime | None = None,
) -> ClaimedCrmLead | None:
    """Claim and commit at most one eligible lead before any HubSpot I/O."""

    current_time = _require_aware_utc(now or datetime.now(UTC))
    claimed: ClaimedCrmLead | None = None

    with engine.begin() as connection:
        gate = _check_or_recover_sending_in_transaction(
            connection,
            worker_lease_seconds,
            current_time,
        )
        if gate.status is not SendingGateStatus.CLEAR:
            return None

        runtime = _runtime_state(connection)
        if runtime["crm_delivery_paused"] is True:
            return None

        due_unknown = connection.execute(
            select(normalized_leads.c.id)
            .where(
                normalized_leads.c.delivery_status == "UNKNOWN",
                normalized_leads.c.next_reconciliation_at <= current_time,
            )
            .limit(1)
        ).scalar_one_or_none()
        if due_unknown is not None:
            return None

        eligible = or_(
            normalized_leads.c.delivery_status == "PENDING",
            and_(
                normalized_leads.c.delivery_status == "RETRY_PENDING",
                normalized_leads.c.next_retry_at <= current_time,
            ),
        )
        due_or_created = case(
            (
                normalized_leads.c.delivery_status == "RETRY_PENDING",
                normalized_leads.c.next_retry_at,
            ),
            else_=normalized_leads.c.created_at,
        )
        candidate = connection.execute(
            select(
                normalized_leads.c.id,
                normalized_leads.c.delivery_status,
                normalized_leads.c.email,
                normalized_leads.c.first_name,
                normalized_leads.c.last_name,
                normalized_leads.c.phone,
                normalized_leads.c.company,
                normalized_leads.c.job_title,
                normalized_leads.c.city,
                normalized_leads.c.country,
                normalized_leads.c.integration_reference,
            )
            .where(eligible)
            .order_by(due_or_created.asc(), normalized_leads.c.id.asc())
            .limit(1)
            .with_for_update()
        ).mappings().one_or_none()
        if candidate is None:
            return None

        candidate_status = candidate["delivery_status"]
        update_eligibility = [
            normalized_leads.c.id == candidate["id"],
            normalized_leads.c.delivery_status == candidate_status,
        ]
        if candidate_status == "RETRY_PENDING":
            update_eligibility.append(
                normalized_leads.c.next_retry_at <= current_time
            )

        claimed_id = connection.execute(
            update(normalized_leads)
            .where(*update_eligibility)
            .values(
                delivery_status="SENDING",
                write_attempt_count=normalized_leads.c.write_attempt_count + 1,
                last_attempt_at=current_time,
                next_retry_at=None,
                updated_at=current_time,
            )
            .returning(normalized_leads.c.id)
        ).scalar_one_or_none()
        if claimed_id is None:
            raise CrmDeliveryConsistencyError("eligible CRM lead claim was lost")

        claimed = ClaimedCrmLead(
            lead_id=candidate["id"],
            contact=HubSpotContactInput(
                email=candidate["email"],
                first_name=candidate["first_name"],
                last_name=candidate["last_name"],
                phone=candidate["phone"],
                company=candidate["company"],
                job_title=candidate["job_title"],
                city=candidate["city"],
                country=candidate["country"],
                integration_reference=candidate["integration_reference"],
            ),
        )

    return claimed


def _retry_after_time(value: str | None, now: datetime) -> datetime | None:
    if value is None:
        return None
    candidate = value.strip()
    if not candidate:
        return None

    if candidate.isascii() and candidate.isdecimal():
        try:
            return now + timedelta(seconds=int(candidate))
        except (OverflowError, ValueError):
            return None

    try:
        parsed = parsedate_to_datetime(candidate)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _failure_values(
    row: dict[str, object],
    error_category: str,
    diagnostics: HubSpotDiagnostics,
    now: datetime,
    *,
    technical: bool,
) -> dict[str, object]:
    values: dict[str, object] = {
        "first_failure_at": row["first_failure_at"] or now,
        "last_failure_at": now,
        "last_error_category": error_category,
        "last_error_code": diagnostics.error_code,
        "last_error": diagnostics.as_persistence_text()[:1_000],
        "crm_correlation_id": diagnostics.correlation_id,
        "updated_at": now,
    }
    if technical:
        values["last_technical_failure_at"] = now
    return values


def _check_or_recover_sending_in_transaction(
    connection: Connection,
    worker_lease_seconds: int,
    now: datetime,
) -> SendingGateOutcome:
    sending = connection.execute(
        select(
            normalized_leads.c.id,
            normalized_leads.c.last_attempt_at,
            normalized_leads.c.first_failure_at,
        )
        .where(normalized_leads.c.delivery_status == "SENDING")
        .order_by(
            normalized_leads.c.last_attempt_at.asc(),
            normalized_leads.c.id.asc(),
        )
        .limit(1)
        .with_for_update()
    ).mappings().one_or_none()
    if sending is None:
        return SendingGateOutcome(status=SendingGateStatus.CLEAR)

    last_attempt_at = sending["last_attempt_at"]
    if not isinstance(last_attempt_at, datetime):
        return SendingGateOutcome(
            status=SendingGateStatus.BLOCKED,
            lead_id=sending["id"],
        )
    attempt_time = _require_aware_utc(last_attempt_at)
    lease_cutoff = now - timedelta(seconds=worker_lease_seconds)
    if attempt_time >= lease_cutoff:
        return SendingGateOutcome(
            status=SendingGateStatus.BLOCKED,
            lead_id=sending["id"],
        )

    diagnostics = HubSpotDiagnostics(
        description="stale SENDING lease expired; create outcome is uncertain"
    )
    values = _failure_values(
        dict(sending),
        "STALE_SENDING_UNKNOWN",
        diagnostics,
        now,
        technical=True,
    )
    recovered_id = connection.execute(
        update(normalized_leads)
        .where(
            normalized_leads.c.id == sending["id"],
            normalized_leads.c.delivery_status == "SENDING",
        )
        .values(
            **values,
            delivery_status="UNKNOWN",
            reconciliation_not_found_count=0,
            reconciliation_error_count=0,
            next_reconciliation_at=now,
            next_retry_at=None,
        )
        .returning(normalized_leads.c.id)
    ).scalar_one_or_none()
    if recovered_id is None:
        raise CrmDeliveryConsistencyError(
            "stale SENDING lead changed during recovery"
        )
    return SendingGateOutcome(
        status=SendingGateStatus.RECOVERED_TO_UNKNOWN,
        lead_id=recovered_id,
    )


def check_or_recover_sending(
    engine: Engine,
    worker_lease_seconds: int,
    *,
    now: datetime | None = None,
) -> SendingGateOutcome:
    """Block on fresh SENDING or recover one stale write to UNKNOWN."""

    if worker_lease_seconds <= 0:
        raise ValueError("worker_lease_seconds must be positive")
    current_time = _require_aware_utc(now or datetime.now(UTC))
    with engine.begin() as connection:
        return _check_or_recover_sending_in_transaction(
            connection,
            worker_lease_seconds,
            current_time,
        )


def _expected_sending_row(connection: Connection, lead_id: int) -> dict[str, object]:
    row = connection.execute(
        select(
            normalized_leads.c.id,
            normalized_leads.c.delivery_status,
            normalized_leads.c.automatic_retry_count,
            normalized_leads.c.first_failure_at,
        )
        .where(normalized_leads.c.id == lead_id)
        .with_for_update()
    ).mappings().one_or_none()
    if row is None or row["delivery_status"] != "SENDING":
        raise CrmDeliveryConsistencyError(
            "CRM lead is not in the expected SENDING state"
        )
    return dict(row)


def _update_expected_sending(
    connection: Connection,
    lead_id: int,
    values: dict[str, object],
) -> None:
    updated_id = connection.execute(
        update(normalized_leads)
        .where(
            normalized_leads.c.id == lead_id,
            normalized_leads.c.delivery_status == "SENDING",
        )
        .values(**values)
        .returning(normalized_leads.c.id)
    ).scalar_one_or_none()
    if updated_id is None:
        raise CrmDeliveryConsistencyError(
            "CRM lead changed while applying its create result"
        )


def _configured_retry_delay(settings: Settings, next_count: int) -> int:
    return {
        1: settings.crm_retry_delay_1_seconds,
        2: settings.crm_retry_delay_2_seconds,
        3: settings.crm_retry_delay_3_seconds,
    }[next_count]


def _diagnostics_with_description(
    evidence: HubSpotDiagnostics,
    description: str,
) -> HubSpotDiagnostics:
    """Retain only allowlisted evidence while replacing provider prose."""

    return HubSpotDiagnostics(
        description=description,
        http_status=evidence.http_status,
        category=evidence.category,
        error_code=evidence.error_code,
        correlation_id=evidence.correlation_id,
        retry_after=evidence.retry_after,
    )


class _DeferredDecisionKind(StrEnum):
    PAUSE = "PAUSE"
    EXISTING_CONTACT = "EXISTING_CONTACT"
    FAILED_ESCALATED = "FAILED_ESCALATED"


@dataclass(frozen=True)
class _DeferredDecision:
    kind: _DeferredDecisionKind
    error_category: str
    diagnostics: HubSpotDiagnostics
    pause_reason: str | None = None
    contact_id: str | None = None


def _pause_decision(
    pause_reason: str,
    error_category: str,
    evidence: HubSpotDiagnostics,
    description: str,
) -> _DeferredDecision:
    return _DeferredDecision(
        kind=_DeferredDecisionKind.PAUSE,
        pause_reason=pause_reason,
        error_category=error_category,
        diagnostics=_diagnostics_with_description(evidence, description),
    )


def _terminal_decision(
    error_category: str,
    evidence: HubSpotDiagnostics,
    description: str,
) -> _DeferredDecision:
    return _DeferredDecision(
        kind=_DeferredDecisionKind.FAILED_ESCALATED,
        error_category=error_category,
        diagnostics=_diagnostics_with_description(evidence, description),
    )


def _reference_found_matches(
    result: ReadContactResult,
    claimed: ClaimedCrmLead,
) -> bool:
    properties = result.properties or {}
    return (
        properties.get("integration_reference")
        == claimed.contact.integration_reference
        and properties.get("email") == claimed.contact.email
    )


def _decision_for_read_blocker(
    result: ReadContactResult,
    *,
    context: str,
) -> _DeferredDecision | None:
    if result.category is ReadResultCategory.AUTH_FAILURE:
        return _pause_decision(
            "AUTH",
            result.category.value,
            result.diagnostics,
            f"{context} authentication failed",
        )
    if result.category is ReadResultCategory.CONFIG_FAILURE:
        return _pause_decision(
            "CONFIG",
            result.category.value,
            result.diagnostics,
            f"{context} configuration failed",
        )
    if result.category in {
        ReadResultCategory.RETRYABLE_READ_FAILURE,
        ReadResultCategory.INDETERMINATE_READ,
    }:
        return _terminal_decision(
            "FOLLOW_UP_READ_INDETERMINATE",
            result.diagnostics,
            f"{context} was indeterminate",
        )
    return None


def _classify_bad_request(
    adapter: HubSpotAdapter,
    claimed: ClaimedCrmLead,
    create_result: CreateContactResult,
) -> _DeferredDecision:
    reference_result = adapter.get_contact_by_integration_reference(
        claimed.contact.integration_reference
    )
    blocker = _decision_for_read_blocker(
        reference_result,
        context="400 reference classification read",
    )
    if blocker is not None:
        return blocker

    if reference_result.category is ReadResultCategory.FOUND:
        matches = _reference_found_matches(reference_result, claimed)
        return _pause_decision(
            "CONFIG",
            "CRM_REFERENCE_COLLISION" if matches else "CRM_DATA_INTEGRITY",
            reference_result.diagnostics,
            (
                "active integration reference collision"
                if matches
                else "CRM data-integrity mismatch"
            ),
        )

    if reference_result.category is ReadResultCategory.ACTIVE_NOT_FOUND:
        return _terminal_decision(
            "UNCLASSIFIED_BAD_REQUEST",
            create_result.diagnostics,
            "unclassified 400 rejection",
        )

    raise CrmDeliveryConsistencyError(
        "unsupported active-read result during 400 classification"
    )


def _classify_conflict(
    adapter: HubSpotAdapter,
    claimed: ClaimedCrmLead,
    create_result: CreateContactResult,
) -> _DeferredDecision:
    reference_result = adapter.get_contact_by_integration_reference(
        claimed.contact.integration_reference
    )
    blocker = _decision_for_read_blocker(
        reference_result,
        context="409 reference classification read",
    )
    if blocker is not None:
        return blocker

    if reference_result.category is ReadResultCategory.FOUND:
        matches = _reference_found_matches(reference_result, claimed)
        return _pause_decision(
            "CONFIG",
            "CRM_REFERENCE_COLLISION" if matches else "CRM_DATA_INTEGRITY",
            reference_result.diagnostics,
            (
                "active integration reference collision"
                if matches
                else "CRM data-integrity mismatch"
            ),
        )

    if reference_result.category is not ReadResultCategory.ACTIVE_NOT_FOUND:
        raise CrmDeliveryConsistencyError(
            "unsupported reference-read result during 409 classification"
        )

    expected_email = claimed.contact.email
    if expected_email is None:
        return _terminal_decision(
            "UNCLASSIFIED_CONFLICT",
            create_result.diagnostics,
            "unclassified 409 conflict without normalized email",
        )

    email_result = adapter.get_contact_by_email(expected_email)
    blocker = _decision_for_read_blocker(
        email_result,
        context="409 email classification read",
    )
    if blocker is not None:
        return blocker

    if email_result.category is ReadResultCategory.FOUND:
        returned_email = (email_result.properties or {}).get("email")
        contact_id = email_result.contact_id
        if (
            isinstance(contact_id, str)
            and bool(contact_id.strip())
            and returned_email == expected_email
        ):
            return _DeferredDecision(
                kind=_DeferredDecisionKind.EXISTING_CONTACT,
                contact_id=contact_id.strip(),
                error_category="EXISTING_CONTACT",
                diagnostics=_diagnostics_with_description(
                    email_result.diagnostics,
                    "existing active Contact confirmed",
                ),
            )
        return _pause_decision(
            "CONFIG",
            "CRM_DATA_INTEGRITY",
            email_result.diagnostics,
            "CRM data-integrity mismatch",
        )

    if email_result.category is ReadResultCategory.ACTIVE_NOT_FOUND:
        return _terminal_decision(
            "UNCLASSIFIED_CONFLICT",
            create_result.diagnostics,
            "unclassified 409 conflict",
        )

    raise CrmDeliveryConsistencyError(
        "unsupported email-read result during 409 classification"
    )


def _activate_pause(
    connection: Connection,
    runtime: dict[str, object],
    pause_reason: str,
    now: datetime,
) -> None:
    if runtime["crm_delivery_paused"] is True:
        return

    pause_values: dict[str, object] = {
        "crm_delivery_paused": True,
        "pause_reason": pause_reason,
        "paused_at": now,
        "pause_alert_sent_at": None,
        "pause_alert_last_error": None,
        "updated_at": now,
    }
    runtime_id = connection.execute(
        update(integration_runtime_state)
        .where(integration_runtime_state.c.id == 1)
        .values(**pause_values)
        .returning(integration_runtime_state.c.id)
    ).scalar_one_or_none()
    if runtime_id is None:
        raise CrmDeliveryConsistencyError(
            "integration_runtime_state singleton changed unexpectedly"
        )


def _due_unknown_in_transaction(
    connection: Connection,
    now: datetime,
) -> ReconciliationLead | None:
    candidate = connection.execute(
        select(
            normalized_leads.c.id,
            normalized_leads.c.integration_reference,
            normalized_leads.c.email,
        )
        .where(
            normalized_leads.c.delivery_status == "UNKNOWN",
            normalized_leads.c.next_reconciliation_at <= now,
        )
        .order_by(
            normalized_leads.c.next_reconciliation_at.asc(),
            normalized_leads.c.id.asc(),
        )
        .limit(1)
    ).mappings().one_or_none()
    if candidate is None:
        return None
    return ReconciliationLead(
        lead_id=candidate["id"],
        integration_reference=candidate["integration_reference"],
        email=candidate["email"],
    )


def _prepare_unknown_reconciliation(
    engine: Engine,
    worker_lease_seconds: int,
    now: datetime,
) -> tuple[ReconciliationLead | None, SendingGateStatus, bool]:
    with engine.begin() as connection:
        gate = _check_or_recover_sending_in_transaction(
            connection,
            worker_lease_seconds,
            now,
        )
        if gate.status is SendingGateStatus.BLOCKED:
            return None, gate.status, False

        runtime = _runtime_state(connection)
        if runtime["crm_delivery_paused"] is True:
            return None, gate.status, True

        return _due_unknown_in_transaction(connection, now), gate.status, False


def select_one_due_unknown(
    engine: Engine,
    worker_lease_seconds: int,
    *,
    now: datetime | None = None,
) -> ReconciliationLead | None:
    """Select one due UNKNOWN after applying the CRM SENDING/pause gates."""

    if worker_lease_seconds <= 0:
        raise ValueError("worker_lease_seconds must be positive")
    current_time = _require_aware_utc(now or datetime.now(UTC))
    candidate, _, _ = _prepare_unknown_reconciliation(
        engine,
        worker_lease_seconds,
        current_time,
    )
    return candidate


def _expected_unknown_row(connection: Connection, lead_id: int) -> dict[str, object]:
    row = connection.execute(
        select(
            normalized_leads.c.id,
            normalized_leads.c.delivery_status,
            normalized_leads.c.integration_reference,
            normalized_leads.c.email,
            normalized_leads.c.automatic_retry_count,
            normalized_leads.c.write_attempt_count,
            normalized_leads.c.reconciliation_not_found_count,
            normalized_leads.c.reconciliation_error_count,
            normalized_leads.c.next_reconciliation_at,
            normalized_leads.c.first_failure_at,
        )
        .where(normalized_leads.c.id == lead_id)
        .with_for_update()
    ).mappings().one_or_none()
    if row is None or row["delivery_status"] != "UNKNOWN":
        raise CrmDeliveryConsistencyError(
            "CRM lead is not in the expected UNKNOWN state"
        )
    return dict(row)


def _update_expected_unknown(
    connection: Connection,
    lead_id: int,
    values: dict[str, object],
) -> None:
    updated_id = connection.execute(
        update(normalized_leads)
        .where(
            normalized_leads.c.id == lead_id,
            normalized_leads.c.delivery_status == "UNKNOWN",
        )
        .values(**values)
        .returning(normalized_leads.c.id)
    ).scalar_one_or_none()
    if updated_id is None:
        raise CrmDeliveryConsistencyError(
            "UNKNOWN lead changed while applying reconciliation evidence"
        )


def _reconciliation_found_matches(
    row: dict[str, object],
    result: ReadContactResult,
) -> bool:
    properties = result.properties or {}
    contact_id = result.contact_id
    return (
        isinstance(contact_id, str)
        and bool(contact_id.strip())
        and "integration_reference" in properties
        and "email" in properties
        and properties.get("integration_reference") == row["integration_reference"]
        and properties.get("email") == row["email"]
    )


def apply_reconciliation_result(
    engine: Engine,
    lead_id: int,
    result: ReadContactResult,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> CrmDeliveryOutcome:
    """Persist one completed active-reference read in a new transaction."""

    current_time = _require_aware_utc(now or datetime.now(UTC))
    with engine.begin() as connection:
        should_pause = result.category in {
            ReadResultCategory.FOUND,
            ReadResultCategory.AUTH_FAILURE,
            ReadResultCategory.CONFIG_FAILURE,
        }
        runtime: dict[str, object] | None = None
        if should_pause:
            runtime = _runtime_state(connection)
        row = _expected_unknown_row(connection, lead_id)

        if result.category is ReadResultCategory.FOUND:
            if _reconciliation_found_matches(row, result):
                assert result.contact_id is not None
                _update_expected_unknown(
                    connection,
                    lead_id,
                    {
                        "delivery_status": "SUCCESS",
                        "crm_record_id": result.contact_id.strip(),
                        "sent_at": current_time,
                        "reconciliation_not_found_count": 0,
                        "reconciliation_error_count": 0,
                        "next_reconciliation_at": None,
                        "next_retry_at": None,
                        "last_error_category": None,
                        "last_error_code": None,
                        "last_error": None,
                        "updated_at": current_time,
                    },
                )
                action = CrmDeliveryAction.SUCCESS
            else:
                if runtime is None:
                    raise CrmDeliveryConsistencyError(
                        "runtime state missing for data-integrity pause"
                    )
                _activate_pause(connection, runtime, "CONFIG", current_time)
                mismatch_diagnostics = _diagnostics_with_description(
                    result.diagnostics,
                    "CRM data-integrity mismatch during UNKNOWN reconciliation",
                )
                _update_expected_unknown(
                    connection,
                    lead_id,
                    {
                        **_failure_values(
                            row,
                            "CRM_DATA_INTEGRITY",
                            mismatch_diagnostics,
                            current_time,
                            technical=False,
                        ),
                        "delivery_status": "UNKNOWN",
                    },
                )
                action = CrmDeliveryAction.GLOBAL_PAUSE_ACTIVATED

        elif result.category is ReadResultCategory.ACTIVE_NOT_FOUND:
            new_count = int(row["reconciliation_not_found_count"]) + 1
            if new_count >= 3:
                not_found_diagnostics = _diagnostics_with_description(
                    result.diagnostics,
                    "third active-reference not-found result",
                )
                _update_expected_unknown(
                    connection,
                    lead_id,
                    {
                        **_failure_values(
                            row,
                            "UNKNOWN_NOT_FOUND_EXHAUSTED",
                            not_found_diagnostics,
                            current_time,
                            technical=True,
                        ),
                        "delivery_status": "UNKNOWN_ESCALATED",
                        "reconciliation_not_found_count": 3,
                        "reconciliation_error_count": 0,
                        "failure_at": current_time,
                        "next_reconciliation_at": None,
                        "next_retry_at": None,
                    },
                )
                action = CrmDeliveryAction.UNKNOWN_ESCALATED
            else:
                delay_seconds = (
                    settings.crm_reconcile_not_found_delay_2_seconds
                    if new_count == 1
                    else settings.crm_reconcile_not_found_delay_3_seconds
                )
                _update_expected_unknown(
                    connection,
                    lead_id,
                    {
                        "delivery_status": "UNKNOWN",
                        "reconciliation_not_found_count": new_count,
                        "reconciliation_error_count": 0,
                        "next_reconciliation_at": current_time
                        + timedelta(seconds=delay_seconds),
                        "updated_at": current_time,
                    },
                )
                action = CrmDeliveryAction.UNKNOWN

        elif result.category in {
            ReadResultCategory.RETRYABLE_READ_FAILURE,
            ReadResultCategory.INDETERMINATE_READ,
        }:
            new_error_count = int(row["reconciliation_error_count"]) + 1
            failure_values = _failure_values(
                row,
                result.category.value,
                _diagnostics_with_description(
                    result.diagnostics,
                    "UNKNOWN reconciliation read failed",
                ),
                current_time,
                technical=True,
            )
            if new_error_count >= 3:
                _update_expected_unknown(
                    connection,
                    lead_id,
                    {
                        **failure_values,
                        "delivery_status": "UNKNOWN_ESCALATED",
                        "reconciliation_error_count": 3,
                        "failure_at": current_time,
                        "next_reconciliation_at": None,
                        "next_retry_at": None,
                    },
                )
                action = CrmDeliveryAction.UNKNOWN_ESCALATED
            else:
                configured_delay = (
                    settings.crm_reconcile_error_delay_1_seconds
                    if new_error_count == 1
                    else settings.crm_reconcile_error_delay_2_seconds
                )
                configured_time = current_time + timedelta(seconds=configured_delay)
                provider_time = None
                if result.category is ReadResultCategory.RETRYABLE_READ_FAILURE:
                    provider_time = _retry_after_time(result.retry_after, current_time)
                next_reconciliation_at = max(
                    configured_time,
                    provider_time or configured_time,
                )
                _update_expected_unknown(
                    connection,
                    lead_id,
                    {
                        **failure_values,
                        "delivery_status": "UNKNOWN",
                        "reconciliation_error_count": new_error_count,
                        "next_reconciliation_at": next_reconciliation_at,
                    },
                )
                action = CrmDeliveryAction.UNKNOWN

        elif result.category in {
            ReadResultCategory.AUTH_FAILURE,
            ReadResultCategory.CONFIG_FAILURE,
        }:
            if runtime is None:
                raise CrmDeliveryConsistencyError(
                    "runtime state missing for reconciliation pause"
                )
            pause_reason = (
                "AUTH"
                if result.category is ReadResultCategory.AUTH_FAILURE
                else "CONFIG"
            )
            _activate_pause(connection, runtime, pause_reason, current_time)
            _update_expected_unknown(
                connection,
                lead_id,
                {
                    **_failure_values(
                        row,
                        result.category.value,
                        _diagnostics_with_description(
                            result.diagnostics,
                            "UNKNOWN reconciliation blocked by CRM configuration",
                        ),
                        current_time,
                        technical=False,
                    ),
                    "delivery_status": "UNKNOWN",
                },
            )
            action = CrmDeliveryAction.GLOBAL_PAUSE_ACTIVATED

        else:
            raise CrmDeliveryConsistencyError(
                "unsupported active-read result during UNKNOWN reconciliation"
            )

    return CrmDeliveryOutcome(
        action=action,
        lead_id=lead_id,
        adapter_category=result.category,
    )


def reconcile_one_unknown(
    engine: Engine,
    adapter: HubSpotAdapter,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> CrmDeliveryOutcome:
    """Read and reconcile at most one due UNKNOWN without issuing a create."""

    selection_time = _require_aware_utc(now or datetime.now(UTC))
    candidate, gate_status, paused = _prepare_unknown_reconciliation(
        engine,
        settings.worker_lease_seconds,
        selection_time,
    )
    if gate_status is SendingGateStatus.BLOCKED:
        return CrmDeliveryOutcome(action=CrmDeliveryAction.CRM_BLOCKED)
    if paused or candidate is None:
        return CrmDeliveryOutcome(action=CrmDeliveryAction.NO_WORK)

    result = adapter.get_contact_by_integration_reference(
        candidate.integration_reference
    )
    result_time = _require_aware_utc(now or datetime.now(UTC))
    return apply_reconciliation_result(
        engine,
        candidate.lead_id,
        result,
        settings,
        now=result_time,
    )


def _apply_deferred_decision(
    engine: Engine,
    lead_id: int,
    decision: _DeferredDecision,
    adapter_category: CreateResultCategory,
    *,
    now: datetime,
) -> CrmDeliveryOutcome:
    with engine.begin() as connection:
        runtime: dict[str, object] | None = None
        if decision.kind is _DeferredDecisionKind.PAUSE:
            runtime = _runtime_state(connection)
        row = _expected_sending_row(connection, lead_id)

        if decision.kind is _DeferredDecisionKind.PAUSE:
            if runtime is None or decision.pause_reason not in {"AUTH", "CONFIG"}:
                raise CrmDeliveryConsistencyError("invalid deferred pause decision")
            _activate_pause(connection, runtime, decision.pause_reason, now)
            _update_expected_sending(
                connection,
                lead_id,
                {
                    **_failure_values(
                        row,
                        decision.error_category,
                        decision.diagnostics,
                        now,
                        technical=False,
                    ),
                    "delivery_status": "PENDING",
                    "next_retry_at": None,
                },
            )
            action = CrmDeliveryAction.GLOBAL_PAUSE_ACTIVATED

        elif decision.kind is _DeferredDecisionKind.EXISTING_CONTACT:
            if decision.contact_id is None:
                raise CrmDeliveryConsistencyError(
                    "EXISTING_CONTACT decision is missing a Contact ID"
                )
            _update_expected_sending(
                connection,
                lead_id,
                {
                    "delivery_status": "EXISTING_CONTACT",
                    "crm_record_id": decision.contact_id,
                    "next_retry_at": None,
                    "next_reconciliation_at": None,
                    "last_error_category": None,
                    "last_error_code": None,
                    "last_error": None,
                    "updated_at": now,
                },
            )
            action = CrmDeliveryAction.EXISTING_CONTACT

        elif decision.kind is _DeferredDecisionKind.FAILED_ESCALATED:
            _update_expected_sending(
                connection,
                lead_id,
                {
                    **_failure_values(
                        row,
                        decision.error_category,
                        decision.diagnostics,
                        now,
                        technical=True,
                    ),
                    "delivery_status": "FAILED_ESCALATED",
                    "failure_at": now,
                    "next_retry_at": None,
                    "next_reconciliation_at": None,
                },
            )
            action = CrmDeliveryAction.FAILED_ESCALATED

        else:
            raise CrmDeliveryConsistencyError("unsupported deferred decision")

    return CrmDeliveryOutcome(
        action=action,
        lead_id=lead_id,
        adapter_category=adapter_category,
    )


def apply_create_result(
    engine: Engine,
    lead_id: int,
    result: CreateContactResult,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> CrmDeliveryOutcome:
    """Apply one adapter result in a new transaction after HubSpot I/O."""

    current_time = _require_aware_utc(now or datetime.now(UTC))

    with engine.begin() as connection:
        runtime: dict[str, object] | None = None
        if result.category in {
            CreateResultCategory.AUTH_FAILURE,
            CreateResultCategory.CONFIG_FAILURE,
        }:
            runtime = _runtime_state(connection)

        row = _expected_sending_row(connection, lead_id)

        if result.category in {
            CreateResultCategory.BAD_REQUEST_REQUIRES_SAFE_CLASSIFICATION,
            CreateResultCategory.CONFLICT_REQUIRES_LOOKUP,
        }:
            raise CrmDeliveryConsistencyError(
                "400/409 create results require the normal safe-classification path"
            )

        if result.category is CreateResultCategory.SUCCESS:
            contact_id = result.contact_id
            if not isinstance(contact_id, str) or not contact_id.strip():
                raise CrmDeliveryConsistencyError(
                    "SUCCESS adapter result is missing a valid Contact ID"
                )
            _update_expected_sending(
                connection,
                lead_id,
                {
                    "delivery_status": "SUCCESS",
                    "crm_record_id": contact_id.strip(),
                    "sent_at": current_time,
                    "next_retry_at": None,
                    "next_reconciliation_at": None,
                    "last_error_category": None,
                    "last_error_code": None,
                    "last_error": None,
                    "updated_at": current_time,
                },
            )
            action = CrmDeliveryAction.SUCCESS

        elif result.category in {
            CreateResultCategory.RETRYABLE_FAILURE,
            CreateResultCategory.RATE_LIMITED,
        }:
            retry_count = int(row["automatic_retry_count"])
            failure_values = _failure_values(
                row,
                result.category.value,
                result.diagnostics,
                current_time,
                technical=True,
            )
            if retry_count >= 3:
                _update_expected_sending(
                    connection,
                    lead_id,
                    {
                        **failure_values,
                        "delivery_status": "FAILED_ESCALATED",
                        "failure_at": current_time,
                        "next_retry_at": None,
                        "next_reconciliation_at": None,
                    },
                )
                action = CrmDeliveryAction.FAILED_ESCALATED
            else:
                next_count = retry_count + 1
                configured_time = current_time + timedelta(
                    seconds=_configured_retry_delay(settings, next_count)
                )
                provider_time = _retry_after_time(result.retry_after, current_time)
                next_retry_at = max(
                    configured_time,
                    provider_time or configured_time,
                )
                _update_expected_sending(
                    connection,
                    lead_id,
                    {
                        **failure_values,
                        "delivery_status": "RETRY_PENDING",
                        "automatic_retry_count": next_count,
                        "next_retry_at": next_retry_at,
                        "next_reconciliation_at": None,
                    },
                )
                action = CrmDeliveryAction.RETRY_PENDING

        elif result.category is CreateResultCategory.UNKNOWN:
            _update_expected_sending(
                connection,
                lead_id,
                {
                    **_failure_values(
                        row,
                        result.category.value,
                        result.diagnostics,
                        current_time,
                        technical=True,
                    ),
                    "delivery_status": "UNKNOWN",
                    "reconciliation_not_found_count": 0,
                    "reconciliation_error_count": 0,
                    "next_reconciliation_at": current_time
                    + timedelta(seconds=settings.crm_reconcile_initial_delay_seconds),
                    "next_retry_at": None,
                },
            )
            action = CrmDeliveryAction.UNKNOWN

        elif result.category in {
            CreateResultCategory.AUTH_FAILURE,
            CreateResultCategory.CONFIG_FAILURE,
        }:
            assert runtime is not None
            pause_reason = (
                "AUTH"
                if result.category is CreateResultCategory.AUTH_FAILURE
                else "CONFIG"
            )
            _activate_pause(connection, runtime, pause_reason, current_time)
            _update_expected_sending(
                connection,
                lead_id,
                {
                    **_failure_values(
                        row,
                        result.category.value,
                        result.diagnostics,
                        current_time,
                        technical=False,
                    ),
                    "delivery_status": "PENDING",
                    "next_retry_at": None,
                },
            )
            action = CrmDeliveryAction.GLOBAL_PAUSE_ACTIVATED

        elif result.category is CreateResultCategory.UNCLASSIFIED_PERMANENT_FAILURE:
            _update_expected_sending(
                connection,
                lead_id,
                {
                    **_failure_values(
                        row,
                        result.category.value,
                        result.diagnostics,
                        current_time,
                        technical=True,
                    ),
                    "delivery_status": "FAILED_ESCALATED",
                    "failure_at": current_time,
                    "next_retry_at": None,
                    "next_reconciliation_at": None,
                },
            )
            action = CrmDeliveryAction.FAILED_ESCALATED

        else:
            raise CrmDeliveryConsistencyError(
                f"unsupported create result category: {result.category.value}"
            )

    return CrmDeliveryOutcome(
        action=action,
        lead_id=lead_id,
        adapter_category=result.category,
    )


def deliver_one_crm_create(
    engine: Engine,
    adapter: HubSpotAdapter,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> CrmDeliveryOutcome:
    """Claim, commit, perform one create, then durably classify its result."""

    claim_time = _require_aware_utc(now or datetime.now(UTC))
    claimed = claim_one_crm_create(
        engine,
        worker_lease_seconds=settings.worker_lease_seconds,
        now=claim_time,
    )
    if claimed is None:
        return CrmDeliveryOutcome(action=CrmDeliveryAction.NO_WORK)

    result = adapter.create_contact(claimed.contact)
    if result.category is CreateResultCategory.BAD_REQUEST_REQUIRES_SAFE_CLASSIFICATION:
        decision = _classify_bad_request(adapter, claimed, result)
        result_time = _require_aware_utc(now or datetime.now(UTC))
        return _apply_deferred_decision(
            engine,
            claimed.lead_id,
            decision,
            result.category,
            now=result_time,
        )
    if result.category is CreateResultCategory.CONFLICT_REQUIRES_LOOKUP:
        decision = _classify_conflict(adapter, claimed, result)
        result_time = _require_aware_utc(now or datetime.now(UTC))
        return _apply_deferred_decision(
            engine,
            claimed.lead_id,
            decision,
            result.category,
            now=result_time,
        )
    result_time = _require_aware_utc(now or datetime.now(UTC))
    return apply_create_result(
        engine,
        claimed.lead_id,
        result,
        settings,
        now=result_time,
    )


def process_one_crm_work(
    engine: Engine,
    adapter: HubSpotAdapter,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> CrmDeliveryOutcome:
    """Process at most one CRM unit, prioritizing UNKNOWN reconciliation."""

    reconciliation = reconcile_one_unknown(
        engine,
        adapter,
        settings,
        now=now,
    )
    if reconciliation.action is not CrmDeliveryAction.NO_WORK:
        return reconciliation
    return deliver_one_crm_create(
        engine,
        adapter,
        settings,
        now=now,
    )
