from __future__ import annotations

import logging
from typing import Any, Callable

import httpx
import pytest
from pydantic import ValidationError

from project3_crm.config import Settings
from project3_crm.integrations.hubspot import (
    CreateResultCategory,
    HubSpotAdapter,
    HubSpotContactInput,
    ReadResultCategory,
)


FAKE_SERVICE_KEY = "fake-service-key-ticket-7"


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": "postgresql://user:password@localhost/project3_test",
        "website_hmac_secret": "fake-website-secret",
        "linkedin_bearer_token": "fake-linkedin-token",
        "partner_api_key": "fake-partner-key",
        "hubspot_service_key": FAKE_SERVICE_KEY,
        "hubspot_api_base_url": "https://hubspot.example.test",
        "hubspot_api_version": "2026-03",
        "hubspot_http_timeout_seconds": 12.5,
        "smtp_host": "smtp.example.test",
        "smtp_username": "fake-smtp-user",
        "smtp_password": "fake-smtp-password",
        "smtp_from_email": "automation@example.test",
        "ops_email_to": "ops@example.test",
        "automation_email_to": "automation@example.test",
    }
    values.update(overrides)
    return Settings(**values, _env_file=None)


def _contact(**overrides: Any) -> HubSpotContactInput:
    values: dict[str, Any] = {
        "first_name": "Ada",
        "last_name": "Lovelace",
        "integration_reference": "IR-00042",
        "email": "ada@example.test",
    }
    values.update(overrides)
    return HubSpotContactInput(**values)


def _adapter_with_handler(
    handler: Callable[[httpx.Request], httpx.Response],
    **settings_overrides: Any,
) -> tuple[HubSpotAdapter, httpx.Client]:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return HubSpotAdapter(_settings(**settings_overrides), client=client), client


@pytest.mark.parametrize(
    ("overrides", "expected_properties"),
    [
        (
            {
                "phone": "+359 888 111 222",
                "company": "Analytical Engines Ltd",
                "job_title": "Mathematician",
                "city": "London",
                "country": "United Kingdom",
            },
            {
                "email": "ada@example.test",
                "firstname": "Ada",
                "lastname": "Lovelace",
                "phone": "+359 888 111 222",
                "company": "Analytical Engines Ltd",
                "jobtitle": "Mathematician",
                "city": "London",
                "country": "United Kingdom",
                "integration_reference": "IR-00042",
            },
        ),
        (
            {
                "phone": None,
                "company": None,
                "job_title": None,
                "city": None,
                "country": None,
            },
            {
                "email": "ada@example.test",
                "firstname": "Ada",
                "lastname": "Lovelace",
                "integration_reference": "IR-00042",
            },
        ),
        (
            {"email": "email-only@example.test", "phone": None},
            {
                "email": "email-only@example.test",
                "firstname": "Ada",
                "lastname": "Lovelace",
                "integration_reference": "IR-00042",
            },
        ),
        (
            {"email": None, "phone": "+359 888 222 333"},
            {
                "firstname": "Ada",
                "lastname": "Lovelace",
                "phone": "+359 888 222 333",
                "integration_reference": "IR-00042",
            },
        ),
    ],
)
def test_create_sends_exact_allowlisted_payload(
    overrides: dict[str, str | None],
    expected_properties: dict[str, str],
) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(201, json={"id": "12345"})

    adapter, client = _adapter_with_handler(handler)
    try:
        result = adapter.create_contact(_contact(**overrides))
    finally:
        client.close()

    assert result.category is CreateResultCategory.SUCCESS
    assert captured[0].read().decode() == httpx.Request(
        "POST",
        "https://example.test",
        json={"properties": expected_properties},
    ).read().decode()
    assert set(expected_properties).isdisjoint(
        {"source_metadata", "campaign", "lead_source", "source", "source_event_id"}
    )


@pytest.mark.parametrize("field", ["first_name", "last_name", "integration_reference"])
def test_required_contact_fields_reject_blank_values(field: str) -> None:
    with pytest.raises(ValidationError):
        _contact(**{field: "   "})


