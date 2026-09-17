"""One-attempt SMTP transport and safe operational message builders."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from enum import StrEnum
import smtplib
import ssl
from typing import Protocol

from project3_crm.config import Settings


_DIAGNOSTIC_LIMIT = 1_000


@dataclass(frozen=True)
class NotificationMessage:
    """Transport input containing only explicit message fields."""

    subject: str
    body: str
    to: tuple[str, ...]
    cc: tuple[str, ...] = ()


class NotificationSendCategory(StrEnum):
    SUCCESS = "SUCCESS"
    TEMPORARY_FAILURE = "TEMPORARY_FAILURE"
    PERMANENT_FAILURE = "PERMANENT_FAILURE"
    CONFIGURATION_FAILURE = "CONFIGURATION_FAILURE"


@dataclass(frozen=True)
class NotificationSendResult:
    category: NotificationSendCategory
    description: str
    smtp_status_code: int | None = None


@dataclass(frozen=True)
class CrmTerminalFailure:
    integration_reference: str
    source: str
    source_event_id: str
    normalized_lead_id: int
    delivery_status: str
    write_attempt_count: int
    first_failure_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_attempt_at: datetime | None = None
    error_category: str | None = None
    error_code: str | None = None
    error: str | None = None
    crm_correlation_id: str | None = None


@dataclass(frozen=True)
class NormalizationFailure:
    webhook_event_id: int
    source: str
    source_event_id: str
    received_at: datetime | None = None
    completed_at: datetime | None = None
    error_category: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class ClusterTechnicalAlert:
    affected_lead_count: int
    alert_window_seconds: int
    normalized_lead_ids: tuple[int, ...] = ()
    integration_references: tuple[str, ...] = ()
    technical_categories: tuple[str, ...] = ()


class _SmtpConnection(Protocol):
    def starttls(self, *, context: ssl.SSLContext) -> object: ...

    def login(self, user: str, password: str) -> object: ...

    def send_message(
        self,
        msg: EmailMessage,
        from_addr: str,
        to_addrs: Sequence[str],
    ) -> dict[str, tuple[int, bytes]]: ...

    def quit(self) -> object: ...

    def close(self) -> object: ...


class _SmtpFactory(Protocol):
    def __call__(
        self,
        host: str,
        port: int,
        *,
        timeout: float,
    ) -> _SmtpConnection: ...


def _safe_result(
    category: NotificationSendCategory,
    description: str,
    status_code: int | None = None,
) -> NotificationSendResult:
    return NotificationSendResult(
        category=category,
        description=description[:_DIAGNOSTIC_LIMIT],
        smtp_status_code=status_code,
    )


def _response_failure(status_code: int | None, description: str) -> NotificationSendResult:
    category = (
        NotificationSendCategory.TEMPORARY_FAILURE
        if status_code is not None and 400 <= status_code < 500
        else NotificationSendCategory.PERMANENT_FAILURE
    )
    return _safe_result(category, description, status_code)


def _refused_recipient_result(
    refused: dict[str, tuple[int, bytes]],
) -> NotificationSendResult:
    status_codes = [
        response[0]
        for response in refused.values()
        if isinstance(response, tuple)
        and response
        and isinstance(response[0], int)
    ]
    temporary_code = next(
        (code for code in status_codes if 400 <= code < 500),
        None,
    )
    if temporary_code is not None:
        return _safe_result(
            NotificationSendCategory.TEMPORARY_FAILURE,
            "one or more SMTP recipients were temporarily refused",
            temporary_code,
        )
    status_code = status_codes[0] if status_codes else None
    return _safe_result(
        NotificationSendCategory.PERMANENT_FAILURE,
        "one or more SMTP recipients were refused",
        status_code,
    )


def _email_message(
    notification: NotificationMessage,
    sender: str,
) -> tuple[EmailMessage, list[str]]:
    to = tuple(notification.to)
    cc = tuple(notification.cc)
    recipients = [*to, *cc]
    if not recipients or any(not value.strip() for value in recipients):
        raise ValueError("at least one non-blank SMTP recipient is required")

    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(to)
    if cc:
        message["Cc"] = ", ".join(cc)
    message["Subject"] = notification.subject
    message.set_content(notification.body, charset="utf-8")
    return message, recipients


class SmtpNotificationSender:
    """Send one notification attempt through verified authenticated SMTP."""

    def __init__(
        self,
        settings: Settings,
        *,
        smtp_factory: _SmtpFactory = smtplib.SMTP,
        tls_context_factory: Callable[[], ssl.SSLContext] = ssl.create_default_context,
    ) -> None:
        self._settings = settings
        self._smtp_factory = smtp_factory
        self._tls_context_factory = tls_context_factory

    def send(self, notification: NotificationMessage) -> NotificationSendResult:
        """Construct and attempt one email without retrying or sleeping."""

        if not self._settings.smtp_starttls:
            return _safe_result(
                NotificationSendCategory.CONFIGURATION_FAILURE,
                "authenticated SMTP requires STARTTLS",
            )

        try:
            message, recipients = _email_message(
                notification,
                self._settings.smtp_from_email,
            )
        except (TypeError, ValueError):
            return _safe_result(
                NotificationSendCategory.CONFIGURATION_FAILURE,
                "SMTP message configuration is invalid",
            )

        connection: _SmtpConnection | None = None
        try:
            connection = self._smtp_factory(
                self._settings.smtp_host,
                self._settings.smtp_port,
                timeout=self._settings.smtp_timeout_seconds,
            )
            context = self._tls_context_factory()
            connection.starttls(context=context)
            connection.login(
                self._settings.smtp_username.get_secret_value(),
                self._settings.smtp_password.get_secret_value(),
            )
            refused = connection.send_message(
                message,
                from_addr=self._settings.smtp_from_email,
                to_addrs=recipients,
            )
            if refused:
                return _refused_recipient_result(refused)
            return _safe_result(
                NotificationSendCategory.SUCCESS,
                "SMTP notification accepted",
            )
        except smtplib.SMTPAuthenticationError as exc:
            return _safe_result(
                NotificationSendCategory.CONFIGURATION_FAILURE,
                "SMTP authentication failed",
                exc.smtp_code,
            )
        except smtplib.SMTPNotSupportedError:
            return _safe_result(
                NotificationSendCategory.CONFIGURATION_FAILURE,
                "SMTP server does not support required secure authentication",
            )
        except smtplib.SMTPRecipientsRefused as exc:
            return _refused_recipient_result(exc.recipients)
        except smtplib.SMTPConnectError as exc:
            return _safe_result(
                NotificationSendCategory.TEMPORARY_FAILURE,
                "SMTP connection failed",
                exc.smtp_code,
            )
        except smtplib.SMTPSenderRefused as exc:
            return _response_failure(exc.smtp_code, "SMTP sender was refused")
        except smtplib.SMTPDataError as exc:
            return _response_failure(exc.smtp_code, "SMTP message was rejected")
        except smtplib.SMTPResponseException as exc:
            return _response_failure(exc.smtp_code, "SMTP request failed")
        except smtplib.SMTPServerDisconnected:
            return _safe_result(
                NotificationSendCategory.TEMPORARY_FAILURE,
                "SMTP server disconnected",
            )
        except ssl.SSLError:
            return _safe_result(
                NotificationSendCategory.CONFIGURATION_FAILURE,
                "SMTP TLS negotiation failed",
            )
        except smtplib.SMTPException:
            return _safe_result(
                NotificationSendCategory.PERMANENT_FAILURE,
                "SMTP operation failed",
            )
        except (TimeoutError, ConnectionError, OSError):
            return _safe_result(
                NotificationSendCategory.TEMPORARY_FAILURE,
                "SMTP network connection failed",
            )
        except Exception:
            return _safe_result(
                NotificationSendCategory.PERMANENT_FAILURE,
                "unexpected SMTP transport failure",
            )
        finally:
            if connection is not None:
                try:
                    connection.quit()
                except Exception:
                    try:
                        connection.close()
                    except Exception:
                        pass


def _display(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _record_lines(fields: Sequence[tuple[str, object | None]]) -> list[str]:
    return [f"{name}: {_display(value)}" for name, value in fields if value is not None]


def build_crm_terminal_failure_batch(
    failures: Sequence[CrmTerminalFailure],
    settings: Settings,
) -> NotificationMessage:
    lines = [f"Terminal CRM failures: {len(failures)}"]
    for index, failure in enumerate(failures, start=1):
        lines.extend(
            [
                "",
                f"Failure {index}",
                *_record_lines(
                    (
                        ("integration_reference", failure.integration_reference),
                        ("source", failure.source),
                        ("source_event_id", failure.source_event_id),
                        ("normalized_lead_id", failure.normalized_lead_id),
                        ("delivery_status", failure.delivery_status),
                        ("write_attempt_count", failure.write_attempt_count),
                        ("first_failure_at", failure.first_failure_at),
                        ("last_failure_at", failure.last_failure_at),
                        ("last_attempt_at", failure.last_attempt_at),
                        ("error_category", failure.error_category),
                        ("error_code", failure.error_code),
                        ("error", failure.error),
                        ("crm_correlation_id", failure.crm_correlation_id),
                    )
                ),
            ]
        )
    return NotificationMessage(
        subject="Project #3 CRM terminal failures",
        body="\n".join(lines),
        to=(settings.ops_email_to,),
        cc=(settings.automation_email_to,),
    )


def build_normalization_failure_batch(
    failures: Sequence[NormalizationFailure],
    settings: Settings,
) -> NotificationMessage:
    lines = [f"Normalization failures: {len(failures)}"]
    for index, failure in enumerate(failures, start=1):
        lines.extend(
            [
                "",
                f"Failure {index}",
                *_record_lines(
                    (
                        ("webhook_event_id", failure.webhook_event_id),
                        ("source", failure.source),
                        ("source_event_id", failure.source_event_id),
                        ("received_at", failure.received_at),
                        ("completed_at", failure.completed_at),
                        ("error_category", failure.error_category),
                        ("error", failure.error),
                    )
                ),
            ]
        )
    return NotificationMessage(
        subject="Project #3 normalization failures",
        body="\n".join(lines),
        to=(settings.automation_email_to,),
    )


def build_crm_pause_alert(
    pause_reason: str,
    paused_at: datetime,
    settings: Settings,
) -> NotificationMessage:
    if pause_reason not in {"AUTH", "CONFIG"}:
        raise ValueError("pause reason must be AUTH or CONFIG")
    body = "\n".join(
        (
            f"pause_reason: {pause_reason}",
            f"paused_at: {paused_at.isoformat()}",
            "Check CRM credentials/configuration and resolve the cause before clearing the pause.",
        )
    )
    return NotificationMessage(
        subject=f"Project #3 CRM delivery paused: {pause_reason}",
        body=body,
        to=(settings.automation_email_to,),
        cc=(settings.ops_email_to,),
    )


def build_cluster_technical_alert(
    alert: ClusterTechnicalAlert,
    settings: Settings,
) -> NotificationMessage:
    lines = _record_lines(
        (
            ("affected_lead_count", alert.affected_lead_count),
            ("alert_window_seconds", alert.alert_window_seconds),
            (
                "normalized_lead_ids",
                ", ".join(str(value) for value in alert.normalized_lead_ids),
            ),
            (
                "integration_references",
                ", ".join(alert.integration_references),
            ),
            (
                "technical_categories",
                ", ".join(alert.technical_categories),
            ),
        )
    )
    return NotificationMessage(
        subject="Project #3 clustered CRM technical failures",
        body="\n".join(lines),
        to=(settings.automation_email_to,),
        cc=(settings.ops_email_to,),
    )
