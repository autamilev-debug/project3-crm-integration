# 02 — PostgreSQL Schema and State Transitions

## Persistence rules

Use PostgreSQL with SQLAlchemy 2.x, psycopg 3, and Alembic.

Use `VARCHAR` + PostgreSQL `CHECK` constraints for source/status values so the allowed values remain visible in SQL and straightforward to migrate.

Use UTC `TIMESTAMPTZ`.

## Table: webhook_events

Required fields:

| Field | Requirement |
|---|---|
| `id` | BIGINT primary key |
| `source` | VARCHAR(32), non-null |
| `event_id` | VARCHAR(128), non-null |
| `raw_payload` | JSONB, non-null |
| `event_status` | VARCHAR(32), non-null |
| `received_at` | TIMESTAMPTZ, non-null |
| `processing_started_at` | nullable |
| `normalized_at` | nullable |
| `completed_at` | nullable; stable terminal normalization timestamp |
| `last_error_category` | VARCHAR(64), nullable |
| `last_error` | VARCHAR(1000), nullable, sanitized |
| `escalation_notified_at` | nullable |
| `created_at` | non-null |
| `updated_at` | non-null |

Constraints:

```text
source IN ('website','linkedin','partner')
event_status IN ('RECEIVED','PROCESSING','NORMALIZED','NORMALIZATION_FAILED')
1 <= length(event_id) <= 128
UNIQUE(source,event_id)
```

`raw_payload` contains authenticated parsed JSON data, never authentication headers/secrets.

## Webhook-event transitions

| From | Trigger | To | Required mutation |
|---|---|---|---|
| new | accepted webhook persisted | `RECEIVED` | set `received_at` |
| `RECEIVED` | worker claims | `PROCESSING` | set `processing_started_at=now` |
| `PROCESSING` | normalization succeeds | `NORMALIZED` | insert one normalized row in same transaction; set `normalized_at=now`; set `completed_at=now`; clear event error |
| `PROCESSING` | deterministic normalization failure | `NORMALIZATION_FAILED` | set `completed_at=now`; store sanitized error; stop automatic processing |
| stale `PROCESSING` | no normalized row exists after 15 min | `RECEIVED` | clear `processing_started_at`; set `completed_at=NULL`; retry normalization |
| stale `PROCESSING` | normalized row already exists | `NORMALIZED` | set `normalized_at=now`; set `completed_at=now`; repair parent state; do not create another normalized row |

## Table: normalized_leads

Required fields:

| Field | Requirement |
|---|---|
| `id` | BIGINT primary key |
| `webhook_event_id` | non-null FK |
| `integration_reference` | VARCHAR(32), non-null, immutable, unique |
| `source` | VARCHAR(32), non-null |
| `source_event_id` | VARCHAR(128), non-null |
| `submitted_at` | nullable TIMESTAMPTZ |
| `first_name` | VARCHAR(100), nullable |
| `last_name` | VARCHAR(100), nullable |
| `email` | VARCHAR(254), nullable |
| `phone` | VARCHAR(64), nullable |
| `company` | VARCHAR(200), nullable |
| `job_title` | VARCHAR(150), nullable |
| `country` | VARCHAR(100), nullable |
| `city` | VARCHAR(100), nullable |
| `campaign` | VARCHAR(200), nullable |
| `lead_source` | VARCHAR(64), nullable |
| `source_metadata` | JSONB non-null default `{}` |
| `delivery_status` | VARCHAR(32), non-null |
| `write_attempt_count` | integer non-null default 0; audit count for actual create calls |
| `automatic_retry_count` | integer non-null default 0; max 3 |
| `next_retry_at` | nullable |
| `last_attempt_at` | nullable |
| `first_failure_at` | nullable |
| `last_failure_at` | nullable |
| `reconciliation_not_found_count` | integer non-null default 0 |
| `reconciliation_error_count` | integer non-null default 0 |
| `next_reconciliation_at` | nullable |
| `last_error_category` | VARCHAR(64), nullable |
| `last_error_code` | VARCHAR(128), nullable |
| `last_error` | VARCHAR(1000), nullable, sanitized |
| `crm_record_id` | VARCHAR(64), nullable |
| `crm_correlation_id` | VARCHAR(128), nullable |
| `sent_at` | nullable |
| `failure_at` | nullable |
| `escalation_notified_at` | nullable |
| `last_technical_failure_at` | nullable |
| `cluster_alert_pending_at` | nullable |
| `cluster_alerted_at` | nullable |
| `created_at` | non-null |
| `updated_at` | non-null |

