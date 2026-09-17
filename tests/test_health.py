import asyncio

import httpx
import pytest

import project3_crm.integrations.hubspot as hubspot_module
import project3_crm.notifications.smtp as smtp_module
import project3_crm.web as web_module
from project3_crm.web import app


def test_application_imports() -> None:
    assert app.title == "Project #3 CRM Integration"


def test_health_returns_process_liveness() -> None:
    async def get_health() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.get("/health")

    response = asyncio.run(get_health())

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def _get(path: str) -> httpx.Response:
    async def request() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.get(path)

    return asyncio.run(request())


def test_health_performs_no_configuration_or_database_readiness_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_call(*args: object, **kwargs: object) -> None:
        raise AssertionError("health must remain a pure liveness check")

    monkeypatch.setattr(web_module, "get_settings", unexpected_call)
    monkeypatch.setattr(web_module, "get_database_engine", unexpected_call)
    monkeypatch.setattr(web_module, "check_database_readiness", unexpected_call)

    response = _get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_returns_200_after_configuration_and_database_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = object()
    calls: list[object] = []
    monkeypatch.setattr(web_module, "get_settings", lambda: calls.append("settings"))
    monkeypatch.setattr(web_module, "get_database_engine", lambda: engine)
    monkeypatch.setattr(
        web_module,
        "check_database_readiness",
        lambda received: calls.append(received),
    )

    response = _get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    assert calls == ["settings", engine]


def test_ready_database_failure_is_generic_and_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    private_marker = "postgresql://user:database-password@private-host/project3"
    monkeypatch.setattr(web_module, "get_settings", lambda: object())
    monkeypatch.setattr(web_module, "get_database_engine", lambda: object())

    def fail_readiness(engine: object) -> None:
        raise RuntimeError(private_marker)

    monkeypatch.setattr(web_module, "check_database_readiness", fail_readiness)

    with caplog.at_level("ERROR", logger="project3_crm.web"):
        response = _get("/ready")

    assert response.status_code == 503
    assert response.json() == {"detail": "Service not ready"}
    assert private_marker not in response.text
    assert private_marker not in caplog.text


def test_ready_configuration_failure_does_not_attempt_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        web_module,
        "get_settings",
        lambda: (_ for _ in ()).throw(ValueError("invalid configuration")),
    )
    database_calls = 0

    def database_engine() -> object:
        nonlocal database_calls
        database_calls += 1
        return object()

    monkeypatch.setattr(web_module, "get_database_engine", database_engine)

    response = _get("/ready")

    assert response.status_code == 503
    assert response.json() == {"detail": "Service not ready"}
    assert database_calls == 0


def test_ready_does_not_contact_hubspot_or_smtp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    external_calls: list[str] = []

    def hubspot_request(*args: object, **kwargs: object) -> None:
        external_calls.append("hubspot")
        raise AssertionError("readiness must not contact HubSpot")

    def smtp_connection(*args: object, **kwargs: object) -> None:
        external_calls.append("smtp")
        raise AssertionError("readiness must not connect to SMTP")

    monkeypatch.setattr(hubspot_module.httpx.Client, "request", hubspot_request)
    monkeypatch.setattr(smtp_module.smtplib, "SMTP", smtp_connection)
    monkeypatch.setattr(web_module, "get_settings", lambda: object())
    monkeypatch.setattr(web_module, "get_database_engine", lambda: object())
    monkeypatch.setattr(web_module, "check_database_readiness", lambda engine: None)

    response = _get("/ready")

    assert response.status_code == 200
    assert external_calls == []
