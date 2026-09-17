# 07 — HubSpot CRM Adapter

## Contract

The concrete implementation is `HubSpotAdapter`, behind a CRM-adapter boundary.

v1 outbound mutation:

```text
CREATE Contact only
```

No automatic update/upsert/delete.

## API baseline

Use the date-versioned HubSpot CRM API baseline:

```text
2026-03
```

Implementation endpoints:

```text
POST /crm/objects/2026-03/contacts
GET  /crm/objects/2026-03/contacts/{value}?idProperty=<property>
```

The GET endpoint is used with:

```text
idProperty=integration_reference
idProperty=email
```

Do not use CRM Search for unknown-write recovery.

Batch read was tested manually during discovery, but v1 uses the simpler single-record GET because the worker processes one CRM lead at a time.

Before deployment, re-check current official HubSpot docs. Do not silently migrate API versions without human review.

## HubSpot Contact property prerequisite

The HubSpot account must contain:

```text
Label: Integration Reference
Internal name: integration_reference
Type: single-line text/string
Unique values: enabled
```

This was manually created in the test account.

Important limitation discovered manually: HubSpot's unique-value behavior does not provide permanent uniqueness across archived Contacts. Once a Contact is archived, the same property value can be reused by a new active Contact.

Therefore:

- PostgreSQL is the permanent source of uniqueness for our integration references;
- HubSpot uniqueness is useful protection for active Contacts;
- active `404` lookup cannot prove a reference was never used historically.

Before deployment/live tests, manually verify the property still exists and unique values remain enabled.

The application does not create HubSpot schema in v1.

## Create payload

Map only approved fields:

```text
email                 → email
first_name            → firstname
last_name             → lastname
phone                 → phone
company               → company
job_title             → jobtitle
city                  → city
country               → country
integration_reference → integration_reference
```

Do not automatically send `source_metadata`, `campaign`, or `lead_source` unless a later human-approved CRM mapping explicitly adds HubSpot properties for them.

## CREATE_ONLY

The business policy is deliberate:

```text
new Contact → create
existing Contact → do not overwrite
```

Never automatically call upsert, PATCH, PUT, or DELETE in response to a conflict.

The prior Postman upsert test was discovery only and proved why upsert is not this company's policy.

## Active read request shapes

### By integration reference

```http
GET /crm/objects/2026-03/contacts/IR-00001
    ?idProperty=integration_reference
    &properties=email,firstname,lastname,integration_reference
```

Observed manually:

```text
existing active reference → 200 + Contact
missing active reference  → 404
```

### By email

```http
GET /crm/objects/2026-03/contacts/{email}?idProperty=email
```

URL-encode the email path value. Use this only to confirm an active pre-existing Contact after a create conflict.

### Archived-record limitation

Do **not** attempt to prove historical absence with:

```text
archived=true + idProperty=integration_reference
```

Manual testing returned:

```text
400 VALIDATION_ERROR
Cannot fetch deleted objects by unique property...
```

A subsequent manual create using the same integration reference previously held by an archived Contact returned `201 Created`.

Consequently there is no `DEFINITIVE_NOT_FOUND` classifier in v1.

## Response-classification precedence

Do not parse arbitrary English error prose.

### Create call

| Evidence | Adapter result |
|---|---|
| 2xx + valid Contact ID | `SUCCESS` |
| unexpected/malformed 2xx | `UNKNOWN` |
| `401` | `AUTH_FAILURE` |
| `403` | `CONFIG_FAILURE` |
| `404`/`405` on frozen create endpoint | `CONFIG_FAILURE` |
| `409` | `CONFLICT_REQUIRES_LOOKUP` |
| `423` | `RETRYABLE_FAILURE` |
| `429` | `RATE_LIMITED` |
| `477` | `RETRYABLE_FAILURE` using `Retry-After` |
| `httpx.ConnectError` / `ConnectTimeout` / `PoolTimeout` | `RETRYABLE_FAILURE` |
| `httpx.WriteError` / `WriteTimeout` / `ReadError` / `ReadTimeout` / `RemoteProtocolError` | `UNKNOWN` |
| `5xx` or other post-transmission/ambiguous transport uncertainty | `UNKNOWN` |
| `400` | `BAD_REQUEST_REQUIRES_SAFE_CLASSIFICATION` |
| other unrecognized `4xx` | `UNCLASSIFIED_PERMANENT_FAILURE` |
| any response whose body cannot establish the required outcome safely | conservative path; never blind resend |

### Active read/reconciliation call

A unique-reference active GET can produce:

```text
200 + valid Contact → FOUND(contact_id, properties)
404 → ACTIVE_NOT_FOUND
401 → AUTH_FAILURE
400/403/405 → CONFIG_FAILURE
423/429/477 → RETRYABLE_READ_FAILURE
5xx/timeout/malformed/other ambiguous response → INDETERMINATE_READ
```

`ACTIVE_NOT_FOUND` is not equivalent to historical nonexistence.

## UNKNOWN reconciliation rule

For an uncertain create:

```text
UNKNOWN
→ active GET by integration_reference
```

