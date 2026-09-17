from __future__ import annotations

from datetime import UTC, datetime
from email.message import EmailMessage
import logging
import smtplib
import ssl
from typing import Any

import pytest

from project3_crm.config import Settings
from project3_crm.notifications.smtp import (
    ClusterTechnicalAlert,
    CrmTerminalFailure,
    NormalizationFailure,
    NotificationMessage,
    NotificationSendCategory,
    SmtpNotificationSender,
    build_cluster_technical_alert,
    build_crm_pause_alert,
    build_crm_terminal_failure_batch,
    build_normalization_failure_batch,
)


NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
SMTP_PASSWORD = "OBVIOUS-FAKE-SMTP-PASSWORD"
SMTP_USERNAME = "obvious-fake-smtp-user"


def notification_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": "postgresql://test:test@localhost/project3_test",
        "website_hmac_secret": "fake-website-secret",
        "linkedin_bearer_token": "fake-linkedin-token",
        "partner_api_key": "fake-partner-key",
        "hubspot_service_key": "fake-hubspot-key",
        "smtp_host": "smtp.example.test",
        "smtp_port": 587,
        "smtp_username": SMTP_USERNAME,
        "smtp_password": SMTP_PASSWORD,
        "smtp_from_email": "sender@example.test",
        "smtp_starttls": True,
        "ops_email_to": "ops@example.test",
        "automation_email_to": "automation@example.test",
    }
    values.update(overrides)
    return Settings(**values, _env_file=None)


class FakeSmtp:
    def __init__(
        self,
        events: list[Any],
        *,
        failure_stage: str | None = None,
        exception: Exception | None = None,
        refused: dict[str, tuple[int, bytes]] | None = None,
    ) -> None:
        self.events = events
        self.failure_stage = failure_stage
        self.exception = exception
        self.refused = refused or {}
        self.message: EmailMessage | None = None
        self.envelope: list[str] | None = None

    def _fail(self, stage: str) -> None:
        if self.failure_stage == stage:
            assert self.exception is not None
            raise self.exception

    def starttls(self, *, context: ssl.SSLContext) -> None:
        self.events.append(("starttls", context))
        self._fail("starttls")

    def login(self, user: str, password: str) -> None:
        self.events.append(("login", user, password))
        self._fail("login")

    def send_message(
        self,
        msg: EmailMessage,
        from_addr: str,
        to_addrs: list[str],
    ) -> dict[str, tuple[int, bytes]]:
        self.events.append(("send", from_addr, list(to_addrs)))
        self.message = msg
        self.envelope = list(to_addrs)
        self._fail("send")
        return self.refused

    def quit(self) -> None:
        self.events.append("quit")

    def close(self) -> None:
        self.events.append("close")


class FakeSmtpFactory:
    def __init__(
        self,
        *,
        failure_stage: str | None = None,
        exception: Exception | None = None,
        refused: dict[str, tuple[int, bytes]] | None = None,
    ) -> None:
        self.events: list[Any] = []
        self.failure_stage = failure_stage
        self.exception = exception
        self.connection = FakeSmtp(
            self.events,
            failure_stage=failure_stage,
            exception=exception,
            refused=refused,
        )

    def __call__(self, host: str, port: int, *, timeout: float) -> FakeSmtp:
        self.events.append(("connect", host, port, timeout))
        if self.failure_stage == "connect":
            assert self.exception is not None
            raise self.exception
        return self.connection


def message(*, cc: tuple[str, ...] = ()) -> NotificationMessage:
    return NotificationMessage(
        subject="Unicode operational notice — проверка",
        body="Plain UTF-8 body: готово",
        to=("to@example.test",),
        cc=cc,
    )


def test_smtp_success_uses_starttls_before_login_and_closes() -> None:
    factory = FakeSmtpFactory()
    context = ssl.create_default_context()
    sender = SmtpNotificationSender(
        notification_settings(),
        smtp_factory=factory,
        tls_context_factory=lambda: context,
    )

    result = sender.send(message(cc=("cc@example.test",)))

    assert result.category is NotificationSendCategory.SUCCESS
    assert factory.events == [
        ("connect", "smtp.example.test", 587, 30),
        ("starttls", context),
        ("login", SMTP_USERNAME, SMTP_PASSWORD),
        (
            "send",
            "sender@example.test",
            ["to@example.test", "cc@example.test"],
        ),
        "quit",
    ]
    sent = factory.connection.message
    assert sent is not None
    assert sent["From"] == "sender@example.test"
    assert sent["To"] == "to@example.test"
    assert sent["Cc"] == "cc@example.test"
    assert sent["Subject"] == "Unicode operational notice — проверка"
    assert "готово" in sent.get_content()


