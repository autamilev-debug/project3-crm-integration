import asyncio
import hashlib
import hmac
import json
import logging
import time
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError

import project3_crm.webhooks as webhook_module
from project3_crm.config import Settings
from project3_crm.web import app


TEST_DATABASE_URL = (
    "postgresql://project3:test-database-password@localhost:5432/project3_test"
)
TEST_WEBSITE_SECRET = "test-website-hmac-secret"
TEST_LINKEDIN_TOKEN = "test-linkedin-bearer-token"
TEST_PARTNER_KEY = "test-partner-api-key"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url=TEST_DATABASE_URL,
        website_hmac_secret=TEST_WEBSITE_SECRET,
        linkedin_bearer_token=TEST_LINKEDIN_TOKEN,
        partner_api_key=TEST_PARTNER_KEY,
        hubspot_service_key="test-unused-hubspot-key",
        smtp_host="smtp.example.test",
        smtp_username="test-unused-smtp-user",
        smtp_password="test-unused-smtp-password",
        smtp_from_email="automation@example.test",
        ops_email_to="ops@example.test",
        automation_email_to="automation@example.test",
        _env_file=None,
    )


@pytest.fixture
def payloads() -> dict[str, dict[str, Any]]:
    return {
        "website": {
            "submission_id": "site-event-001",
            "email": "website.lead@example.test",
            "custom": {"keep": True},
        },
        "linkedin": {
            "event": {"id": "linkedin-event-001"},
            "lead": {"email": "linkedin.lead@example.test"},
        },
        "partner": {
            "reference": "Partner-Event-001",
            "contact": {"email": "partner.lead@example.test"},
        },
    }


class FakeResult:
    def __init__(self, value: int | None) -> None:
        self.value = value

    def scalar_one_or_none(self) -> int | None:
        return self.value


@dataclass
class FakePostgreSQLEngine:
    rows: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    statements: list[Any] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    fail_execute: bool = False
    fail_commit: bool = False
    next_id: int = 1

    def begin(self) -> "FakeTransaction":
        return FakeTransaction(self)


class FakeTransaction:
    def __init__(self, engine: FakePostgreSQLEngine) -> None:
        self.engine = engine
        self.original_rows: dict[tuple[str, str], dict[str, Any]] = {}

    def __enter__(self) -> "FakeConnection":
        self.original_rows = deepcopy(self.engine.rows)
        self.engine.events.append("begin")
        return FakeConnection(self.engine)

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        if exc_type is not None:
            self.engine.rows = self.original_rows
            self.engine.events.append("rollback")
            return False
        if self.engine.fail_commit:
            self.engine.rows = self.original_rows
            self.engine.events.append("commit_failed")
            raise SQLAlchemyError("simulated database commit failure")
        self.engine.events.append("commit")
        return False


class FakeConnection:
    def __init__(self, engine: FakePostgreSQLEngine) -> None:
        self.engine = engine

    def execute(self, statement: Any) -> FakeResult:
        self.engine.events.append("execute")
        self.engine.statements.append(statement)
        if self.engine.fail_execute:
            raise SQLAlchemyError("simulated database execute failure")

        parameters = statement.compile(dialect=postgresql.dialect()).params
        key = (parameters["source"], parameters["event_id"])
        if key in self.engine.rows:
            return FakeResult(None)

        row_id = self.engine.next_id
        self.engine.next_id += 1
        self.engine.rows[key] = deepcopy(parameters)
        self.engine.rows[key]["id"] = row_id
        return FakeResult(row_id)


@pytest.fixture
def fake_engine(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
) -> FakePostgreSQLEngine:
    engine = FakePostgreSQLEngine()
    monkeypatch.setattr(webhook_module, "get_settings", lambda: settings)
    monkeypatch.setattr(webhook_module, "get_database_engine", lambda: engine)
    return engine


def request(
    method: str,
    path: str,
    *,
    content: bytes | None = None,
    headers: dict[str, str] | list[tuple[bytes, bytes]] | None = None,
) -> httpx.Response:
    async def send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.request(
                method,
                path,
                content=content,
                headers=headers,
            )

    return asyncio.run(send())


def json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def website_headers(
    raw_body: bytes,
    *,
    timestamp: str | None = None,
    secret: str = TEST_WEBSITE_SECRET,
) -> dict[str, str]:
    timestamp = timestamp or str(int(time.time()))
    signature = hmac.new(
        secret.encode("utf-8"),
        timestamp.encode("ascii") + b"." + raw_body,
        hashlib.sha256,
    ).hexdigest()
    return {
        "X-Webhook-Timestamp": timestamp,
        "X-Webhook-Signature": f"sha256={signature}",
    }