- `FOUND` → validate returned `integration_reference` and normalized email; only an exact reference match plus normalized-email match becomes `SUCCESS` and stores the HubSpot Contact ID.
- found-record reference/email mismatch → remain `UNKNOWN` + global CONFIG/data-integrity pause; never `SUCCESS`.
- completed `404` → schedule another delayed active check.
- three completed active `404` results → `UNKNOWN_ESCALATED`.
- never automatically resend an `UNKNOWN` create.

The three checks are intended to tolerate short visibility delays only. They do not claim to prove that an archived Contact does not exist.

## 409 classification

Because v1 never retries an `UNKNOWN` create automatically, a create reaching `409` is handled as a normal create conflict.

```text
409
→ active GET by integration_reference
   200 + expected reference/email match → current lead PENDING + CONFIG pause (unexpected active reference collision)
   200 + reference/email mismatch → current lead PENDING + CONFIG/data-integrity pause
   401 → current lead PENDING + AUTH pause
   400/403/405 → current lead PENDING + CONFIG pause
   404 → if email present, active GET by email
       200 + valid Contact ID + exact normalized-email match → EXISTING_CONTACT; store Contact ID
       200 + missing/mismatching email or invalid Contact ID → current lead PENDING + CONFIG/data-integrity pause
       401 → current lead PENDING + AUTH pause
       400/403/405 → current lead PENDING + CONFIG pause
       404 → FAILED_ESCALATED unclassified conflict
       indeterminate → FAILED_ESCALATED technical/unclassified
   indeterminate → FAILED_ESCALATED technical/unclassified
```

This avoids relying on the observed English phrase "Contact already exists".

## 400 classification

For create `400`, first active-read the lead's `integration_reference`:

- `FOUND` with expected reference/email → current lead `PENDING` + CONFIG pause for unexpected active reference collision;
- `FOUND` with reference/email mismatch → current lead `PENDING` + CONFIG/data-integrity pause;
- active `404` → `FAILED_ESCALATED`; structured fields may distinguish ordinary per-record validation from technical/unclassified `400` for alert aggregation;
- read `401`/`403`/frozen-endpoint config failure → current lead `PENDING` + matching global pause;
- indeterminate follow-up read → `FAILED_ESCALATED` technical/unclassified; no resend.

Never parse free-form English message text for business decisions.

## Error diagnostics

Persist/log only a sanitized allowlist:

```text
HTTP status
HubSpot category if present
structured error code if present
HubSpot correlationId if present
Retry-After if present
sanitized/truncated message up to 1000 chars
```

Never persist/log Authorization headers, Service Keys, full request payloads as error strings, or complete provider responses that may echo PII.

## Manual Postman discovery evidence

### 2026-09-01

| Check | Observed |
|---|---|
| authenticated Contacts GET | 200 |
| create new Contact | 201 |
| same email create | 409 CONFLICT |
| upsert discovery test | 200; existing Contact changed |
| create Contact with unique `integration_reference` | 201 |
| Search existing reference | 200; one |
| Search nonexistent reference | 200; zero |
| duplicate unique reference against active Contact | 400 VALIDATION_ERROR |
| no auth | 401 INVALID_AUTHENTICATION |
| rate-limit headers | present |

The upsert result supports the deliberate CREATE_ONLY policy; upsert is not used in implementation.

Manual discovery used `INT-000001`; implementation format is `IR-xxxxx`.

### 2026-09-02 — direct/batch unique-property reads and archive behavior

Observed active Contact:

```text
HubSpot ID: 858179816647
integration_reference: INT-000001
```

Results:

| Check | Observed |
|---|---|
| batch read existing `INT-000001` by `idProperty=integration_reference` | 200 COMPLETE; Contact returned |
| batch read nonexistent `INT-999999` | 207 COMPLETE; `OBJECT_NOT_FOUND` in errors |
| batch read nonexistent with `archived=true` + custom unique property | 400 VALIDATION_ERROR |
| single GET existing `INT-000001` by custom unique property | 200; Contact returned |
| single GET nonexistent `INT-999999` by custom unique property | 404 |
| repeat single GET existing `INT-000001` before Ticket 1B | 200; Contact ID `858179816647` returned again |
| repeat single GET nonexistent `INT-999999` before Ticket 1B | 404 again |
| create disposable `INT-ARCHIVE-001` Contact | 201 |
| archive disposable Contact by Record ID | 204 No Content |
| single GET archived Contact with `archived=true` + custom unique property | 400 VALIDATION_ERROR |
| create new active Contact reusing archived `INT-ARCHIVE-001` | 201 Created |

The archive tests are the reason unknown-write recovery does **not** automatically resend after repeated active `404` results.

DELETE was used only to manufacture this manual test case. DELETE is not part of the Project #3 integration flow.

## Deployment verification gate

Before live deployment:

1. re-check current Service Key status,
2. re-check current supported API version,
3. verify Contact read/write scopes,
4. verify `integration_reference` internal name/type/unique setting,
5. rerun one active unique-property GET,
6. keep the archived-reference limitation explicitly accepted; do not silently reintroduce automatic resend-from-UNKNOWN behavior.
