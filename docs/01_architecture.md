# 01 — Architecture

## System shape

```text
website  ─┐
linkedin ─┼─> POST /webhooks/{source} ─> PostgreSQL ─> ONE worker ─> HubSpotAdapter ─> HubSpot
partner  ─┘
```

FastAPI acknowledges only after the raw event is committed. Normalization and CRM delivery happen outside the request lifecycle.

## What "resolve source from route" means

The URL selects the provider contract:

```text
/webhooks/website  → website auth + website event-ID extractor + WebsitePayload + WebsiteAdapter
/webhooks/linkedin → bearer auth + LinkedIn event-ID extractor + LinkedInPayload + LinkedInAdapter
/webhooks/partner  → API-key auth + partner event-ID extractor + PartnerPayload + PartnerAdapter
```

The service reads the literal `{source}` value from the route, verifies it is exactly one supported source, and selects that provider's logic.

A JSON field named `source` must never override the route.

## Happy path

```text
1. POST /webhooks/{source}
2. Verify source is website/linkedin/partner.
3. Read raw body within the request-size limit.
4. Authenticate using that source's mechanism.
5. Parse JSON and validate only the minimal event envelope.
6. Extract exact stable event_id.
7. Insert webhook_events(RECEIVED).
8. PostgreSQL enforces UNIQUE(source, event_id).
9. Commit.
10. Return 202.
11. Worker claims one RECEIVED event.
12. Full provider Pydantic model validates.
13. Provider adapter produces NormalizedLead data.
14. In one transaction reserve normalized_leads.id, derive IR-xxxxx, insert normalized row, mark webhook event NORMALIZED.
15. Worker claims one due CRM delivery.
16. Persist SENDING + increment write attempt before HTTP I/O.
17. HubSpotAdapter sends CREATE_ONLY Contact.
18. Persist the confirmed result or a safe recovery state.
```

## One worker in v1

There is exactly one Railway worker replica.

The worker may perform several logical loops—normalization, CRM delivery/reconciliation, stale recovery, and notification checks—but only one worker process owns these duties in v1.

Outbound HubSpot writes are sequential.

Why:

- project traffic is modest,
- one worker is enough for thousands of leads/day,
- it removes unnecessary distributed races,
- PostgreSQL durability and restart recovery are still demonstrated,
- horizontal worker coordination can be added only when real throughput requires it.

`FOR UPDATE SKIP LOCKED` may still be used for safe row claiming, but v1 correctness must not depend on multiple worker replicas.

CRM-selection gate: before every CRM-work selection, if the worker sees any existing `SENDING` row, it starts no new CRM I/O. If that row is younger than the 15-minute lease, the worker waits while continuing non-CRM work. Once stale, it becomes `UNKNOWN` and is reconciled before later CRM work. This rule applies both after restart and after an unexpected item-level exception.

## Two durable state machines

### Webhook processing

```text
RECEIVED
→ PROCESSING
→ NORMALIZED | NORMALIZATION_FAILED
```

### CRM delivery

```text
PENDING
→ SENDING
→ SUCCESS
   EXISTING_CONTACT
   RETRY_PENDING
   UNKNOWN
   FAILED_ESCALATED
   UNKNOWN_ESCALATED
```

The exact transition matrix is authoritative in `02_database.md`.

## Crash-safety invariants

1. Raw event commits before `202`.
2. `UNIQUE(source,event_id)` is the race-safe webhook idempotency guarantee.
3. `UNIQUE(webhook_event_id)` prevents duplicate normalized rows.
4. Creating the normalized row and marking the webhook event `NORMALIZED` are one transaction.
5. A stale `PROCESSING` event is recovered after 15 minutes.
6. Before every HubSpot create, `SENDING`, `write_attempt_count`, and `last_attempt_at` are committed.
7. A stale `SENDING` row is treated as `UNKNOWN`, never blindly resent.
8. A reconciliation not-found check counts only after a completed active unique-property GET returns `404`.
9. `integration_reference` is permanently unique in PostgreSQL. HubSpot enforces it for active Contacts, but manual discovery proved archived Contacts can release/reuse the same value; therefore active lookup absence is never treated as proof that a Contact was never created.

## CREATE_ONLY conflict principle

A HubSpot conflict does not automatically mean `EXISTING_CONTACT`.

Because unknown writes are never automatically resent, a normal create `409` is resolved using active-record reads only:

```text
create 409
→ GET active Contact by integration_reference
   found + returned reference/email match this lead → unexpected reference collision → current lead PENDING + CONFIG pause
   found but returned reference/email does not match → current lead PENDING + CONFIG/data-integrity pause
   404 → if email exists, GET active Contact by email
       found → EXISTING_CONTACT
       404/indeterminate → conservative FAILED_ESCALATED
   indeterminate → conservative technical failure path
```

No conflict path may call upsert.

Archived unique-property lookup is not part of this flow. Manual testing proved HubSpot cannot retrieve archived Contacts by custom unique property and allows reuse of that value after archival.

## Durable global CRM pause

Use one singleton PostgreSQL runtime-state record for global CRM delivery state.

When a definite auth/configuration blocker is detected:

```text
current lead stays in a safe resumable state:
- create/auth failure → PENDING
- reconciliation/auth failure → UNKNOWN

runtime state → crm_delivery_paused=true
worker → stops starting new HubSpot create/reconciliation work
normalization → may continue
```

The current lead records the safe error category, but global availability is represented only by the singleton runtime-state row.

Because v1 has one sequential CRM worker, there is no multi-worker race between publishing the pause and another simultaneous create.

After a human fixes the problem, a non-public operator command clears the durable pause. No per-lead mass requeue is required; work resumes from its already-safe state.

## Scale and philosophy

Optimize v1 for:

```text
correctness
durability
traceability
recovery
understandability
```

not distributed-system complexity.
