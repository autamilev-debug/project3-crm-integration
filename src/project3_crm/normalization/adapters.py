"""Deterministic source-specific adapters for stored webhook payloads."""

from dataclasses import dataclass
from typing import Any, Callable

from pydantic import EmailStr, TypeAdapter, ValidationError

from project3_crm.normalization.models import (
    LinkedInPayload,
    NormalizedLeadData,
    PartnerPayload,
    WebsitePayload,
)


EMAIL_ADAPTER = TypeAdapter(EmailStr)
PHONE_PRESENTATION_CHARACTERS = frozenset(" -()")
DIAGNOSTIC_LIMIT = 1_000


@dataclass(frozen=True)
class NormalizationFailure(Exception):
    category: str
    diagnostic: str

    def __str__(self) -> str:
        return self.diagnostic


def _sanitized_validation_diagnostic(error: ValidationError) -> str:
    parts: list[str] = []
    for item in error.errors(include_input=False, include_url=False):
        location = ".".join(str(part) for part in item["loc"])
        error_type = str(item["type"])
        parts.append(f"provider.{location}: {error_type}")
    return "; ".join(parts)[:DIAGNOSTIC_LIMIT]


def _normalize_email(value: str | None) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    if len(value) > 254:
        return None, "contact.email: invalid_email"
    try:
        normalized = str(EMAIL_ADAPTER.validate_python(value))
    except ValidationError:
        return None, "contact.email: invalid_email"
    return normalized.lower(), None


def _normalize_phone(value: str | None) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    if len(value) > 64:
        return None, "contact.phone: invalid_phone"
    if value.count("+") > 1 or ("+" in value and not value.startswith("+")):
        return None, "contact.phone: invalid_phone"

    digits = 0
    for index, character in enumerate(value):
        if character in "0123456789":
            digits += 1
        elif character == "+" and index == 0:
            continue
        elif character not in PHONE_PRESENTATION_CHARACTERS:
            return None, "contact.phone: invalid_phone"
    if not 7 <= digits <= 15:
        return None, "contact.phone: invalid_phone"
    return value, None


def _normalized_contacts(
    email_value: str | None,
    phone_value: str | None,
) -> tuple[str | None, str | None]:
    email, email_error = _normalize_email(email_value)
    phone, phone_error = _normalize_phone(phone_value)
    if (
        email is not None
        or phone is not None
        or (email_error is None and phone_error is None)
    ):
        return email, phone

    diagnostics = [error for error in (email_error, phone_error) if error]
    raise NormalizationFailure(
        category="CONTACT_VALIDATION",
        diagnostic="; ".join(diagnostics)[:DIAGNOSTIC_LIMIT],
    )


def _metadata(**values: str | None) -> dict[str, str | None]:
    return values


def normalize_website(payload: WebsitePayload) -> NormalizedLeadData:
    email, phone = _normalized_contacts(payload.email, payload.phone)
    return NormalizedLeadData(
        source="website",
        source_event_id=payload.submission_id,
        submitted_at=payload.submitted_at,
        first_name=payload.first_name,
        last_name=payload.last_name,
        email=email,
        phone=phone,
        company=payload.company,
        job_title=payload.job_title,
        country=payload.country,
        city=payload.city,
        campaign=payload.utm_campaign,
        lead_source="website_form",
        source_metadata=_metadata(
            form_id=payload.form_id,
            page_url=payload.page_url,
            utm_source=payload.utm_source,
            utm_medium=payload.utm_medium,
            preferred_contact=payload.preferred_contact,
        ),
    )


def normalize_linkedin(payload: LinkedInPayload) -> NormalizedLeadData:
    email, phone = _normalized_contacts(
        payload.leadData.emailAddress,
        payload.leadData.phoneNumber,
    )
    organization = payload.organization
    location = payload.location
    campaign = payload.campaign
    form = payload.form
    return NormalizedLeadData(
        source="linkedin",
        source_event_id=payload.event.id,
        submitted_at=payload.event.createdAt,
        first_name=payload.leadData.firstName,
        last_name=payload.leadData.lastName,
        email=email,
        phone=phone,
        company=organization.name if organization else None,
        job_title=payload.leadData.jobTitle,
        country=location.country if location else None,
        city=location.city if location else None,
        campaign=campaign.name if campaign else None,
        lead_source="linkedin_like",
        source_metadata=_metadata(
            event_type=payload.event.type,
            campaign_id=campaign.id if campaign else None,
            form_id=form.id if form else None,
            form_name=form.name if form else None,
            seniority=payload.leadData.seniority,
            industry=organization.industry if organization else None,
        ),
    )


def normalize_partner(payload: PartnerPayload) -> NormalizedLeadData:
    contacts = payload.person.contacts
    email, phone = _normalized_contacts(
        contacts.email if contacts else None,
        contacts.mobile if contacts else None,
    )
    location = payload.person.location
    business = payload.business
    acquisition = payload.acquisition
    extras = payload.extras
    return NormalizedLeadData(
        source="partner",
        source_event_id=payload.reference,
        submitted_at=payload.occurred_at,
        first_name=payload.person.name.given,
        last_name=payload.person.name.family,
        email=email,
        phone=phone,
        company=business.legal_name if business else None,
        job_title=business.role if business else None,
        country=location.country if location else None,
        city=location.city if location else None,
        campaign=acquisition.campaign_code if acquisition else None,
        lead_source="partner",
        source_metadata=_metadata(
            referrer_code=acquisition.referrer_code if acquisition else None,
            channel=acquisition.channel if acquisition else None,
            employee_band=business.employee_band if business else None,
            partner_tier=extras.partner_tier if extras else None,
            consent_source=extras.consent_source if extras else None,
        ),
    )


Adapter = Callable[[Any], NormalizedLeadData]
PROVIDER_MODELS = {
    "website": WebsitePayload,
    "linkedin": LinkedInPayload,
    "partner": PartnerPayload,
}
ADAPTERS: dict[str, Adapter] = {
    "website": normalize_website,
    "linkedin": normalize_linkedin,
    "partner": normalize_partner,
}


def normalize_payload(
    source: str,
    event_id: str,
    raw_payload: dict[str, Any],
) -> NormalizedLeadData:
    """Validate one stored provider payload and return provider-neutral data."""

    model_type = PROVIDER_MODELS.get(source)
    adapter = ADAPTERS.get(source)
    if model_type is None or adapter is None:
        raise NormalizationFailure(
            category="PROVIDER_VALIDATION",
            diagnostic="provider.source: unsupported_source",
        )
    try:
        provider_payload = model_type.model_validate(raw_payload)
    except ValidationError as error:
        raise NormalizationFailure(
            category="PROVIDER_VALIDATION",
            diagnostic=_sanitized_validation_diagnostic(error),
        ) from None

    normalized = adapter(provider_payload)
    if normalized.source_event_id != event_id:
        raise NormalizationFailure(
            category="PROVIDER_VALIDATION",
            diagnostic="provider.event_id: identity_mismatch",
        )
    return normalized
