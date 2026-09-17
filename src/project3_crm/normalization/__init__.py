"""Provider validation and normalization public API."""

from project3_crm.normalization.adapters import (
    NormalizationFailure,
    normalize_linkedin,
    normalize_partner,
    normalize_payload,
    normalize_website,
)
from project3_crm.normalization.models import (
    LinkedInPayload,
    NormalizedLeadData,
    PartnerPayload,
    WebsitePayload,
)
from project3_crm.normalization.service import (
    NormalizationOutcome,
    NormalizationStateError,
    StoredWebhookEvent,
    integration_reference_for_id,
    normalize_webhook_event,
)

__all__ = [
    "LinkedInPayload",
    "NormalizationFailure",
    "NormalizationOutcome",
    "NormalizationStateError",
    "NormalizedLeadData",
    "PartnerPayload",
    "StoredWebhookEvent",
    "WebsitePayload",
    "integration_reference_for_id",
    "normalize_linkedin",
    "normalize_partner",
    "normalize_payload",
    "normalize_website",
    "normalize_webhook_event",
]
