# Agent Instructions — Project #3

> This file contains the implementation constraints supplied to OpenAI Codex. Architecture, business rules, reliability policy, and acceptance criteria are owned by the project author; Codex is used primarily for bounded implementation work.

## Purpose

This file controls AI/Codex work on Project #3. Read `README.md` first, then read only the focused docs relevant to the task.

## Human / AI responsibility boundary

The project author owns:

- business-process interpretation,
- architecture and system boundaries,
- source-of-truth decisions,
- state-machine and reliability policy,
- retry/idempotency requirements,
- acceptance criteria,
- review of critical code paths,
- manual integration verification,
- deployment/configuration decisions.

Codex may:

- implement already-agreed architecture,
- write and refactor application code,
- create automated tests,
- fix bounded implementation defects,
- prepare deployment/infrastructure code from explicit requirements.

Codex must not invent important business rules or silently redesign the system.

Generated implementation must be treated as code requiring automated tests and human review before it is considered accepted.

## Global rules

1. Do not invent business rules.
2. Do not expand the project into a CRM, identity-resolution engine, duplicate-merging engine, or workflow engine.
3. CRM business logic belongs to the CRM/business layer unless a document explicitly says otherwise.
4. The fictional company uses **`CREATE_ONLY`** Contact delivery. Do not introduce automatic upsert.
5. HubSpot `409 CONFLICT` for an existing email maps to **`EXISTING_CONTACT`**, not `FAILED`.
6. Do not retry `EXISTING_CONTACT` automatically.
7. Do not overwrite an existing Contact after `409`.
8. `integration_reference` is technical delivery identity, not customer identity.
9. Persist raw webhook events in PostgreSQL before acknowledging the provider.
10. PostgreSQL `UNIQUE(source, event_id)` is the final race-safe webhook-idempotency rule.
11. Do not use FastAPI `BackgroundTasks` as the only durability mechanism. Use the DB-backed worker design.
12. Do not introduce RabbitMQ, Kafka, Celery, SQS, or another queue unless the architecture is explicitly changed.
13. Do not introduce `crm_operations` in the initial version unless requirements change to multiple independent outbound operations.
14. Secrets must come from environment/configuration and must never be hard-coded, committed, or logged.
15. Do not introduce OAuth into the initial HubSpot implementation. The project uses a Service Key for one controlled account.
16. Do not introduce n8n as the foundation.
17. Do not add pagination merely as a learning exercise.
18. Do not modify unrelated files while implementing a focused ticket.
19. Run the relevant tests and the full test suite before reporting an implementation ticket complete.
20. Report changed files, tests run, results, assumptions, and any unresolved limitation.
21. Do not perform destructive production operations or expose live secrets.
22. The v1 runtime requires exactly one worker replica unless the architecture is explicitly redesigned for distributed worker coordination.

## Document router

| Area | Governing document |
|---|---|
| Overall scope / boundaries | `README.md`, `docs/00_project_contract.md`, `docs/01_architecture.md` |
| Database / constraints / statuses | `docs/02_database.md` |
| Authentication / secrets | `docs/03_authentication.md` |
| Webhook endpoint / idempotency | `docs/04_webhook_ingestion.md` |
| Provider mapping / models | `docs/05_normalization.md` |
| Worker / retries / recovery | `docs/06_worker_and_retries.md` |
| HubSpot / CRM behavior | `docs/07_crm_adapter.md` |
| Notifications | `docs/08_notifications.md` |
| Tests | `docs/09_testing.md` |
| Deployment / runtime | `docs/10_deployment.md` |

## Conflict rule

If two documents appear inconsistent:

1. Stop.
2. Identify the exact conflict.
3. Do not silently choose one interpretation.
4. Report the conflict for human review before implementing the affected behavior.

## Open-decision rule

If a document explicitly says an implementation detail is not yet frozen, do not invent it as a business requirement.

Either:

- implement behind a small configurable abstraction if the ticket explicitly requires it, or
- report the unresolved decision before proceeding.

## Review mode

When asked to review the project, specifically look for:

- contradictions between docs and implementation,
- state transitions that cannot terminate or recover,
- missing database constraints,
- retry paths that could duplicate external writes,
- CRM/business decisions leaking into the integration layer,
- secrets or sensitive values that could be logged,
- race conditions around idempotency or worker claiming,
- unsafe handling of 400/401/403/409/423/429/477/5xx/timeouts,
- gaps between manual HubSpot evidence and documented adapter behavior,
- deployment assumptions that violate the exactly-one-worker invariant,
- migrations being run by normal application startup,
- tests that prove only happy paths rather than recovery guarantees.
