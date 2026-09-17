# 00 — Frozen Project Contract

## Objective

Build a durable multi-source CRM integration service:

```text
external sources
→ authenticated FastAPI webhook
→ PostgreSQL raw-event persistence
→ provider-specific normalization
→ one NormalizedLead
→ reliable CREATE_ONLY HubSpot Contact delivery
```

HubSpot is the concrete CRM used for implementation/testing. CRM-specific behavior stays behind a CRM adapter.

## Responsibility boundary

### Source systems own

- required form/submission fields,
- frontend/source validation,
- whether a submission is allowed,
- generation of a stable source event/submission ID.

### Integration owns

- supported source routing,
- inbound authentication,
- technical request/envelope validation,
- raw-event persistence before acknowledgement,
- `(source, event_id)` idempotency,
- provider-specific mapping,
- `NormalizedLead`,
- durable CRM delivery,
- retries/backoff,
- rate-limit handling,
- unknown-write recovery,
- safe logging/diagnostics,
- SMTP operational notifications.

### CRM/business layer owns

- customer/person identity resolution,
- duplicate/profile matching and merging,
- application/submission association,
- CRM data-quality policy,
- assignment, queues, Cases/tasks, workflows, reports,
- business escalation and customer/account decisions.

## CREATE_ONLY business policy

The fictional company deliberately uses:

```text
CRM_CONTACT_DELIVERY_POLICY=CREATE_ONLY
```

Therefore:

- send Contact create requests,
- never automatically upsert,
- never automatically overwrite an existing Contact,
- a confirmed pre-existing Contact by email becomes `EXISTING_CONTACT`,
- `EXISTING_CONTACT` is a business outcome, not a technical failure,
- preserve the normalized lead locally.

This is business-specific, not a universal CRM integration rule.

## Inbound sources and authentication

Use exactly:

```text
website  → HMAC-SHA256 + timestamp replay protection
linkedin → Bearer token
partner  → X-API-Key
```

Exact payloads/mappings are in `05_normalization.md`.

## Persistence and runtime

Frozen v1 topology:

```text
FastAPI web process
ONE worker process / ONE Railway worker replica
PostgreSQL
```

The worker is separate from FastAPI and processes outbound CRM work sequentially.

PostgreSQL is the durable work store. Do not add an external queue in v1.

Before **every** CRM-work selection (including immediately after startup), if any CRM row is already `SENDING`, the worker must not start another HubSpot create/read. A fresh `SENDING` row is allowed to reach the normal 15-minute stale lease; once stale it becomes `UNKNOWN` and is reconciled before new CRM work continues. This also covers an unexpected in-process exception that leaves a row `SENDING`, without adding distributed coordination.

Worker stale-processing lease:

```text
15 minutes default
configurable
```

## Database stack

Use:

```text
PostgreSQL
SQLAlchemy 2.x
psycopg 3
Alembic
```

Use synchronous SQLAlchemy sessions and modern 2.0-style SQL-expression statements. Do not use legacy `Session.query()`.

Alembic owns deployed schema migrations. Do not use startup `create_all()` as production schema management.

## Integration reference

Reference format:

```text
IR-00001
IR-00002
...
IR-99999
IR-100000
```

Rule:

```python
f"IR-{normalized_lead_id:05d}"
```

Five digits are minimum width, not maximum width.

`integration_reference` is:

- unique locally,
- immutable,
- stored in HubSpot in the custom unique Contact property `integration_reference`,
- technical delivery identity only.

## HubSpot baseline

Implementation baseline:

```text
API version: 2026-03
Contact create: POST /crm/objects/2026-03/contacts
Active read by integration reference:
GET /crm/objects/2026-03/contacts/{integration_reference}?idProperty=integration_reference
Active read by email:
GET /crm/objects/2026-03/contacts/{email}?idProperty=email
```

Do not use CRM Search for unknown-write recovery.

Manual Postman discovery on 2026-09-02 proved that an active Contact can be retrieved directly by the custom unique `integration_reference`, and a nonexistent active reference returns HTTP `404`. This direct GET behavior was then re-tested once more before Ticket 1B: `INT-000001` again returned `200` with Contact ID `858179816647`, and `INT-999999` again returned `404`.

