# Project #3 — Multi-Source CRM Integration

> **Status:** application implementation and core validation are complete. Containerization, CI/CD, and production deployment are the remaining project-closure steps.

## Purpose

Project #3 is a production-minded CRM integration service that receives lead/contact submissions from multiple external sources, authenticates and validates inbound events, persists them durably, normalizes provider-specific payloads into one internal model, and delivers that data safely to HubSpot.

The project is intentionally focused on integration reliability rather than building a CRM. It exercises webhook ingestion, authentication, PostgreSQL persistence, normalization, idempotency, retry/recovery behavior, operational notifications, migrations, and production deployment practices.

## Development approach

This project uses an **AI-assisted engineering workflow**.

The project author owns the system architecture, business rules, integration boundaries, reliability decisions, acceptance criteria, review process, deployment decisions, and live verification.

OpenAI Codex is used primarily as the implementation agent: translating bounded specifications and engineering tickets into application code, automated tests, refactors, and narrowly scoped fixes.

After implementation, critical code paths are reviewed by the project author, the automated test suite is executed, and important production behaviors are verified manually against real integrations.

AI-generated implementation is therefore treated as code that requires review and verification, not as an authoritative source of architecture or business rules.

## High-level architecture

```text
External provider
      ↓
FastAPI web process
      ↓
authenticate + technical validation
      ↓
persist raw event in PostgreSQL
      ↓
commit
      ↓
return 202

---------------- asynchronous boundary ----------------

Worker process
      ↓
claim persisted event
      ↓
provider-specific normalization
      ↓
NormalizedLead validation
      ↓
persist normalized lead
      ↓
durable CRM delivery state machine
      ↓
HubSpot CREATE_ONLY delivery
      ↓
retry / reconciliation / escalation
      ↓
operational notifications
```

The web process and worker are separate runtime processes. They use the same codebase but perform different responsibilities.

## Runtime components

```text
1. FastAPI web process
2. One worker process
3. PostgreSQL
```

The v1 deployment invariant is **exactly one worker replica**.

## Responsibility boundary

### Source systems own

- their own required form fields and submission rules,
- whether a user is allowed to submit,
- source-side validation,
- stable source event/submission identifiers.

### Integration owns

- supported-source routing,
- provider authentication,
- technical request/schema validation,
- durable raw-event persistence before acknowledgement,
- webhook idempotency,
- source-specific normalization,
- normalized-model validation,
- durable CRM delivery state,
- safe retry and recovery behavior,
- unknown-write reconciliation,
- logging, auditability, and technical/Ops notifications.

### CRM / business process owns

- customer/person identity resolution,
- duplicate/profile matching,
- merge rules,
- CRM data-quality decisions,
- business statuses,
- assignment, queues, workflows, reports, and business escalation.

## Inbound sources

The project deliberately uses different inbound contracts and authentication styles:

- Website — HMAC-SHA256 with timestamp/replay protection
- LinkedIn-like provider — Bearer token
- Partner provider — API key

Raw accepted events are committed to PostgreSQL before the provider is acknowledged.

The database constraint:

```text
UNIQUE(source, event_id)
```

is the final race-safe webhook idempotency rule.

## Normalization

Different provider payloads are mapped into one `NormalizedLead` model.

Normalization is deterministic and technical. It may map names, trim strings, parse timestamps, normalize optional fields, and preserve provider-specific metadata.

It does not perform customer identity resolution or invent missing business data.

## CRM delivery policy

The fictional company used in this project has a deliberate **CREATE_ONLY** HubSpot Contact policy.

For this project:

- new normalized leads are sent through the HubSpot Contact create endpoint,
- an existing-email conflict is recorded as `EXISTING_CONTACT`,
- the integration does not automatically upsert,
- the integration does not overwrite the existing Contact,
- the normalized lead remains in PostgreSQL for traceability.

This is a project-specific business policy, not a universal CRM integration rule.

## Durable delivery and unknown-write safety

Each normalized lead receives an immutable technical delivery reference such as:

```text
IR-00001
```

