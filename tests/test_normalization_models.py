from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from project3_crm.normalization.models import (
    LinkedInPayload,
    NormalizedLeadData,
    PartnerPayload,
    WebsitePayload,
)


@pytest.mark.parametrize(
    ("source", "model_type", "timestamp_path"),
    [
        ("website", WebsitePayload, ("submitted_at",)),
        ("linkedin", LinkedInPayload, ("event", "createdAt")),
        ("partner", PartnerPayload, ("occurred_at",)),
    ],
)
def test_representative_provider_payloads_validate_with_utc_timestamps(
    source: str,
    model_type: type[Any],
    timestamp_path: tuple[str, ...],
    normalization_payloads: dict[str, dict[str, Any]],
) -> None:
    model = model_type.model_validate(normalization_payloads[source])
    timestamp: Any = model
    for part in timestamp_path:
        timestamp = getattr(timestamp, part)

    assert timestamp.tzinfo is UTC
    expected_minute = {"website": 30, "linkedin": 31, "partner": 32}[source]
    assert timestamp == datetime(2026, 9, 1, 12, expected_minute, tzinfo=UTC)
    assert model.model_extra is None


@pytest.mark.parametrize(
    ("source", "model_type", "remove_path"),
    [
        ("website", WebsitePayload, ("first_name",)),
        ("linkedin", LinkedInPayload, ("leadData", "firstName")),
        ("partner", PartnerPayload, ("person", "name", "given")),
    ],
)
def test_required_provider_fields_are_enforced(
    source: str,
    model_type: type[Any],
    remove_path: tuple[str, ...],
    normalization_payloads: dict[str, dict[str, Any]],
) -> None:
    payload = normalization_payloads[source]
    container = payload
    for part in remove_path[:-1]:
        container = container[part]
    del container[remove_path[-1]]

    with pytest.raises(ValidationError):
        model_type.model_validate(payload)


@pytest.mark.parametrize(
    ("source", "model_type", "email_path", "phone_path"),
    [
        ("website", WebsitePayload, ("email",), ("phone",)),
        (
            "linkedin",
            LinkedInPayload,
            ("leadData", "emailAddress"),
            ("leadData", "phoneNumber"),
        ),
        (
            "partner",
            PartnerPayload,
            ("person", "contacts", "email"),
            ("person", "contacts", "mobile"),
        ),
    ],
)
def test_email_and_phone_are_independently_optional_provider_fields(
    source: str,
    model_type: type[Any],
    email_path: tuple[str, ...],
    phone_path: tuple[str, ...],
    normalization_payloads: dict[str, dict[str, Any]],
) -> None:
    for removed_path in (email_path, phone_path, (email_path, phone_path)):
        payload = deepcopy(normalization_payloads[source])
        paths = removed_path if isinstance(removed_path[0], tuple) else (removed_path,)
        for path in paths:
            container = payload
            for part in path[:-1]:
                container = container[part]
            container.pop(path[-1], None)
        model_type.model_validate(payload)


def test_optional_nested_linkedin_and_partner_sections_may_be_absent(
    normalization_payloads: dict[str, dict[str, Any]],
) -> None:
    linkedin = normalization_payloads["linkedin"]
    for field in ("organization", "location", "campaign", "form"):
        linkedin.pop(field)
    partner = normalization_payloads["partner"]
    for field in ("business", "acquisition", "extras"):
        partner.pop(field)
    partner["person"].pop("location")

    assert LinkedInPayload.model_validate(linkedin).organization is None
    assert PartnerPayload.model_validate(partner).business is None


@pytest.mark.parametrize(
    "timestamp",
    ["2026-09-01T12:30:00", "not-a-timestamp", 123, True],
)
def test_naive_malformed_or_wrong_type_timestamp_is_rejected(
    timestamp: Any,
    normalization_payloads: dict[str, dict[str, Any]],
) -> None:
    payload = normalization_payloads["website"]
    payload["submitted_at"] = timestamp

    with pytest.raises(ValidationError):
        WebsitePayload.model_validate(payload)


def test_non_utc_timestamp_is_converted_to_utc(
    normalization_payloads: dict[str, dict[str, Any]],
) -> None:
    payload = normalization_payloads["website"]
    payload["submitted_at"] = "2026-09-01T15:30:00+03:00"

    model = WebsitePayload.model_validate(payload)

    assert model.submitted_at == datetime(2026, 9, 1, 12, 30, tzinfo=UTC)
    assert model.submitted_at.tzinfo is UTC


@pytest.mark.parametrize("field", ["first_name", "last_name"])
def test_required_names_trim_and_whitespace_only_fails(
    field: str,
    normalization_payloads: dict[str, dict[str, Any]],
) -> None:
    payload = normalization_payloads["website"]
    model = WebsitePayload.model_validate(payload)
    assert getattr(model, field) in {"Anna", "Petrova"}

    payload[field] = "   "
    with pytest.raises(ValidationError):
        WebsitePayload.model_validate(payload)


def test_optional_whitespace_only_strings_become_none(
    normalization_payloads: dict[str, dict[str, Any]],
) -> None:
    payload = normalization_payloads["website"]
    payload["phone"] = "   "
    payload["company"] = "   "

    model = WebsitePayload.model_validate(payload)

    assert model.phone is None
    assert model.company is None


@pytest.mark.parametrize("value", [123, True, {}, []])
def test_core_scalar_values_do_not_coerce_to_strings(
    value: Any,
    normalization_payloads: dict[str, dict[str, Any]],
) -> None:
    payload = normalization_payloads["website"]
    payload["email"] = value

    with pytest.raises(ValidationError):
        WebsitePayload.model_validate(payload)


def test_over_length_non_contact_field_fails_provider_validation(
    normalization_payloads: dict[str, dict[str, Any]],
) -> None:
    payload = normalization_payloads["website"]
    payload["company"] = "x" * 201

    with pytest.raises(ValidationError):
        WebsitePayload.model_validate(payload)


def test_common_normalized_model_allows_omitted_optional_contacts() -> None:
    normalized = NormalizedLeadData(
        source="website",
        source_event_id="WEB-1",
        submitted_at=datetime(2026, 9, 1, tzinfo=UTC),
        first_name="Anna",
        last_name="Petrova",
        email=None,
        phone=None,
        lead_source="website_form",
        source_metadata={},
    )

    assert normalized.email is None
    assert normalized.phone is None