Constraints:

```text
UNIQUE(webhook_event_id)
UNIQUE(integration_reference)
FK webhook_event_id → webhook_events.id ON DELETE RESTRICT

source IN ('website','linkedin','partner')

delivery_status IN (
  'PENDING','SENDING','RETRY_PENDING','UNKNOWN',
  'SUCCESS','EXISTING_CONTACT',
  'FAILED_ESCALATED','UNKNOWN_ESCALATED'
)

write_attempt_count >= 0
automatic_retry_count BETWEEN 0 AND 3
reconciliation_not_found_count >= 0
reconciliation_error_count >= 0

integration_reference ~ '^IR-[0-9]{5,}$'
```

Status-dependent invariants:

```text
SUCCESS          → crm_record_id IS NOT NULL AND sent_at IS NOT NULL
EXISTING_CONTACT → crm_record_id IS NOT NULL
RETRY_PENDING    → next_retry_at IS NOT NULL
UNKNOWN          → next_reconciliation_at IS NOT NULL
FAILED_ESCALATED / UNKNOWN_ESCALATED
                 → failure_at IS NOT NULL
```

## Integration-reference generation

Use an explicit PostgreSQL sequence for `normalized_leads.id`.

Normalization transaction:

```text
1. obtain next sequence value for normalized_leads.id
2. format IR-{id:05d}
3. insert normalized_leads with explicit id + integration_reference
4. mark parent webhook event NORMALIZED
5. commit once
```

Sequence gaps after rollback are acceptable.

## Table: integration_runtime_state

Exactly one singleton row (`id=1`).

The initial Alembic migration must create this table and idempotently seed row `id=1` with `crm_delivery_paused=false`.

Required fields:

| Field | Requirement |
|---|---|
| `id` | SMALLINT PK, constrained to 1 |
| `crm_delivery_paused` | boolean non-null default false |
| `pause_reason` | nullable: `AUTH` or `CONFIG` |
| `paused_at` | nullable |
| `pause_alert_sent_at` | nullable |
| `pause_alert_last_error` | VARCHAR(1000), nullable |
| `last_crm_batch_attempt_at` | nullable |
| `last_crm_batch_success_at` | nullable |
| `last_crm_batch_error` | VARCHAR(1000), nullable |
| `last_normalization_batch_attempt_at` | nullable |
| `last_normalization_batch_success_at` | nullable |
| `last_normalization_batch_error` | VARCHAR(1000), nullable |
| `updated_at` | non-null |

Constraints:

```text
id = 1

crm_delivery_paused=false
→ pause_reason IS NULL AND paused_at IS NULL

crm_delivery_paused=true
→ pause_reason IS NOT NULL AND paused_at IS NOT NULL
```

This row is the durable global CRM circuit-breaker. Do not infer global runtime availability by scanning lead rows.

When entering a **new** pause episode, set:

```text
crm_delivery_paused=true
pause_reason=<AUTH|CONFIG>
paused_at=now
pause_alert_sent_at=NULL
pause_alert_last_error=NULL
```

## Authoritative CRM delivery transitions