def test_create_uses_configured_url_version_authentication_and_timeout() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(201, json={"id": "contact-1"})

    adapter, client = _adapter_with_handler(
        handler,
        hubspot_api_base_url="https://configured.example.test/api-root/",
        hubspot_api_version="2026-03",
        hubspot_http_timeout_seconds=7.25,
    )
    try:
        adapter.create_contact(_contact())
    finally:
        client.close()

    request = captured[0]
    assert str(request.url) == (
        "https://configured.example.test/api-root/crm/objects/2026-03/contacts"
    )
    assert request.headers["Authorization"] == f"Bearer {FAKE_SERVICE_KEY}"
    assert request.extensions["timeout"] == {
        "connect": 7.25,
        "read": 7.25,
        "write": 7.25,
        "pool": 7.25,
    }


@pytest.mark.parametrize("status", [200, 201, 202, 299])
def test_create_2xx_with_valid_string_id_is_success(status: int) -> None:
    adapter, client = _adapter_with_handler(
        lambda _request: httpx.Response(status, json={"id": "  contact-123  "})
    )
    try:
        result = adapter.create_contact(_contact())
    finally:
        client.close()

    assert result.category is CreateResultCategory.SUCCESS
    assert result.contact_id == "contact-123"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={}),
        httpx.Response(200, json={"id": "   "}),
        httpx.Response(200, json={"id": 123}),
        httpx.Response(200, json={"id": {"value": "123"}}),
        httpx.Response(200, json={"id": ["123"]}),
        httpx.Response(200, json={"id": True}),
        httpx.Response(200, json=[]),
        httpx.Response(200, content=b"not-json"),
    ],
)
def test_unusable_create_2xx_is_unknown(response: httpx.Response) -> None:
    adapter, client = _adapter_with_handler(lambda _request: response)
    try:
        result = adapter.create_contact(_contact())
    finally:
        client.close()

    assert result.category is CreateResultCategory.UNKNOWN
    assert result.contact_id is None


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (400, CreateResultCategory.BAD_REQUEST_REQUIRES_SAFE_CLASSIFICATION),
        (401, CreateResultCategory.AUTH_FAILURE),
        (403, CreateResultCategory.CONFIG_FAILURE),
        (404, CreateResultCategory.CONFIG_FAILURE),
        (405, CreateResultCategory.CONFIG_FAILURE),
        (409, CreateResultCategory.CONFLICT_REQUIRES_LOOKUP),
        (423, CreateResultCategory.RETRYABLE_FAILURE),
        (429, CreateResultCategory.RATE_LIMITED),
        (477, CreateResultCategory.RETRYABLE_FAILURE),
        (418, CreateResultCategory.UNCLASSIFIED_PERMANENT_FAILURE),
        (500, CreateResultCategory.UNKNOWN),
        (503, CreateResultCategory.UNKNOWN),
    ],
)
def test_create_status_classification(
    status: int,
    expected: CreateResultCategory,
) -> None:
    request_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(status, json={"message": "not used for classification"})

    adapter, client = _adapter_with_handler(handler)
    try:
        result = adapter.create_contact(_contact())
    finally:
        client.close()

    assert result.category is expected
    assert request_count == 1


class _OtherAmbiguousTransportError(httpx.TransportError):
    pass


