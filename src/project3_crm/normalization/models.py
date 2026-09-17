"""Strict provider payload models and the common normalized lead contract."""

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)


def _optional_trimmed_string(value: Any) -> Any:
    if isinstance(value, str):
        trimmed = value.strip()
        return trimmed or None
    return value


def _aware_utc_datetime(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("must be an ISO-8601 string with timezone information")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("must be a valid ISO-8601 timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp timezone is required")
    return parsed.astimezone(UTC)


def _exact_event_id(value: Any) -> Any:
    if isinstance(value, str) and value != value.strip():
        raise ValueError("event ID must not contain surrounding whitespace")
    return value


RequiredString100 = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        strict=True,
        min_length=1,
        max_length=100,
    ),
]
EventId = Annotated[
    str,
    BeforeValidator(_exact_event_id),
    StringConstraints(strict=True, min_length=1, max_length=128),
]
AwareUtcDatetime = Annotated[datetime, BeforeValidator(_aware_utc_datetime)]
OptionalContactString = Annotated[
    str | None,
    BeforeValidator(_optional_trimmed_string),
]


def optional_string(maximum_length: int | None = None) -> Any:
    string_type = Annotated[
        str,
        StringConstraints(strict=True, max_length=maximum_length),
    ]
    return Annotated[
        string_type | None,
        BeforeValidator(_optional_trimmed_string),
    ]


OptionalString = optional_string()
OptionalString100 = optional_string(100)
OptionalString150 = optional_string(150)
OptionalString200 = optional_string(200)


class ProviderModel(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)


class WebsitePayload(ProviderModel):
    submission_id: EventId
    submitted_at: AwareUtcDatetime
    first_name: RequiredString100
    last_name: RequiredString100
    email: OptionalContactString = None
    phone: OptionalContactString = None
    company: OptionalString200 = None
    job_title: OptionalString150 = None
    city: OptionalString100 = None
    country: OptionalString100 = None
    form_id: OptionalString = None
    page_url: OptionalString = None
    utm_source: OptionalString = None
    utm_medium: OptionalString = None
    utm_campaign: OptionalString200 = None
    preferred_contact: OptionalString = None


class LinkedInEvent(ProviderModel):
    id: EventId
    createdAt: AwareUtcDatetime
    type: OptionalString = None


class LinkedInLeadData(ProviderModel):
    firstName: RequiredString100
    lastName: RequiredString100
    emailAddress: OptionalContactString = None
    phoneNumber: OptionalContactString = None
    jobTitle: OptionalString150 = None
    seniority: OptionalString = None


class LinkedInOrganization(ProviderModel):
    name: OptionalString200 = None
    industry: OptionalString = None


class LinkedInLocation(ProviderModel):
    city: OptionalString100 = None
    country: OptionalString100 = None


class LinkedInCampaign(ProviderModel):
    id: OptionalString = None
    name: OptionalString200 = None


class LinkedInForm(ProviderModel):
    id: OptionalString = None
    name: OptionalString = None


class LinkedInPayload(ProviderModel):
    event: LinkedInEvent
    leadData: LinkedInLeadData
    organization: LinkedInOrganization | None = None
    location: LinkedInLocation | None = None
    campaign: LinkedInCampaign | None = None
    form: LinkedInForm | None = None


class PartnerName(ProviderModel):
    given: RequiredString100
    family: RequiredString100


class PartnerContacts(ProviderModel):
    email: OptionalContactString = None
    mobile: OptionalContactString = None


class PartnerLocation(ProviderModel):
    city: OptionalString100 = None
    country: OptionalString100 = None


class PartnerPerson(ProviderModel):
    name: PartnerName
    contacts: PartnerContacts | None = None
    location: PartnerLocation | None = None


class PartnerBusiness(ProviderModel):
    legal_name: OptionalString200 = None
    role: OptionalString150 = None
    employee_band: OptionalString = None


class PartnerAcquisition(ProviderModel):
    referrer_code: OptionalString = None
    campaign_code: OptionalString200 = None
    channel: OptionalString = None


class PartnerExtras(ProviderModel):
    partner_tier: OptionalString = None
    consent_source: OptionalString = None


class PartnerPayload(ProviderModel):
    reference: EventId
    occurred_at: AwareUtcDatetime
    person: PartnerPerson
    business: PartnerBusiness | None = None
    acquisition: PartnerAcquisition | None = None
    extras: PartnerExtras | None = None


class NormalizedLeadData(BaseModel):
    """Provider-neutral data ready for the normalized_leads table."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    source: Literal["website", "linkedin", "partner"]
    source_event_id: str = Field(strict=True, min_length=1, max_length=128)
    submitted_at: datetime
    first_name: str = Field(strict=True, min_length=1, max_length=100)
    last_name: str = Field(strict=True, min_length=1, max_length=100)
    email: str | None = Field(default=None, strict=True, max_length=254)
    phone: str | None = Field(default=None, strict=True, max_length=64)
    company: str | None = Field(default=None, strict=True, max_length=200)
    job_title: str | None = Field(default=None, strict=True, max_length=150)
    country: str | None = Field(default=None, strict=True, max_length=100)
    city: str | None = Field(default=None, strict=True, max_length=100)
    campaign: str | None = Field(default=None, strict=True, max_length=200)
    lead_source: str = Field(strict=True, min_length=1, max_length=64)
    source_metadata: dict[str, Any]

    @field_validator("submitted_at")
    @classmethod
    def normalize_submitted_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("submitted_at timezone is required")
        return value.astimezone(UTC)