Important HubSpot limitation discovered manually:

```text
archived=true + idProperty=integration_reference
→ 400 VALIDATION_ERROR
```

HubSpot stated that deleted/archived objects cannot be fetched by a unique property because more than one deleted object may have the same value. A second manual test then proved that a new active Contact can reuse an `integration_reference` previously held by an archived Contact.

Therefore HubSpot's unique custom property is **not treated as a permanent cross-archive idempotency guarantee**. It protects active records, but a missing active lookup can never prove that the reference was never created and later archived.

The Service Key approach is an explicitly accepted external beta dependency for this portfolio project. Current Service Key/API-version status must be re-checked against official HubSpot docs before live deployment. Do not silently migrate API versions.

## CRM retry defaults

Normal write schedule:

```text
initial write
retry 1 → 5 minutes
retry 2 → 30 minutes
retry 3 → 2 hours
then terminal escalation
```

Maximum normal automatic retries:

```text
3 retries after the initial write
```

`write_attempt_count` is an audit counter for every actual create HTTP call. A separate `automatic_retry_count` controls the three-retry ceiling so auth/configuration failures and deliberate human resume actions do not accidentally consume the transient retry budget.

Provider-directed delays such as `Retry-After` may override the next due time but do not remove the three-retry safety ceiling.

## Unknown-write recovery

A timeout, uncertain `5xx`, connection loss after transmission may have begun, malformed success response, or stale `SENDING` state may happen after HubSpot accepted a create.

Therefore:

```text
uncertain create
→ UNKNOWN
→ active GET by integration_reference
→ found → SUCCESS
→ active 404 → delayed confirmation reads
→ three completed active 404 results → UNKNOWN_ESCALATED
```

An `UNKNOWN` write is **never automatically resent** in v1. The archived-record discovery proved that an active 404 does not establish that the create never happened, because an earlier-created Contact could have been archived and its reference could then be reusable.

Delayed active reads are still useful to tolerate short visibility delays:

```text
1st check → about 15 seconds
2nd check → about 60 seconds later
3rd check → about 5 minutes later
```

Only completed active `404` outcomes count as not-found evidence. Failed/indeterminate reconciliation calls do not count.

Automatic retries remain available only for outcomes that are safe to retry: HubSpot-documented `423`, `429`, `477` responses, or an `httpx` failure that is provably pre-transmission (`ConnectError`, `ConnectTimeout`, or `PoolTimeout`). `WriteError`, `WriteTimeout`, `ReadError`, `ReadTimeout`, `RemoteProtocolError`, and other ambiguous transport failures are `UNKNOWN`.

## Clustered technical failure rule

Immediate cluster alert when:

```text
5 distinct normalized leads
within rolling 10 minutes
```

have qualifying technical CRM/API failures.

Qualifying:

- `429`,
- `423`,
- `477`,
- `5xx`,
- network/transport errors,
- `UNKNOWN`,
- unclassified technical provider responses.

Excluded:

- confirmed `EXISTING_CONTACT`,
- confidently classified per-record permanent validation errors,
- auth/config blockers because those alert immediately on the first occurrence.

Threshold/window are configurable; defaults are 5 / 10 minutes.

## Notifications

Use generic SMTP via environment variables.

For testing, Gmail SMTP + App Password may be used.

New terminal per-lead failures are consolidated approximately every 2 hours. Immediate auth/config and clustered-failure alerts are separate.

SMTP delivery is explicitly **at-least-once**. A rare duplicate email is acceptable if SMTP accepted a message but the worker crashed before local success was recorded. Do not build an exactly-once email subsystem.

## Deployment

Target:

```text
Railway project
├── FastAPI web service
├── ONE worker service replica
└── PostgreSQL
```

Codex prepares deployment-ready code/configuration/instructions. The human developer performs the actual Railway deployment manually.

## Explicit non-goals

- custom CRM,
- identity-resolution engine,
- fuzzy matching,
- duplicate merging,
- automatic upsert,
- CRM Case/queue engine,
- multi-tenant OAuth installation,
- frontend,
- distributed/multi-replica worker coordination,
- exactly-once SMTP,
- normalization reprocessing subsystem,
- pagination added only for practice,
- production PII retention/purge policy.

This portfolio project uses test/demo data.