def test_custom_smtp_timeout_is_passed_to_connection_creation() -> None:
    factory = FakeSmtpFactory()
    sender = SmtpNotificationSender(
        notification_settings(smtp_timeout_seconds=12.5),
        smtp_factory=factory,
    )

    result = sender.send(message())

    assert result.category is NotificationSendCategory.SUCCESS
    assert factory.events[0] == ("connect", "smtp.example.test", 587, 12.5)


def test_default_tls_context_requires_certificate_verification() -> None:
    factory = FakeSmtpFactory()
    sender = SmtpNotificationSender(notification_settings(), smtp_factory=factory)

    result = sender.send(message())

    assert result.category is NotificationSendCategory.SUCCESS
    starttls_event = factory.events[1]
    assert starttls_event[0] == "starttls"
    context = starttls_event[1]
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_starttls_false_fails_closed_before_connection_or_authentication() -> None:
    factory = FakeSmtpFactory()
    sender = SmtpNotificationSender(
        notification_settings(smtp_starttls=False),
        smtp_factory=factory,
    )

    result = sender.send(message())

    assert result.category is NotificationSendCategory.CONFIGURATION_FAILURE
    assert result.description == "authenticated SMTP requires STARTTLS"
    assert factory.events == []


@pytest.mark.parametrize(
    ("to", "cc", "expected_envelope"),
    [
        (("to@example.test",), (), ["to@example.test"]),
        (
            ("to@example.test",),
            ("cc1@example.test", "cc2@example.test"),
            ["to@example.test", "cc1@example.test", "cc2@example.test"],
        ),
    ],
)
def test_envelope_includes_explicit_to_and_cc_recipients(
    to: tuple[str, ...],
    cc: tuple[str, ...],
    expected_envelope: list[str],
) -> None:
    factory = FakeSmtpFactory()
    sender = SmtpNotificationSender(notification_settings(), smtp_factory=factory)

    result = sender.send(NotificationMessage("subject", "body", to, cc))

    assert result.category is NotificationSendCategory.SUCCESS
    assert factory.connection.envelope == expected_envelope


def test_missing_recipients_is_rejected_before_smtp_connection() -> None:
    factory = FakeSmtpFactory()
    sender = SmtpNotificationSender(notification_settings(), smtp_factory=factory)

    result = sender.send(NotificationMessage("subject", "body", (), ()))

    assert result.category is NotificationSendCategory.CONFIGURATION_FAILURE
    assert factory.events == []


@pytest.mark.parametrize(
    ("stage", "exception", "expected_category", "expected_code"),
    [
        ("connect", ConnectionError("network details"), NotificationSendCategory.TEMPORARY_FAILURE, None),
        ("connect", TimeoutError("timeout details"), NotificationSendCategory.TEMPORARY_FAILURE, None),
        ("send", smtplib.SMTPServerDisconnected("server details"), NotificationSendCategory.TEMPORARY_FAILURE, None),
        ("send", smtplib.SMTPDataError(450, b"transient private response"), NotificationSendCategory.TEMPORARY_FAILURE, 450),
        ("login", smtplib.SMTPAuthenticationError(535, b"credential details"), NotificationSendCategory.CONFIGURATION_FAILURE, 535),
        ("send", smtplib.SMTPDataError(550, b"permanent private response"), NotificationSendCategory.PERMANENT_FAILURE, 550),
        ("send", smtplib.SMTPException(SMTP_PASSWORD), NotificationSendCategory.PERMANENT_FAILURE, None),
        ("send", RuntimeError(SMTP_PASSWORD), NotificationSendCategory.PERMANENT_FAILURE, None),
    ],
)
def test_smtp_failures_are_classified_without_leaking_exception_text(
    stage: str,
    exception: Exception,
    expected_category: NotificationSendCategory,
    expected_code: int | None,
) -> None:
    factory = FakeSmtpFactory(failure_stage=stage, exception=exception)
    sender = SmtpNotificationSender(notification_settings(), smtp_factory=factory)

    result = sender.send(message())

    assert result.category is expected_category
    assert result.smtp_status_code == expected_code
    assert len(result.description) <= 1_000
    assert SMTP_PASSWORD not in result.description
    if stage != "connect":
        assert factory.events[-1] == "quit"


@pytest.mark.parametrize(
    ("status_code", "expected_category"),
    [
        (450, NotificationSendCategory.TEMPORARY_FAILURE),
        (550, NotificationSendCategory.PERMANENT_FAILURE),
    ],
)
def test_refused_recipient_results_are_safe(
    status_code: int,
    expected_category: NotificationSendCategory,
) -> None:
    private_recipient = "private-person@example.test"
    factory = FakeSmtpFactory(
        refused={private_recipient: (status_code, b"private server response")}
    )
    sender = SmtpNotificationSender(notification_settings(), smtp_factory=factory)

    result = sender.send(message())

    assert result.category is expected_category
    assert result.smtp_status_code == status_code
    assert private_recipient not in result.description
    assert "private server response" not in result.description