@pytest.mark.parametrize(
    ("exception_factory", "expected"),
    [
        (
            lambda request: httpx.ConnectError("fake", request=request),
            CreateResultCategory.RETRYABLE_FAILURE,
        ),
        (
            lambda request: httpx.ConnectTimeout("fake", request=request),
            CreateResultCategory.RETRYABLE_FAILURE,
        ),
        (
            lambda request: httpx.PoolTimeout("fake", request=request),
            CreateResultCategory.RETRYABLE_FAILURE,
        ),
        (
            lambda request: httpx.WriteError("fake", request=request),
            CreateResultCategory.UNKNOWN,
        ),
        (
            lambda request: httpx.WriteTimeout("fake", request=request),
            CreateResultCategory.UNKNOWN,
        ),
        (
            lambda request: httpx.ReadError("fake", request=request),
            CreateResultCategory.UNKNOWN,
        ),
        (
            lambda request: httpx.ReadTimeout("fake", request=request),
            CreateResultCategory.UNKNOWN,
        ),
        (
            lambda request: httpx.RemoteProtocolError("fake", request=request),
            CreateResultCategory.UNKNOWN,
        ),
        (
            lambda request: _OtherAmbiguousTransportError("fake", request=request),
            CreateResultCategory.UNKNOWN,
        ),
    ],
)
def test_create_transport_safety_boundary(
    exception_factory: Callable[[httpx.Request], httpx.TransportError],
    expected: CreateResultCategory,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exception_factory(request)

    adapter, client = _adapter_with_handler(handler)
    try:
        result = adapter.create_contact(_contact())
    finally:
        client.close()

    assert result.category is expected
    assert result.contact_id is None


def test_integration_reference_read_request_and_found_result() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "id": "contact-7",
                "properties": {
                    "email": "ada@example.test",
                    "firstname": "Ada",
                    "lastname": "Lovelace",
                    "integration_reference": "IR-00042",
                    "unrequested": "not carried across the boundary",
                },
            },
        )

    adapter, client = _adapter_with_handler(handler)
    try:
        result = adapter.get_contact_by_integration_reference("IR-00042")
    finally:
        client.close()

    assert result.category is ReadResultCategory.FOUND
    assert result.contact_id == "contact-7"
    assert result.properties == {
        "email": "ada@example.test",
        "firstname": "Ada",
        "lastname": "Lovelace",
        "integration_reference": "IR-00042",
    }
    request = captured[0]
    assert request.url.path == "/crm/objects/2026-03/contacts/IR-00042"
    assert request.url.params["idProperty"] == "integration_reference"
    assert request.url.params["properties"] == (
        "email,firstname,lastname,integration_reference"
    )


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (404, ReadResultCategory.ACTIVE_NOT_FOUND),
        (401, ReadResultCategory.AUTH_FAILURE),
        (400, ReadResultCategory.CONFIG_FAILURE),
        (403, ReadResultCategory.CONFIG_FAILURE),
        (405, ReadResultCategory.CONFIG_FAILURE),
        (423, ReadResultCategory.RETRYABLE_READ_FAILURE),
        (429, ReadResultCategory.RETRYABLE_READ_FAILURE),
        (477, ReadResultCategory.RETRYABLE_READ_FAILURE),
        (500, ReadResultCategory.INDETERMINATE_READ),
        (503, ReadResultCategory.INDETERMINATE_READ),
        (418, ReadResultCategory.INDETERMINATE_READ),
    ],
)
def test_active_read_status_classification(
    status: int,
    expected: ReadResultCategory,
) -> None:
    adapter, client = _adapter_with_handler(
        lambda _request: httpx.Response(status, json={"message": "ignored"})
    )
    try:
        result = adapter.get_contact_by_integration_reference("IR-00042")
    finally:
        client.close()

    assert result.category is expected
    assert result.contact_id is None
    assert result.properties is None


def test_email_read_url_encodes_path_and_requests_verification_properties() -> None:
    captured: list[httpx.Request] = []
    email = "lead+reserved @example.test/path?value"

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={"id": "contact-8", "properties": {"email": email}},
        )

    adapter, client = _adapter_with_handler(handler)
    try:
        result = adapter.get_contact_by_email(email)
    finally:
        client.close()

    assert result.category is ReadResultCategory.FOUND
    assert result.properties == {"email": email}
    assert captured[0].url.raw_path.split(b"?")[0].endswith(
        b"/lead%2Breserved%20%40example.test%2Fpath%3Fvalue"
    )
    assert captured[0].url.params["idProperty"] == "email"
    assert captured[0].url.params["properties"] == _READ_PROPERTIES_FOR_TEST


_READ_PROPERTIES_FOR_TEST = "email,firstname,lastname,integration_reference"


def test_email_read_404_is_active_not_found() -> None:
    adapter, client = _adapter_with_handler(
        lambda _request: httpx.Response(404, json={"status": "error"})
    )
    try:
        result = adapter.get_contact_by_email("missing@example.test")
    finally:
        client.close()

    assert result.category is ReadResultCategory.ACTIVE_NOT_FOUND


@pytest.mark.parametrize(
    "body",
    [
        [],
        {},
        {"id": None, "properties": {}},
        {"id": "", "properties": {}},
        {"id": "   ", "properties": {}},
        {"id": 123, "properties": {}},
        {"id": True, "properties": {}},
        {"id": {"value": "123"}, "properties": {}},
        {"id": ["123"], "properties": {}},
        {"id": "123"},
        {"id": "123", "properties": []},
        {"id": "123", "properties": "not-an-object"},
        {"id": "123", "properties": {"email": 123}},
        {"id": "123", "properties": {"integration_reference": {}}},
    ],
)
def test_malformed_read_200_is_indeterminate(body: Any) -> None:
    adapter, client = _adapter_with_handler(
        lambda _request: httpx.Response(200, json=body)
    )
    try:
        result = adapter.get_contact_by_integration_reference("IR-00042")
    finally:
        client.close()

    assert result.category is ReadResultCategory.INDETERMINATE_READ


