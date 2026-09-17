"""Concrete CREATE_ONLY HubSpot Contact HTTP adapter."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from project3_crm.config import Settings


_READ_PROPERTIES = "email,firstname,lastname,integration_reference"
_RETURNED_PROPERTY_NAMES = frozenset(
    {"email", "firstname", "lastname", "integration_reference"}
)
_SAFE_DIAGNOSTIC_VALUE = re.compile(r"^[A-Za-z0-9_.:-]+$")
_DIAGNOSTIC_FIELD_LIMIT = 128
_PERSISTED_DIAGNOSTIC_LIMIT = 1_000
_RETRY_AFTER_LIMIT = 128


class HubSpotContactInput(BaseModel):
    """Only normalized fields approved to cross the CRM boundary."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    integration_reference: str = Field(min_length=1, max_length=32)
    email: str | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=64)
    company: str | None = Field(default=None, max_length=200)
    job_title: str | None = Field(default=None, max_length=150)
    city: str | None = Field(default=None, max_length=100)
    country: str | None = Field(default=None, max_length=100)

    @field_validator("first_name", "last_name", "integration_reference")
    @classmethod
    def require_non_whitespace_value(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("required CRM delivery value must not be blank")
        return value

    def hubspot_properties(self) -> dict[str, str]:
        """Build the explicit allowlisted Contact properties payload."""

        mapped = {
            "email": self.email,
            "firstname": self.first_name,
            "lastname": self.last_name,
            "phone": self.phone,
            "company": self.company,
            "jobtitle": self.job_title,
            "city": self.city,
            "country": self.country,
            "integration_reference": self.integration_reference,
        }
        return {name: value for name, value in mapped.items() if value is not None}


class CreateResultCategory(StrEnum):
    SUCCESS = "SUCCESS"
    AUTH_FAILURE = "AUTH_FAILURE"
    CONFIG_FAILURE = "CONFIG_FAILURE"
    CONFLICT_REQUIRES_LOOKUP = "CONFLICT_REQUIRES_LOOKUP"
    RETRYABLE_FAILURE = "RETRYABLE_FAILURE"
    RATE_LIMITED = "RATE_LIMITED"
    UNKNOWN = "UNKNOWN"
    BAD_REQUEST_REQUIRES_SAFE_CLASSIFICATION = (
        "BAD_REQUEST_REQUIRES_SAFE_CLASSIFICATION"
    )
    UNCLASSIFIED_PERMANENT_FAILURE = "UNCLASSIFIED_PERMANENT_FAILURE"


class ReadResultCategory(StrEnum):
    FOUND = "FOUND"
    ACTIVE_NOT_FOUND = "ACTIVE_NOT_FOUND"
    AUTH_FAILURE = "AUTH_FAILURE"
    CONFIG_FAILURE = "CONFIG_FAILURE"
    RETRYABLE_READ_FAILURE = "RETRYABLE_READ_FAILURE"
    INDETERMINATE_READ = "INDETERMINATE_READ"


@dataclass(frozen=True)
class HubSpotDiagnostics:
    """Sanitized provider evidence suitable for later persistence."""

    description: str
    http_status: int | None = None
    category: str | None = None
    error_code: str | None = None
    correlation_id: str | None = None
    retry_after: str | None = None

    def as_persistence_text(self) -> str:
        parts = [self.description]
        for name, value in (
            ("http_status", self.http_status),
            ("category", self.category),
            ("error_code", self.error_code),
            ("correlation_id", self.correlation_id),
            ("retry_after", self.retry_after),
        ):
            if value is not None:
                parts.append(f"{name}={value}")
        return "; ".join(parts)[:_PERSISTED_DIAGNOSTIC_LIMIT]


@dataclass(frozen=True)
class CreateContactResult:
    category: CreateResultCategory
    contact_id: str | None
    diagnostics: HubSpotDiagnostics

    @property
    def retry_after(self) -> str | None:
        return self.diagnostics.retry_after


@dataclass(frozen=True)
class ReadContactResult:
    category: ReadResultCategory
    contact_id: str | None
    properties: dict[str, str | None] | None
    diagnostics: HubSpotDiagnostics

    @property
    def retry_after(self) -> str | None:
        return self.diagnostics.retry_after


def _safe_structured_value(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if (
        not candidate
        or len(candidate) > _DIAGNOSTIC_FIELD_LIMIT
        or _SAFE_DIAGNOSTIC_VALUE.fullmatch(candidate) is None
    ):
        return None
    return candidate


def _safe_retry_after(value: str | None) -> str | None:
    if value is None:
        return None
    candidate = value.strip()
    if not candidate or len(candidate) > _RETRY_AFTER_LIMIT:
        return None
    if any(ord(character) < 32 or ord(character) > 126 for character in candidate):
        return None
    return candidate


def _response_json_object(response: httpx.Response) -> dict[str, Any] | None:
    try:
        payload = response.json()
    except (ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _valid_contact_id(payload: dict[str, Any] | None) -> str | None:
    if payload is None:
        return None
    value = payload.get("id")
    if not isinstance(value, str) or isinstance(value, bool):
        return None
    candidate = value.strip()
    return candidate or None


def _diagnostics_for_response(
    response: httpx.Response,
    description: str,
    payload: dict[str, Any] | None = None,
) -> HubSpotDiagnostics:
    if payload is None:
        payload = _response_json_object(response)
    return HubSpotDiagnostics(
        description=description,
        http_status=response.status_code,
        category=_safe_structured_value(payload.get("category")) if payload else None,
        error_code=_safe_structured_value(payload.get("code")) if payload else None,
        correlation_id=(
            _safe_structured_value(payload.get("correlationId")) if payload else None
        ),
        retry_after=_safe_retry_after(response.headers.get("Retry-After")),
    )


def _transport_diagnostics(description: str) -> HubSpotDiagnostics:
    return HubSpotDiagnostics(description=description)


class HubSpotAdapter:
    """Small synchronous adapter for approved HubSpot Contact operations."""

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self._base_url = settings.hubspot_api_base_url.rstrip("/")
        self._api_version = settings.hubspot_api_version
        self._service_key = settings.hubspot_service_key
        self._timeout = settings.hubspot_http_timeout_seconds
        self._client = client if client is not None else httpx.Client()
        self._owns_client = client is None

    def __enter__(self) -> HubSpotAdapter:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Close only a client created by this adapter."""

        if self._owns_client:
            self._client.close()

    def create_contact(self, contact: HubSpotContactInput) -> CreateContactResult:
        """Perform exactly one CREATE_ONLY Contact request."""

        try:
            response = self._client.post(
                self._contacts_url,
                headers=self._authorization_headers,
                json={"properties": contact.hubspot_properties()},
                timeout=self._timeout,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            return CreateContactResult(
                category=CreateResultCategory.RETRYABLE_FAILURE,
                contact_id=None,
                diagnostics=_transport_diagnostics(
                    "create failed before transmission could begin"
                ),
            )
        except httpx.TransportError:
            return CreateContactResult(
                category=CreateResultCategory.UNKNOWN,
                contact_id=None,
                diagnostics=_transport_diagnostics(
                    "create transmission outcome is uncertain"
                ),
            )

        return self._classify_create_response(response)

    def get_contact_by_integration_reference(
        self,
        integration_reference: str,
    ) -> ReadContactResult:
        return self._get_contact(integration_reference, "integration_reference")

    def get_contact_by_email(self, email: str) -> ReadContactResult:
        return self._get_contact(email, "email")

    @property
    def _contacts_url(self) -> str:
        version = quote(self._api_version, safe="")
        return f"{self._base_url}/crm/objects/{version}/contacts"

    @property
    def _authorization_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._service_key.get_secret_value()}",
            "Accept": "application/json",
        }

    def _get_contact(self, value: str, id_property: str) -> ReadContactResult:
        encoded_value = quote(value, safe="")
        try:
            response = self._client.get(
                f"{self._contacts_url}/{encoded_value}",
                headers=self._authorization_headers,
                params={
                    "idProperty": id_property,
                    "properties": _READ_PROPERTIES,
                },
                timeout=self._timeout,
            )
        except httpx.TransportError:
            return ReadContactResult(
                category=ReadResultCategory.INDETERMINATE_READ,
                contact_id=None,
                properties=None,
                diagnostics=_transport_diagnostics(
                    "active read outcome is indeterminate"
                ),
            )
        return self._classify_read_response(response)

    @staticmethod
    def _classify_create_response(response: httpx.Response) -> CreateContactResult:
        payload = _response_json_object(response)
        status = response.status_code

        if 200 <= status < 300:
            contact_id = _valid_contact_id(payload)
            if contact_id is not None:
                category = CreateResultCategory.SUCCESS
                description = "Contact create succeeded"
            else:
                category = CreateResultCategory.UNKNOWN
                description = "create success response was unusable"
            return CreateContactResult(
                category=category,
                contact_id=contact_id,
                diagnostics=_diagnostics_for_response(response, description, payload),
            )

        category = {
            400: CreateResultCategory.BAD_REQUEST_REQUIRES_SAFE_CLASSIFICATION,
            401: CreateResultCategory.AUTH_FAILURE,
            403: CreateResultCategory.CONFIG_FAILURE,
            404: CreateResultCategory.CONFIG_FAILURE,
            405: CreateResultCategory.CONFIG_FAILURE,
            409: CreateResultCategory.CONFLICT_REQUIRES_LOOKUP,
            423: CreateResultCategory.RETRYABLE_FAILURE,
            429: CreateResultCategory.RATE_LIMITED,
            477: CreateResultCategory.RETRYABLE_FAILURE,
        }.get(status)
        if category is None:
            if 500 <= status < 600:
                category = CreateResultCategory.UNKNOWN
            elif 400 <= status < 500:
                category = CreateResultCategory.UNCLASSIFIED_PERMANENT_FAILURE
            else:
                category = CreateResultCategory.UNKNOWN

        return CreateContactResult(
            category=category,
            contact_id=None,
            diagnostics=_diagnostics_for_response(
                response,
                f"Contact create returned {category.value}",
                payload,
            ),
        )

    @staticmethod
    def _classify_read_response(response: httpx.Response) -> ReadContactResult:
        payload = _response_json_object(response)
        status = response.status_code

        if status == 200:
            contact_id = _valid_contact_id(payload)
            raw_properties = payload.get("properties") if payload else None
            if contact_id is not None and isinstance(raw_properties, dict):
                returned_properties = {
                    name: value
                    for name, value in raw_properties.items()
                    if name in _RETURNED_PROPERTY_NAMES
                }
                if all(
                    isinstance(value, str) or value is None
                    for value in returned_properties.values()
                ):
                    return ReadContactResult(
                        category=ReadResultCategory.FOUND,
                        contact_id=contact_id,
                        properties=returned_properties,
                        diagnostics=_diagnostics_for_response(
                            response, "active Contact found", payload
                        ),
                    )

        category = {
            404: ReadResultCategory.ACTIVE_NOT_FOUND,
            401: ReadResultCategory.AUTH_FAILURE,
            400: ReadResultCategory.CONFIG_FAILURE,
            403: ReadResultCategory.CONFIG_FAILURE,
            405: ReadResultCategory.CONFIG_FAILURE,
            423: ReadResultCategory.RETRYABLE_READ_FAILURE,
            429: ReadResultCategory.RETRYABLE_READ_FAILURE,
            477: ReadResultCategory.RETRYABLE_READ_FAILURE,
        }.get(status, ReadResultCategory.INDETERMINATE_READ)
        return ReadContactResult(
            category=category,
            contact_id=None,
            properties=None,
            diagnostics=_diagnostics_for_response(
                response,
                f"active read returned {category.value}",
                payload,
            ),
        )