The same `integration_reference` is sent to HubSpot.

Before a CRM create call, the worker persists `SENDING`. If a write outcome is uncertain, the lead becomes `UNKNOWN`.

An `UNKNOWN` result is **never blindly recreated**. The worker performs a read/reconciliation by `integration_reference`.

```text
CREATE begins
    ↓
SENDING committed
    ↓
uncertain provider outcome
    ↓
UNKNOWN
    ↓
read/reconcile by integration_reference
    ↓
found + expected data → SUCCESS
not safely resolved   → continue reconciliation / escalate
```

This prevents an uncertain network result from automatically creating a duplicate CRM record.

## Global CRM pause

Authentication or configuration failures can activate a global CRM-delivery pause.

Inbound webhook ingestion and normalization can continue, but outbound CRM creation is blocked until the underlying problem is corrected and an operator explicitly clears the pause.

This separates intake durability from downstream-provider availability.

## Database and migrations

PostgreSQL is the durable state store for the web process and worker.

Schema evolution is handled through **Alembic migrations** rather than runtime `create_all()` behavior.

Deployment must apply the required migration before the new web and worker processes begin handling production traffic.

## Notifications

Operational notifications are sent through SMTP.

The notification layer covers cases such as:

- global CRM authentication/configuration failure,
- escalated CRM delivery failures,
- normalization failures,
- clustered technical failures.

Notification delivery is intentionally at-least-once; a process crash after SMTP succeeds but before the database state is updated can result in a duplicate notification.

## Testing and verification

The automated test suite covers unit, API, PostgreSQL integration, CRM-state-machine, recovery, notification, and failure-path behavior.

At the end of the implementation/hardening phase, the local suite reported:

```text
424 passed
```

Important behaviors were also verified manually against real HubSpot and SMTP integrations, including:

- successful end-to-end Contact creation,
- duplicate webhook idempotency,
- SMTP notification delivery,
- authentication-failure pause and recovery,
- uncertain CRM write reconciliation without a second create,
- stale worker-processing recovery after simulated crash,
- real existing-email conflict handling without upsert or overwrite.

Live external checks are intentionally separate from the default automated suite because they require real credentials and mutate external systems.

## Security

- real secrets belong only in environment/runtime secret stores,
- `.env` is not committed,
- `.env.example` contains placeholders only,
- authentication credentials must not be logged,
- raw webhook persistence excludes authentication headers/secrets,
- external diagnostics are sanitized before persistence/logging.

## Current project-closure work

The application and core reliability behavior are implemented and tested.

Remaining production-oriented work:

```text
Docker
→ Docker Compose
→ CI/CD
→ deployment
→ live deployment smoke tests
```

## Documentation map

- `agent.md` — implementation constraints supplied to Codex and documentation router.
- `docs/00_project_contract.md` — project contract and locked decisions.
- `docs/01_architecture.md` — system boundaries and end-to-end architecture.
- `docs/02_database.md` — persistence model, constraints, statuses, and recovery data.
- `docs/03_authentication.md` — inbound authentication and secret-handling rules.
- `docs/04_webhook_ingestion.md` — intake, acknowledgement, idempotency, and raw persistence.
- `docs/05_normalization.md` — provider models, adapters, and `NormalizedLead`.
- `docs/06_worker_and_retries.md` — worker state machine, retries, recovery, and reconciliation.
- `docs/07_crm_adapter.md` — CRM adapter contract and HubSpot-specific behavior.
- `docs/08_notifications.md` — operational notification behavior.
- `docs/09_testing.md` — automated and manual verification strategy.
- `docs/10_deployment.md` — deployment architecture, migrations, CI/CD, and operational requirements.

## Known operational constraints

- v1 requires exactly one worker replica,
- SMTP notification delivery is at-least-once,
- terminal CRM states require explicit human intervention to reprocess,
- HubSpot-specific behavior observed manually is kept distinct from general integration guarantees,
- production deployment is not considered complete until Docker, CI/CD, deployment, and live smoke testing are finished.