def test_non_json_read_200_is_indeterminate() -> None:
    adapter, client = _adapter_with_handler(
        lambda _request: httpx.Response(200, content=b"not-json")
    )
    try:
        result = adapter.get_contact_by_email("ada@example.test")
    finally:
        client.close()

    assert result.category is ReadResultCategory.INDETERMINATE_READ


def test_non_200_read_2xx_is_indeterminate() -> None:
    adapter, client = _adapter_with_handler(
        lambda _request: httpx.Response(
            201,
            json={"id": "123", "properties": {}},
        )
    )
    try:
        result = adapter.get_contact_by_email("ada@example.test")
    finally:
        client.close()

    assert result.category is ReadResultCategory.INDETERMINATE_READ


def test_read_transport_error_is_indeterminate() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("fake", request=request)

    adapter, client = _adapter_with_handler(handler)
    try:
        result = adapter.get_contact_by_email("ada@example.test")
    finally:
        client.close()

    assert result.category is ReadResultCategory.INDETERMINATE_READ


def test_retry_after_and_diagnostics_are_safely_allowlisted_and_bounded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fictional_email = "private.person@example.test"
    fictional_phone = "+359-888-999-000"
    provider_secret = "provider-secret-like-value"
    response_body = {
        "category": "RATE_LIMIT",
        "code": "RATE_LIMITED",
        "correlationId": "corr-123",
        "message": (
            f"Request for {fictional_email} / {fictional_phone}; "
            f"Authorization: Bearer {provider_secret}"
        ),
        "request": {"properties": {"email": fictional_email}},
    }
    caplog.set_level(logging.DEBUG)
    adapter, client = _adapter_with_handler(
        lambda _request: httpx.Response(
            429,
            json=response_body,
            headers={"Retry-After": " 120 "},
        )
    )
    try:
        result = adapter.create_contact(_contact())
    finally:
        client.close()

    persistence_text = result.diagnostics.as_persistence_text()
    exposed = f"{result!r} {persistence_text} {caplog.text}"
    assert result.retry_after == "120"
    assert result.diagnostics.category == "RATE_LIMIT"
    assert result.diagnostics.error_code == "RATE_LIMITED"
    assert result.diagnostics.correlation_id == "corr-123"
    assert len(persistence_text) <= 1_000
    assert fictional_email not in exposed
    assert fictional_phone not in exposed
    assert provider_secret not in exposed
    assert FAKE_SERVICE_KEY not in exposed
    assert "Authorization" not in exposed


@pytest.mark.parametrize(
    "retry_after",
    ["x" * 129, "line\nbreak", "   "],
)
def test_unusable_retry_after_is_omitted(retry_after: str) -> None:
    adapter, client = _adapter_with_handler(
        lambda _request: httpx.Response(
            429,
            headers={"Retry-After": retry_after},
        )
    )
    try:
        result = adapter.create_contact(_contact())
    finally:
        client.close()

    assert result.retry_after is None


def test_malformed_structured_diagnostics_are_omitted() -> None:
    adapter, client = _adapter_with_handler(
        lambda _request: httpx.Response(
            400,
            json={
                "category": "contains private.person@example.test",
                "code": ["not", "a", "string"],
                "correlationId": "x" * 129,
            },
        )
    )
    try:
        result = adapter.create_contact(_contact())
    finally:
        client.close()

    assert result.diagnostics.category is None
    assert result.diagnostics.error_code is None
    assert result.diagnostics.correlation_id is None


def test_adapter_does_not_close_injected_client() -> None:
    adapter, client = _adapter_with_handler(
        lambda _request: httpx.Response(201, json={"id": "123"})
    )
    adapter.close()
    try:
        assert client.is_closed is False
    finally:
        client.close()


def test_adapter_closes_owned_client() -> None:
    adapter = HubSpotAdapter(_settings())
    adapter.close()

    assert adapter._client.is_closed is True