def test_secret_values_never_enter_results_logs_or_operational_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    factory = FakeSmtpFactory(
        failure_stage="login",
        exception=smtplib.SMTPAuthenticationError(535, SMTP_PASSWORD.encode()),
    )
    settings = notification_settings()
    sender = SmtpNotificationSender(settings, smtp_factory=factory)
    operational = build_crm_pause_alert("AUTH", NOW, settings)

    with caplog.at_level(logging.DEBUG):
        result = sender.send(operational)

    assert SMTP_PASSWORD not in result.description
    assert SMTP_PASSWORD not in caplog.text
    assert SMTP_PASSWORD not in operational.body
    assert "fake-hubspot-key" not in operational.body
    assert SMTP_USERNAME not in operational.body


def test_crm_terminal_message_uses_ops_to_and_automation_cc() -> None:
    settings = notification_settings()
    notification = build_crm_terminal_failure_batch(
        [
            CrmTerminalFailure(
                integration_reference="IR-00042",
                source="website",
                source_event_id="WEB-42",
                normalized_lead_id=42,
                delivery_status="UNKNOWN_ESCALATED",
                write_attempt_count=1,
                first_failure_at=NOW,
                last_failure_at=NOW,
                error_category="UNKNOWN",
                error_code="SAFE_CODE",
                crm_correlation_id="corr-42",
            )
        ],
        settings,
    )

    assert notification.subject == "Project #3 CRM terminal failures"
    assert notification.to == ("ops@example.test",)
    assert notification.cc == ("automation@example.test",)
    for expected in (
        "IR-00042",
        "website",
        "WEB-42",
        "normalized_lead_id: 42",
        "UNKNOWN_ESCALATED",
        "write_attempt_count: 1",
        "SAFE_CODE",
        "corr-42",
    ):
        assert expected in notification.body


def test_normalization_message_uses_automation_only() -> None:
    notification = build_normalization_failure_batch(
        [
            NormalizationFailure(
                webhook_event_id=7,
                source="partner",
                source_event_id="PARTNER-7",
                received_at=NOW,
                completed_at=NOW,
                error_category="VALIDATION_ERROR",
                error="safe normalization diagnostic",
            )
        ],
        notification_settings(),
    )

    assert notification.subject == "Project #3 normalization failures"
    assert notification.to == ("automation@example.test",)
    assert notification.cc == ()
    assert "webhook_event_id: 7" in notification.body
    assert "PARTNER-7" in notification.body
    assert "safe normalization diagnostic" in notification.body


@pytest.mark.parametrize("reason", ["AUTH", "CONFIG"])
def test_pause_message_uses_automation_to_and_ops_cc(reason: str) -> None:
    notification = build_crm_pause_alert(reason, NOW, notification_settings())

    assert notification.subject.endswith(reason)
    assert notification.to == ("automation@example.test",)
    assert notification.cc == ("ops@example.test",)
    assert f"pause_reason: {reason}" in notification.body
    assert NOW.isoformat() in notification.body
    assert "before clearing the pause" in notification.body


def test_pause_message_rejects_unknown_reason() -> None:
    with pytest.raises(ValueError, match="AUTH or CONFIG"):
        build_crm_pause_alert("OTHER", NOW, notification_settings())


def test_cluster_message_contains_only_safe_summary_fields() -> None:
    notification = build_cluster_technical_alert(
        ClusterTechnicalAlert(
            affected_lead_count=5,
            alert_window_seconds=600,
            normalized_lead_ids=(1, 2, 3, 4, 5),
            integration_references=("IR-00001", "IR-00002"),
            technical_categories=("UNKNOWN", "RATE_LIMITED"),
        ),
        notification_settings(),
    )

    assert notification.subject == "Project #3 clustered CRM technical failures"
    assert notification.to == ("automation@example.test",)
    assert notification.cc == ("ops@example.test",)
    assert "affected_lead_count: 5" in notification.body
    assert "alert_window_seconds: 600" in notification.body
    assert "IR-00001, IR-00002" in notification.body
    assert "UNKNOWN, RATE_LIMITED" in notification.body


def test_helpers_cannot_accept_raw_payload_or_source_metadata() -> None:
    with pytest.raises(TypeError):
        CrmTerminalFailure(
            integration_reference="IR-00001",
            source="website",
            source_event_id="WEB-1",
            normalized_lead_id=1,
            delivery_status="FAILED_ESCALATED",
            write_attempt_count=1,
            raw_payload={"secret": "must-not-appear"},  # type: ignore[call-arg]
        )
    with pytest.raises(TypeError):
        NormalizationFailure(
            webhook_event_id=1,
            source="website",
            source_event_id="WEB-1",
            source_metadata={"secret": "must-not-appear"},  # type: ignore[call-arg]
        )