def valid_headers(source: str, raw_body: bytes) -> dict[str, str]:
    if source == "website":
        return website_headers(raw_body)
    if source == "linkedin":
        return {"Authorization": f"Bearer {TEST_LINKEDIN_TOKEN}"}
    return {"X-API-Key": TEST_PARTNER_KEY}


@pytest.mark.parametrize("source", ["website", "linkedin", "partner"])
def test_each_source_accepts_valid_authentication_and_payload(
    source: str,
    payloads: dict[str, dict[str, Any]],
    fake_engine: FakePostgreSQLEngine,
) -> None:
    raw_body = json_bytes(payloads[source])

    response = request(
        "POST",
        f"/webhooks/{source}",
        content=raw_body,
        headers=valid_headers(source, raw_body),
    )

    assert response.status_code == 202
    assert response.json() == {"status": "accepted", "duplicate": False}
    assert fake_engine.events == ["begin", "execute", "commit"]


@pytest.mark.parametrize(
    ("source", "headers"),
    [
        ("website", {}),
        (
            "website",
            {
                "X-Webhook-Timestamp": "not-a-timestamp",
                "X-Webhook-Signature": "SHA256=" + ("0" * 64),
            },
        ),
        (
            "website",
            {
                "X-Webhook-Timestamp": "9" * 5_000,
                "X-Webhook-Signature": "sha256=" + ("0" * 64),
            },
        ),
        (
            "website",
            {
                "X-Webhook-Timestamp": str(int(time.time())),
                "X-Webhook-Signature": "sha256=" + ("0" * 64),
            },
        ),
        ("linkedin", {}),
        ("linkedin", {"Authorization": "Bearer wrong-token"}),
        ("linkedin", {"Authorization": TEST_LINKEDIN_TOKEN}),
        ("partner", {}),
        ("partner", {"X-API-Key": "wrong-key"}),
    ],
)
def test_missing_invalid_or_malformed_authentication_is_rejected_before_database(
    source: str,
    headers: dict[str, str],
    payloads: dict[str, dict[str, Any]],
    fake_engine: FakePostgreSQLEngine,
) -> None:
    response = request(
        "POST",
        f"/webhooks/{source}",
        content=json_bytes(payloads[source]),
        headers=headers,
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Authentication failed"}
    assert fake_engine.events == []


@pytest.mark.parametrize(
    ("source", "header_name", "header_prefix", "configured_secret"),
    [
        (
            "linkedin",
            b"authorization",
            b"Bearer ",
            TEST_LINKEDIN_TOKEN,
        ),
        (
            "partner",
            b"x-api-key",
            b"",
            TEST_PARTNER_KEY,
        ),
    ],
)
def test_non_ascii_credential_returns_generic_401_without_persistence_or_logging(
    source: str,
    header_name: bytes,
    header_prefix: bytes,
    configured_secret: str,
    payloads: dict[str, dict[str, Any]],
    fake_engine: FakePostgreSQLEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    supplied_credential = "malformed-é-credential"
    raw_header_value = header_prefix + supplied_credential.encode("utf-8")

    with caplog.at_level(logging.DEBUG):
        response = request(
            "POST",
            f"/webhooks/{source}",
            content=json_bytes(payloads[source]),
            headers=[(header_name, raw_header_value)],
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "Authentication failed"}
    assert fake_engine.events == []
    assert supplied_credential not in response.text
    assert supplied_credential not in caplog.text
    assert configured_secret not in response.text
    assert configured_secret not in caplog.text


def test_authentication_runs_before_json_parsing(
    fake_engine: FakePostgreSQLEngine,
) -> None:
    response = request(
        "POST",
        "/webhooks/partner",
        content=b"not-json",
        headers={"X-API-Key": "wrong-key"},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Authentication failed"}
    assert fake_engine.events == []


def test_expired_website_timestamp_is_rejected_before_database(
    payloads: dict[str, dict[str, Any]],
    fake_engine: FakePostgreSQLEngine,
    settings: Settings,
) -> None:
    raw_body = json_bytes(payloads["website"])
    expired = str(int(time.time()) - settings.website_hmac_max_skew_seconds - 1)

    response = request(
        "POST",
        "/webhooks/website",
        content=raw_body,
        headers=website_headers(raw_body, timestamp=expired),
    )

    assert response.status_code == 401
    assert fake_engine.events == []


def test_website_signature_uses_exact_raw_body_bytes(
    payloads: dict[str, dict[str, Any]],
    fake_engine: FakePostgreSQLEngine,
) -> None:
    compact = json_bytes(payloads["website"])
    spaced = json.dumps(payloads["website"], indent=2).encode("utf-8")
    timestamp = str(int(time.time()))

    rejected = request(
        "POST",
        "/webhooks/website",
        content=spaced,
        headers=website_headers(compact, timestamp=timestamp),
    )
    accepted = request(
        "POST",
        "/webhooks/website",
        content=spaced,
        headers=website_headers(spaced, timestamp=timestamp),
    )

    assert rejected.status_code == 401
    assert accepted.status_code == 202
    assert fake_engine.events == ["begin", "execute", "commit"]


@pytest.mark.parametrize("source", ["website", "linkedin", "partner"])
def test_authentication_comparison_uses_compare_digest(
    source: str,
    payloads: dict[str, dict[str, Any]],
    fake_engine: FakePostgreSQLEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str | bytes, str | bytes]] = []
    real_compare_digest = hmac.compare_digest

    def recording_compare_digest(
        left: str | bytes,
        right: str | bytes,
    ) -> bool:
        calls.append((left, right))
        return real_compare_digest(left, right)

    monkeypatch.setattr(webhook_module.hmac, "compare_digest", recording_compare_digest)
    raw_body = json_bytes(payloads[source])

    response = request(
        "POST",
        f"/webhooks/{source}",
        content=raw_body,
        headers=valid_headers(source, raw_body),
    )

    assert response.status_code == 202
    assert len(calls) == 1
    if source in {"linkedin", "partner"}:
        assert all(isinstance(value, bytes) for value in calls[0])


def test_unsupported_or_case_mismatched_source_returns_404_without_database(
    fake_engine: FakePostgreSQLEngine,
) -> None:
    for source in ("unknown", "Website", "LINKEDIN"):
        response = request("POST", f"/webhooks/{source}", content=b"{}")
        assert response.status_code == 404

    assert fake_engine.events == []


@pytest.mark.parametrize(
    "raw_body",
    [
        b"{",
        b"[]",
        b"null",
        b'{"reference":NaN}',
        b'{"reference":' + (b"[" * 2_000) + (b"]" * 2_000) + b"}",
    ],
)
def test_malformed_or_non_object_json_is_rejected_after_authentication(
    raw_body: bytes,
    fake_engine: FakePostgreSQLEngine,
) -> None:
    response = request(
        "POST",
        "/webhooks/partner",
        content=raw_body,
        headers={"X-API-Key": TEST_PARTNER_KEY},
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid request"}
    assert fake_engine.events == []


@pytest.mark.parametrize(
    "event_id",
    [None, 123, "", " leading", "trailing ", "x" * 129],
)
def test_invalid_partner_event_id_is_rejected(
    event_id: Any,
    fake_engine: FakePostgreSQLEngine,
) -> None:
    payload = {} if event_id is None else {"reference": event_id}
    raw_body = json_bytes(payload)

    response = request(
        "POST",
        "/webhooks/partner",
        content=raw_body,
        headers=valid_headers("partner", raw_body),
    )

    assert response.status_code == 400
    assert fake_engine.events == []


@pytest.mark.parametrize(
    ("source", "payload"),
    [
        ("website", {"submissionId": "wrong-field-name"}),
        ("website", {"submission_id": 123}),
        ("linkedin", {"id": "wrong-level"}),
        ("linkedin", {"event": "not-an-object"}),
        ("linkedin", {"event": {"id": " surrounded "}}),
    ],
)
def test_source_specific_event_id_envelopes_are_exact(
    source: str,
    payload: dict[str, Any],
    fake_engine: FakePostgreSQLEngine,
) -> None:
    raw_body = json_bytes(payload)

    response = request(
        "POST",
        f"/webhooks/{source}",
        content=raw_body,
        headers=valid_headers(source, raw_body),
    )

    assert response.status_code == 400
    assert fake_engine.events == []


def test_route_source_is_authoritative_and_payload_is_preserved(
    fake_engine: FakePostgreSQLEngine,
) -> None:
    payload = {
        "reference": "Case-Sensitive-ID",
        "source": "website",
        "unknown_provider_field": {"nested": [1, 2, 3]},
    }
    raw_body = json_bytes(payload)

    response = request(
        "POST",
        "/webhooks/partner",
        content=raw_body,
        headers=valid_headers("partner", raw_body),
    )

    assert response.status_code == 202
    stored = fake_engine.rows[("partner", "Case-Sensitive-ID")]
    assert stored["source"] == "partner"
    assert stored["event_id"] == "Case-Sensitive-ID"
    assert stored["raw_payload"] == payload
    assert stored["event_status"] == "RECEIVED"
    for name in ("received_at", "created_at", "updated_at"):
        assert stored[name].tzinfo is not None
        assert stored[name].utcoffset().total_seconds() == 0


def test_oversized_payload_is_rejected_before_authentication_or_database(
    fake_engine: FakePostgreSQLEngine,
    settings: Settings,
) -> None:
    oversized = b"x" * (settings.max_webhook_body_bytes + 1)

    response = request(
        "POST",
        "/webhooks/partner",
        content=oversized,
        headers={"X-API-Key": TEST_PARTNER_KEY},
    )

    assert response.status_code == 413
    assert fake_engine.events == []


def test_duplicate_delivery_is_race_safe_and_does_not_mutate_original(
    fake_engine: FakePostgreSQLEngine,
) -> None:
    original = {"reference": "duplicate-id", "value": "original"}
    duplicate = {"reference": "duplicate-id", "value": "replacement"}

    first = request(
        "POST",
        "/webhooks/partner",
        content=json_bytes(original),
        headers={"X-API-Key": TEST_PARTNER_KEY},
    )
    second = request(
        "POST",
        "/webhooks/partner",
        content=json_bytes(duplicate),
        headers={"X-API-Key": TEST_PARTNER_KEY},
    )

    assert first.json() == {"status": "accepted", "duplicate": False}
    assert second.status_code == 202
    assert second.json() == {"status": "accepted", "duplicate": True}
    assert len(fake_engine.rows) == 1
    assert fake_engine.rows[("partner", "duplicate-id")]["raw_payload"] == original

    sql = str(
        fake_engine.statements[-1].compile(dialect=postgresql.dialect())
    ).upper()
    assert "ON CONFLICT ON CONSTRAINT UQ_WEBHOOK_EVENTS_SOURCE_EVENT_ID DO NOTHING" in sql
    assert "DO UPDATE" not in sql
    assert "RETURNING WEBHOOK_EVENTS.ID" in sql


def test_same_event_id_is_distinct_across_sources(
    fake_engine: FakePostgreSQLEngine,
) -> None:
    website_payload = {"submission_id": "shared-id"}
    partner_payload = {"reference": "shared-id"}
    website_raw = json_bytes(website_payload)
    partner_raw = json_bytes(partner_payload)

    website_response = request(
        "POST",
        "/webhooks/website",
        content=website_raw,
        headers=website_headers(website_raw),
    )
    partner_response = request(
        "POST",
        "/webhooks/partner",
        content=partner_raw,
        headers={"X-API-Key": TEST_PARTNER_KEY},
    )

    assert website_response.json()["duplicate"] is False
    assert partner_response.json()["duplicate"] is False
    assert set(fake_engine.rows) == {("website", "shared-id"), ("partner", "shared-id")}


@pytest.mark.parametrize("failure_point", ["execute", "commit"])
def test_database_failure_returns_generic_503_and_rolls_back(
    failure_point: str,
    fake_engine: FakePostgreSQLEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake_engine.fail_execute = failure_point == "execute"
    fake_engine.fail_commit = failure_point == "commit"
    payload = {
        "reference": "database-failure-id",
        "private_field": "payload-private-marker",
    }
    raw_body = json_bytes(payload)

    with caplog.at_level(logging.ERROR, logger="project3_crm.webhooks"):
        response = request(
            "POST",
            "/webhooks/partner",
            content=raw_body,
            headers={"X-API-Key": TEST_PARTNER_KEY},
        )

    assert response.status_code == 503
    assert response.json() == {"detail": "Service temporarily unavailable"}
    assert fake_engine.rows == {}
    assert "payload-private-marker" not in caplog.text
    assert TEST_PARTNER_KEY not in caplog.text
    assert "test-database-password" not in caplog.text
    assert "simulated database" not in caplog.text
