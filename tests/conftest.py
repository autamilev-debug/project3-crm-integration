from copy import deepcopy
from typing import Any

import pytest


NORMALIZATION_PAYLOADS: dict[str, dict[str, Any]] = {
    "website": {
        "submission_id": "WEB-01001",
        "submitted_at": "2026-09-01T12:30:00Z",
        "first_name": "  Anna  ",
        "last_name": "  Petrova  ",
        "email": "  ANNA@EXAMPLE.COM  ",
        "phone": "  +359 888 111 222  ",
        "company": "  Example Ltd  ",
        "job_title": "  Operations Manager  ",
        "city": "  Sofia  ",
        "country": "  Bulgaria  ",
        "form_id": "  sales_contact_bg  ",
        "page_url": "  https://example.test/pricing  ",
        "utm_source": "  google  ",
        "utm_medium": "  cpc  ",
        "utm_campaign": "  autumn_demo  ",
        "preferred_contact": "  email  ",
        "unknown_provider_field": "raw-only-value",
    },
    "linkedin": {
        "event": {
            "id": "LI-83921",
            "createdAt": "2026-09-01T15:31:00+03:00",
            "type": "  LEAD_SUBMITTED  ",
            "unknownEventField": True,
        },
        "leadData": {
            "firstName": "  Boris  ",
            "lastName": "  Ivanov  ",
            "emailAddress": "  BORIS@EXAMPLE.COM  ",
            "phoneNumber": "  +359 (888) 222-333  ",
            "jobTitle": "  Sales Director  ",
            "seniority": "  director  ",
        },
        "organization": {
            "name": "  Northstar AD  ",
            "industry": "  Software  ",
        },
        "location": {"city": "  Plovdiv  ", "country": "  Bulgaria  "},
        "campaign": {"id": "  CMP-220  ", "name": "  Q3 Lead Generation  "},
        "form": {"id": "  FORM-18  ", "name": "  Enterprise Interest  "},
        "unknownTopLevel": {"kept": "only in raw payload"},
    },
    "partner": {
        "reference": "PARTNER-5521",
        "occurred_at": "2026-09-01T12:32:00Z",
        "person": {
            "name": {"given": "  Elena  ", "family": "  Georgieva  "},
            "contacts": {
                "email": "  ELENA@EXAMPLE.COM  ",
                "mobile": "  +359 888 333 444  ",
            },
            "location": {"city": "  Varna  ", "country": "  Bulgaria  "},
        },
        "business": {
            "legal_name": "  Partner Client OOD  ",
            "role": "  Finance Manager  ",
            "employee_band": "  50-99  ",
        },
        "acquisition": {
            "referrer_code": "  REF-BG-19  ",
            "campaign_code": "  PARTNER-AUTUMN  ",
            "channel": "  reseller  ",
        },
        "extras": {
            "partner_tier": "  gold  ",
            "consent_source": "  partner_portal  ",
        },
        "unknownPartnerField": ["raw", "only"],
    },
}


@pytest.fixture
def normalization_payloads() -> dict[str, dict[str, Any]]:
    return deepcopy(NORMALIZATION_PAYLOADS)
