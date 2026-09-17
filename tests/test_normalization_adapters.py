from datetime import UTC, datetime
from typing import Any

import pytest

from project3_crm.normalization import NormalizationFailure, normalize_payload


EXPECTED_NORMALIZED = {
    "website": {
        "source": "website",
        "source_event_id": "WEB-01001",
        "submitted_at": datetime(2026, 9, 1, 12, 30, tzinfo=UTC),
        "first_name": "Anna",
        "last_name": "Petrova",
        "email": "anna@example.com",
        "phone": "+359 888 111 222",
        "company": "Example Ltd",
        "job_title": "Operations Manager",
        "country": "Bulgaria",
        "city": "Sofia",
        "campaign": "autumn_demo",
        "lead_source": "website_form",
        "source_metadata": {
            "form_id": "sales_contact_bg",
            "page_url": "https://example.test/pricing",
            "utm_source": "google",
            "utm_medium": "cpc",
            "preferred_contact": "email",
        },
    },
    "linkedin": {
        "source": "linkedin",
        "source_event_id": "LI-83921",
        "submitted_at": datetime(2026, 9, 1, 12, 31, tzinfo=UTC),
        "first_name": "Boris",
        "last_name": "Ivanov",
        "email": "boris@example.com",
        "phone": "+359 (888) 222-333",
        "company": "Northstar AD",
        "job_title": "Sales Director",
        "country": "Bulgaria",
        "city": "Plovdiv",
        "campaign": "Q3 Lead Generation",
        "lead_source": "linkedin_like",
        "source_metadata": {
            "event_type": "LEAD_SUBMITTED",
            "campaign_id": "CMP-220",
            "form_id": "FORM-18",
            "form_name": "Enterprise Interest",
            "seniority": "director",
            "industry": "Software",
        },
    },
    "partner": {
        "source": "partner",
        "source_event_id": "PARTNER-5521",
        "submitted_at": datetime(2026, 9, 1, 12, 32, tzinfo=UTC),
        "first_name": "Elena",
        "last_name": "Georgieva",
        "email": "elena@example.com",
        "phone": "+359 888 333 444",
        "company": "Partner Client OOD",
        "job_title": "Finance Manager",
        "country": "Bulgaria",
        "city": "Varna",
        "campaign": "PARTNER-AUTUMN",
        "lead_source": "partner",
        "source_metadata": {
            "referrer_code": "REF-BG-19",
            "channel": "reseller",
            "employee_band": "50-99",
            "partner_tier": "gold",
            "consent_source": "partner_portal",
        },
    },
}


@pytest.mark.parametrize("source", ["website", "linkedin", "partner"])
def test_each_source_maps_exactly_to_normalized_lead_data(
    source: str,
    normalization_payloads: dict[str, dict[str, Any]],
) -> None:
    event_id = EXPECTED_NORMALIZED[source]["source_event_id"]

    normalized = normalize_payload(
        source,
        event_id,
        normalization_payloads[source],
    )

    assert normalized.model_dump() == EXPECTED_NORMALIZED[source]
    assert "unknown_provider_field" not in normalized.source_metadata
    assert "unknownTopLevel" not in normalized.source_metadata
    assert "unknownPartnerField" not in normalized.source_metadata


@pytest.mark.parametrize(
    ("email", "phone", "expected_email", "expected_phone"),
    [
        ("ONLY@EXAMPLE.COM", None, "only@example.com", None),
        (None, "+359 888 123 456", None, "+359 888 123 456"),
        (
            "BOTH@EXAMPLE.COM",
            "+359 888 123 456",
            "both@example.com",
            "+359 888 123 456",
        ),
        ("not-an-email", "+359 888 123 456", None, "+359 888 123 456"),
        ("valid@example.com", "invalid-phone", "valid@example.com", None),
        ("x" * 255, "+359 888 123 456", None, "+359 888 123 456"),
        ("valid@example.com", "1" * 65, "valid@example.com", None),
    ],
)
def test_contact_normalization_discards_one_invalid_string_contact(
    email: str | None,
    phone: str | None,
    expected_email: str | None,
    expected_phone: str | None,
    normalization_payloads: dict[str, dict[str, Any]],
) -> None:
    payload = normalization_payloads["website"]
    payload["email"] = email
    payload["phone"] = phone

    normalized = normalize_payload("website", "WEB-01001", payload)

    assert normalized.email == expected_email
    assert normalized.phone == expected_phone


@pytest.mark.parametrize(
    ("email", "phone", "expected_codes"),
    [
        ("not-an-email", None, ("contact.email: invalid_email",)),
        (
            "not-an-email",
            "invalid-phone",
            ("contact.email: invalid_email", "contact.phone: invalid_phone"),
        ),
        ("x" * 255, "123", ("contact.email: invalid_email", "contact.phone: invalid_phone")),
    ],
)
def test_supplied_invalid_contacts_produce_only_sanitized_diagnostics(
    email: str | None,
    phone: str | None,
    expected_codes: tuple[str, ...],
    normalization_payloads: dict[str, dict[str, Any]],
) -> None:
    payload = normalization_payloads["website"]
    payload["email"] = email
    payload["phone"] = phone

    with pytest.raises(NormalizationFailure) as exc_info:
        normalize_payload("website", "WEB-01001", payload)

    failure = exc_info.value
    assert failure.category == "CONTACT_VALIDATION"
    for code in expected_codes:
        assert code in failure.diagnostic
    for rejected_value in (email, phone):
        if rejected_value:
            assert rejected_value not in failure.diagnostic
    assert len(failure.diagnostic) <= 1_000


@pytest.mark.parametrize("source", ["website", "linkedin", "partner"])
def test_source_valid_payload_without_email_or_phone_normalizes(
    source: str,
    normalization_payloads: dict[str, dict[str, Any]],
) -> None:
    payload = normalization_payloads[source]
    if source == "website":
        payload.pop("email")
        payload.pop("phone")
        event_id = payload["submission_id"]
    elif source == "linkedin":
        payload["leadData"].pop("emailAddress")
        payload["leadData"].pop("phoneNumber")
        event_id = payload["event"]["id"]
    else:
        payload["person"].pop("contacts")
        event_id = payload["reference"]

    normalized = normalize_payload(source, event_id, payload)

    assert normalized.email is None
    assert normalized.phone is None


@pytest.mark.parametrize(("field", "value"), [("email", 123), ("phone", True)])
def test_wrong_contact_scalar_type_is_fatal_even_when_other_contact_is_valid(
    field: str,
    value: Any,
    normalization_payloads: dict[str, dict[str, Any]],
) -> None:
    payload = normalization_payloads["website"]
    payload[field] = value
    pii_marker = "private-person@example.com"
    payload["first_name"] = pii_marker

    with pytest.raises(NormalizationFailure) as exc_info:
        normalize_payload("website", "WEB-01001", payload)

    assert exc_info.value.category == "PROVIDER_VALIDATION"
    assert f"provider.{field}: string_type" in exc_info.value.diagnostic
    assert pii_marker not in exc_info.value.diagnostic
    assert str(value) not in exc_info.value.diagnostic


def test_provider_event_id_must_match_persisted_identity(
    normalization_payloads: dict[str, dict[str, Any]],
) -> None:
    with pytest.raises(NormalizationFailure) as exc_info:
        normalize_payload(
            "website",
            "DIFFERENT-ID",
            normalization_payloads["website"],
        )

    assert exc_info.value.diagnostic == "provider.event_id: identity_mismatch"
