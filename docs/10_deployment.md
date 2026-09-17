# 10 — Railway Deployment Preparation

## Goal

Deploy the FastAPI API and durable worker so accepted events survive restarts and downstream CRM processing continues independently from the webhook request lifecycle.

Railway is the target hosting platform for the first production-style deployment.

## Responsibility boundary

The project author owns the architecture, deployment design, configuration decisions, secrets, migration execution, production deployment, and live verification.

OpenAI Codex is used to implement deployment-ready application and infrastructure code from those defined requirements.

All generated changes remain subject to automated tests and human review before deployment.

No normal application process should run migrations automatically or call `metadata.create_all()` in production.

## Runtime components

Minimum deployment:

```text
Railway project
│
├── FastAPI web service
├── Worker service — exactly 1 replica
└── PostgreSQL
```

The web and worker may use the same application image/codebase but run different startup commands.

## Web process

Responsibilities:

- expose webhook endpoints,
- authenticate providers,
- validate the technical envelope,
- persist raw events,
- acknowledge only after database commit,
- expose health/readiness endpoints.

The web process must not wait for normalization or CRM delivery before acknowledging accepted webhook work.

## Worker process

Responsibilities:

- recover stale processing work,
- claim committed raw events,
- perform provider-specific normalization,
- persist normalized leads,
- perform durable HubSpot CREATE_ONLY delivery,
- schedule safe retries,
- reconcile uncertain CRM outcomes,
- process operational notification work.

### Worker replica invariant

Project #3 v1 is designed for **exactly one worker replica**.

The current implementation does not add a distributed singleton/advisory lock for multiple worker processes.

Deployment must therefore keep the worker replica count at `1`.

Scaling the worker horizontally would require an explicit architecture change and additional coordination guarantees.

## PostgreSQL

Production uses PostgreSQL as durable application state.

Requirements:

- durable storage,
- connectivity from both web and worker,
- UTC-aware timestamps,
- schema constraints defined by the project,
- migration-driven schema changes,
- backups appropriate to the deployment environment.

SQLite is not the deployment database for this project.

## Database migrations

Schema changes are managed by Alembic.

Deployment sequence:

```text
new application revision
        ↓
run Alembic migration once
        ↓
migration succeeds
        ↓
start/restart web + worker
```

The migration step must be a dedicated deployment/pre-deploy action or an explicitly executed one-shot command.

Do **not** let both web and worker independently run migrations during normal startup.

Do **not** rely on `metadata.create_all()` for production schema management.

A failed migration must block deployment of the new application revision.

## Configuration and secrets

Runtime configuration belongs in Railway environment variables / secret configuration.

Examples include:

```text
DATABASE_URL
HubSpot credential
website webhook secret
LinkedIn-like bearer token
partner API key
SMTP credentials
notification recipients
worker/retry settings
```

Rules:

- never commit real secret values,
- `.env` is local-only and ignored by Git,
- `.env.example` contains placeholders/documentation only,
- do not bake secrets into a Docker image,
- do not print secrets in CI/CD logs.

## Health and readiness

The web deployment should distinguish:

```text
/health → process is alive
/ready  → application configuration is valid and PostgreSQL is reachable
```

A HubSpot outage should not automatically make the HTTP process itself unhealthy.

Downstream CRM failures belong in durable state, logs, pause state, and operational notifications.

## Restart behavior

Restarts are expected operational events.

After restart:

- `RECEIVED` webhook events remain durable,
- stale `PROCESSING` work can be recovered,
- pending/retryable CRM work resumes,
- `UNKNOWN` CRM outcomes are reconciled before unsafe resend,
- terminal statuses remain terminal unless a human explicitly reprocesses them.

## Logging and observability

Log safe operational identifiers such as:

- internal row ID,
- source,
- event ID,
- integration reference,
- delivery status,
- attempt count,
- CRM result category,
- safe provider correlation ID when useful.

Never log credentials or sensitive authentication values.

## CI/CD requirement

The CI/CD pipeline should follow:

```text
push / pull request
      ↓
automated tests
      ↓
tests pass
      ↓
build Docker image
      ↓
deployment allowed
      ↓
run migration
      ↓
start/restart services
```

Default CI must not require live HubSpot or SMTP credentials.

Live external-integration tests remain separate/opt-in because they mutate external systems and depend on real secrets.

## Docker requirements

Project #3 should use one reusable application image.

The image must:

- install the application and runtime dependencies,
- exclude `.env`, local virtual environments, caches, and development artifacts,
- support both the web and worker process commands,
- avoid embedding runtime credentials.

Local Docker Compose should model:

```text
web
worker
postgres
```

with exactly one worker instance.

Migration execution should be explicit and must not race between web and worker.

## Deployment sequence

Recommended production sequence:

```text
1. Provision PostgreSQL.
2. Configure Railway environment variables/secrets.
3. Build the application image.
4. Run Alembic migration once.
5. Start the web service.
6. Start exactly one worker replica.
7. Verify /health.
8. Verify /ready.
9. Perform controlled live smoke tests.
```

## Live smoke tests

Deployment is not complete until live behavior is verified.

Minimum checks:

- `/health` responds successfully,
- `/ready` confirms PostgreSQL connectivity,
- a valid webhook is accepted and persisted,
- the worker normalizes the event,
- CRM delivery completes,
- database state reflects the final outcome,
- operational email can be delivered,
- restart/recovery behavior remains consistent with the documented state machine.

Do not use live secrets in screenshots, logs, tickets, or public documentation.

## Current status

Application implementation, automated tests, and the major manual integration/recovery scenarios are complete.

Remaining deployment work:

```text
Dockerize
→ Docker Compose local verification
→ GitHub Actions CI/CD
→ Railway configuration
→ migration/deploy
→ live smoke tests
```

Project #3 is complete only after those production-oriented steps succeed.
