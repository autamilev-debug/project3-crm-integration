# 05 — Source Payloads, Adapters, and NormalizedLead

## Goal

Show meaningful adapter behavior without bloated fixtures:

```text
different provider contracts
→ provider-specific models
→ provider adapters
→ one NormalizedLead
```

Use the term `NormalizedLead`.

## Validation philosophy

For all three simulated source contracts, email and phone are independently optional
source fields. Project #3 does not impose a global email-or-phone minimum when a
source-valid payload omits both fields.

The integration does not attempt to prove that a person's email/phone/name is genuinely correct.

Provider models:

- do not coerce event IDs or core scalar fields from numbers/booleans into strings,
- may parse ISO-8601 timestamp strings into datetime values,
- ignore unknown provider fields rather than failing,
- preserve all unknown data only in the original `raw_payload`,
- map only deliberately selected source extras into `source_metadata`.

Normalization string rules:

- trim surrounding whitespace from mapped contact/business strings;
- required strings are validated as non-empty **after** trimming;
- optional strings that become empty after trimming map to `None`;
- normalized email is trimmed, validated with Pydantic `EmailStr`, and lowercased
  for deterministic CRM lookup/comparison;
- do not add business-level deliverability/identity checks.

Provider timestamps must be timezone-aware ISO-8601 values. Convert accepted timestamps to UTC. Reject naive timestamps with no timezone/offset.

Email maximum length remains 254.

### Contact validation

Email and phone accept only JSON strings when supplied; numbers, booleans, objects,
and arrays are provider-contract failures and are never coerced. Missing, null, or
whitespace-only optional contact values map to `None`.

Email validation is technical only: trim surrounding whitespace, enforce the
254-character maximum, validate normal email syntax with Pydantic `EmailStr`, and
lowercase accepted values. Do not perform DNS, mailbox, deliverability, verification,
or identity checks.

Phone validation is deliberately lightweight: trim surrounding whitespace, enforce
the 64-character maximum, allow `+` only once and only at the beginning, and require
7–15 digits after ignoring presentation characters. Preserve the trimmed valid
representation; do not convert it to E.164 or remove separators.

If a supplied email or phone string is invalid but the other contact method is valid,
discard only the invalid contact and continue. If a contact value is supplied but is
invalid and no other valid contact remains, normalization fails technical validation.
A payload that omits both optional contact fields may normalize successfully. Invalid
values remain only in `webhook_events.raw_payload`; they are not copied into
`source_metadata` or diagnostics.

## 1. website — flat snake_case

```json
{
  "submission_id": "WEB-01001",
  "submitted_at": "2026-09-01T12:30:00Z",
  "first_name": "Anna",
  "last_name": "Petrova",
  "email": "anna@example.com",
  "phone": "+359888111222",
  "company": "Example Ltd",
  "job_title": "Operations Manager",
  "city": "Sofia",
  "country": "Bulgaria",
  "form_id": "sales_contact_bg",
  "page_url": "https://example.test/pricing",
  "utm_source": "google",
  "utm_medium": "cpc",
  "utm_campaign": "autumn_demo",
  "preferred_contact": "email"
}
```

Required:

```text
submission_id
submitted_at
first_name
last_name
```

Event ID:

```text
submission_id
```

Mapping:

```text
submission_id  → source_event_id
submitted_at   → submitted_at
first_name     → first_name
last_name      → last_name
email          → email
phone          → phone
company        → company
job_title      → job_title
city           → city
country        → country
utm_campaign   → campaign
constant       → lead_source="website_form"
```

`source_metadata`:

```json
{
  "form_id": "sales_contact_bg",
  "page_url": "https://example.test/pricing",
  "utm_source": "google",
  "utm_medium": "cpc",
  "preferred_contact": "email"
}
```

## 2. linkedin — nested camelCase

```json
{
  "event": {
    "id": "LI-83921",
    "createdAt": "2026-09-01T12:31:00Z",
    "type": "LEAD_SUBMITTED"
  },
  "leadData": {
    "firstName": "Boris",
    "lastName": "Ivanov",
    "emailAddress": "boris@example.com",
    "phoneNumber": "+359888222333",
    "jobTitle": "Sales Director",
    "seniority": "director"
  },
  "organization": {
    "name": "Northstar AD",
    "industry": "Software"
  },
  "location": {
    "city": "Plovdiv",
    "country": "Bulgaria"
  },
  "campaign": {
    "id": "CMP-220",
    "name": "Q3 Lead Generation"
  },
  "form": {
    "id": "FORM-18",
    "name": "Enterprise Interest"
  }
}
```