| From | Trigger/result | To | Required field behavior |
|---|---|---|---|
| `PENDING` | worker begins create | `SENDING` | `write_attempt_count += 1`; `last_attempt_at=now`; clear `next_retry_at` |
| `RETRY_PENDING` | due + pause false | `SENDING` | `write_attempt_count += 1`; set `last_attempt_at=now`; clear retry timestamp |
| stale `SENDING` | 15 min lease exceeded | `UNKNOWN` | zero reconciliation counters; reconciliation due immediately |
| `SENDING` | confirmed create with CRM ID | `SUCCESS` | set `crm_record_id`, `sent_at`; clear retry/reconciliation/current-error fields |
| `SENDING` | confirmed pre-existing active email Contact after `409` | `EXISTING_CONTACT` | set existing `crm_record_id`; clear retry/reconciliation/current-error fields |
| `SENDING` | reference collision or found-record identity mismatch during `400`/`409` classification | `PENDING` | keep retry budget unchanged; store collision/data-integrity error; transactionally set global CONFIG pause |
| `SENDING` | safe retryable outcome + retry budget available | `RETRY_PENDING` | increment `automatic_retry_count`; set `next_retry_at`; store safe error |
| `SENDING` | safe retryable outcome + `automatic_retry_count=3` | `FAILED_ESCALATED` | set `failure_at`; clear schedules |
| `SENDING` | uncertain outcome | `UNKNOWN` | zero reconciliation counters; first active lookup +15 sec |
| `SENDING` | deterministic/permanent failure | `FAILED_ESCALATED` | set `failure_at`; clear schedules |
| `SENDING` | definite auth/config blocker from create | `PENDING` | store safe error; keep retry budget unchanged; transactionally set global pause |
| `UNKNOWN` | active unique-reference GET finds Contact and returned reference + expected email match | `SUCCESS` | store CRM ID, `sent_at`; clear reconciliation/current-error fields |
| `UNKNOWN` | active unique-reference GET finds Contact but returned reference/email mismatch | `UNKNOWN` | preserve uncertainty; store data-integrity error; transactionally set global CONFIG pause |
| `UNKNOWN` | active unique-reference GET returns 404, count becomes 1 | `UNKNOWN` | not-found count=1; next check +60 sec |
| `UNKNOWN` | active unique-reference GET returns 404, count becomes 2 | `UNKNOWN` | not-found count=2; next check +300 sec |
| `UNKNOWN` | active unique-reference GET returns 404, count becomes 3 | `UNKNOWN_ESCALATED` | set `failure_at`; clear reconciliation scheduling; **do not resend** |
| `UNKNOWN` | reconciliation call indeterminate | `UNKNOWN` | not-found count unchanged; increment reconciliation error count; 5m/30m schedule |
| `UNKNOWN` | third consecutive indeterminate reconciliation failure | `UNKNOWN_ESCALATED` | set `failure_at`; clear reconciliation scheduling |
| `UNKNOWN` | definite auth/config blocker during read | `UNKNOWN` | preserve evidence counters; transactionally set global pause; resume read after human clears pause |

A completed active found/404 read resets `reconciliation_error_count=0`.

### Failure/error field rules

For any non-business failure/retry/UNKNOWN outcome:

```text
first_failure_at = now only if currently NULL
last_failure_at = now
last_error_category / last_error_code / last_error = current sanitized diagnostic
```

`failure_at` is set only when entering `FAILED_ESCALATED` or `UNKNOWN_ESCALATED`.

On `SUCCESS` or `EXISTING_CONTACT`, clear current error fields and due/scheduling fields, but retain `first_failure_at` / `last_failure_at` as audit history if earlier technical failures occurred.

`last_technical_failure_at` is updated only for failures that qualify for clustered technical alerts.

`UNKNOWN` never transitions automatically to `RETRY_PENDING` in v1. This is deliberate duplicate-prevention behavior based on the observed HubSpot archived-record limitation.

## Retry schedule and budget

`automatic_retry_count` is the retry budget, not the HTTP audit count.

```text
0 → initial write has happened; first retry may be scheduled
1 → retry 1 scheduled/used
2 → retry 2 scheduled/used
3 → retry 3 scheduled/used; no fourth retry
```

Delays when incrementing the retry counter:

```text
to 1 → 5 minutes
to 2 → 30 minutes
to 3 → 2 hours
```

Provider `Retry-After` may make the next due time later.

## Retry scheduling

Retry delays apply only to outcomes that are safe to resend, such as `423`, `429`, `477`, or a transport failure that is known to have occurred before request transmission.

```text
next_retry_at = max(now + configured_delay, provider Retry-After when applicable)
```

An `UNKNOWN` write never uses this retry path.

## Human auth/config recovery

After fixing the Service Key, scopes, endpoint/configuration **or resolving a reference/data-integrity collision**, the human runs a non-public operator command. Do not clear a CONFIG pause caused by a collision until the underlying collision/configuration problem has been checked.

The command:

```text
1. lock integration_runtime_state(id=1)
2. verify crm_delivery_paused=true
3. print the current pause reason
4. set crm_delivery_paused=false
5. set pause_reason=NULL
6. set paused_at=NULL
7. clear pause-alert fields
8. commit
```

It does **not** mass-rewrite lead statuses.

Leads were deliberately left in resumable states (`PENDING` for a blocked create or `UNKNOWN` for a blocked read), so the worker resumes safely after the pause is cleared.

No public admin HTTP endpoint is required.

## Why no crm_operations table

There is one outbound CRM mutation in v1: Contact create. Delivery state belongs on `normalized_leads`.