Required:

```text
event.id
event.createdAt
leadData.firstName
leadData.lastName
```

Event ID:

```text
event.id
```

Mapping:

```text
event.id              → source_event_id
event.createdAt       → submitted_at
leadData.firstName    → first_name
leadData.lastName     → last_name
leadData.emailAddress → email
leadData.phoneNumber  → phone
organization.name     → company
leadData.jobTitle     → job_title
location.city         → city
location.country      → country
campaign.name         → campaign
constant              → lead_source="linkedin_like"
```

`source_metadata`:

```json
{
  "event_type": "LEAD_SUBMITTED",
  "campaign_id": "CMP-220",
  "form_id": "FORM-18",
  "form_name": "Enterprise Interest",
  "seniority": "director",
  "industry": "Software"
}
```

## 3. partner — differently nested contract

```json
{
  "reference": "PARTNER-5521",
  "occurred_at": "2026-09-01T12:32:00Z",
  "person": {
    "name": {
      "given": "Elena",
      "family": "Georgieva"
    },
    "contacts": {
      "email": "elena@example.com",
      "mobile": "+359888333444"
    },
    "location": {
      "city": "Varna",
      "country": "Bulgaria"
    }
  },
  "business": {
    "legal_name": "Partner Client OOD",
    "role": "Finance Manager",
    "employee_band": "50-99"
  },
  "acquisition": {
    "referrer_code": "REF-BG-19",
    "campaign_code": "PARTNER-AUTUMN",
    "channel": "reseller"
  },
  "extras": {
    "partner_tier": "gold",
    "consent_source": "partner_portal"
  }
}
```

Required:

```text
reference
occurred_at
person.name.given
person.name.family
```

Event ID:

```text
reference
```

Mapping:

```text
reference                      → source_event_id
occurred_at                    → submitted_at
person.name.given              → first_name
person.name.family             → last_name
person.contacts.email          → email
person.contacts.mobile         → phone
business.legal_name            → company
business.role                  → job_title
person.location.city           → city
person.location.country        → country
acquisition.campaign_code      → campaign
constant                       → lead_source="partner"
```

`source_metadata`:

```json
{
  "referrer_code": "REF-BG-19",
  "channel": "reseller",
  "employee_band": "50-99",
  "partner_tier": "gold",
  "consent_source": "partner_portal"
}
```

## NormalizedLead contract

```text
integration_reference
source
source_event_id
submitted_at
first_name
last_name
email
phone
company
job_title
country
city
campaign
lead_source
source_metadata
```

`integration_reference` is assigned only after the database ID is reserved.

First and last names are required by the three current provider contracts. Email and
phone are independently optional through provider validation and normalization.

## Field-size guardrails

Normalized string maxima:

```text
source_event_id 128
first_name 100
last_name 100
email 254
phone 64
company 200
job_title 150
country 100
city 100
campaign 200
lead_source 64
```

Never silently truncate source data. A non-contact mapped field exceeding its limit
fails normalization. An over-length email or phone makes only that contact method
invalid; normalization may still succeed when the other contact method is valid. A
payload that does not supply either optional contact field is not invalid for that
reason alone.

## source_metadata boundary

`source_metadata` demonstrates preservation of useful provider context, but it is not automatically sent to HubSpot.

CRM mapping remains explicit.

## Normalization failure

If the persisted payload cannot be converted into a valid `NormalizedLead` by the current provider model/adapter:

```text
PROCESSING
→ NORMALIZATION_FAILED
```

Persist:

- raw payload already stored,
- safe error category,
- sanitized validation summary.

For Pydantic/provider validation diagnostics, persist/log only safe field paths, error types, and short summaries. Do not include rejected input values. Cap stored diagnostic text at 1000 characters.

Do not automatically repeat the same deterministic normalization.

No dedicated reprocessing subsystem or public recovery endpoint is required in v1.
