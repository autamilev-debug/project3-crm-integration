# 06 — Worker, Retries, Reconciliation, and Crash Recovery

## v1 worker model

Railway runs exactly **one worker replica**.

The worker processes one CRM delivery/reconciliation at a time. FastAPI still accepts and persists webhook requests independently while the worker is busy.

This deliberately avoids distributed-worker coordination while still teaching durable DB-backed work, state transitions, retries, external side-effect safety, restart recovery, and scheduled operational work.

## Worker responsibilities

At startup and then approximately every configured five-minute interval, the
worker first runs one bounded periodic notification check. It then performs the
core cycle in this order:

1. stale-state recovery,
2. normalize one due `RECEIVED` webhook event,
3. if CRM is not globally paused, process one due CRM delivery/reconciliation,
4. sleep briefly when no useful work is due.

Running a due periodic check before the core cycle prevents a newly activated
pause whose immediate alert fails from being retried again seconds later in the
same loop. A newly committed AUTH/CONFIG pause still triggers its immediate alert
directly after the CRM result is persisted.

An unexpected exception around one item must be caught/logged safely so the top-level loop continues after a short backoff.

## Unresolved SENDING gate

Before **every** CRM-work selection, check for existing `SENDING` rows. This applies after startup and after any caught item-level exception.

- if no `SENDING` row exists, CRM work may continue normally;
- if a `SENDING` row exists but is younger than the 15-minute lease, start **no new CRM I/O**; normalization and notification work may continue;
- once it reaches the 15-minute stale lease, convert it to `UNKNOWN` and make reconciliation due immediately;
- reconcile that unresolved prior write before later CRM creates.

This intentionally uses the existing lease instead of adding distributed locks/advisory-lock machinery in v1.

## Normalization claim

Claim one `RECEIVED` event transactionally:

```text
RECEIVED → PROCESSING
processing_started_at=now
commit
```

Then validate/map outside the lock.

Success transaction:

```text
reserve normalized lead ID
derive integration_reference
insert normalized_leads(PENDING)
PROCESSING → NORMALIZED
commit
```

Failure:

```text
PROCESSING → NORMALIZATION_FAILED
```

No automatic normalization retry.

## Stale normalization lease

Default lease:

```text
15 minutes
```

If `PROCESSING` is stale:

```text
normalized row exists → repair parent to NORMALIZED
no normalized row      → reset parent to RECEIVED
```

## CRM create attempt

Before HTTP create:

```text
PENDING/RETRY_PENDING
→ SENDING
write_attempt_count += 1
last_attempt_at=now
commit
```

This happens **before** HubSpot I/O so a crash does not erase evidence that an external write may have been attempted.

## Safe automatic retry schedule

Normal retry budget:

```text
initial write
retry 1 → after 5 minutes
retry 2 → after 30 minutes
retry 3 → after 2 hours
then FAILED_ESCALATED if another retry would be required
```

`write_attempt_count` increments before every actual create HTTP call. `automatic_retry_count` controls the three-retry ceiling.

Use `httpx` for HubSpot HTTP calls.

Automatic retry is allowed only when the previous outcome establishes that repeating the create is safe:

- `423` lock response,
- `429` rate-limit response,
- `477` migration response,
- `httpx.ConnectError`,
- `httpx.ConnectTimeout`,
- `httpx.PoolTimeout`.

The three `httpx` exceptions above are the v1 allowlist for failures treated as pre-transmission/safe-to-repeat.

Treat `httpx.WriteError`, `httpx.WriteTimeout`, `httpx.ReadError`, `httpx.ReadTimeout`, `httpx.RemoteProtocolError`, and any transport exception whose transmission state is unclear as `UNKNOWN`.

For provider-directed responses, respect `Retry-After`/documented minimum delay.

`423` and `477` retryability are accepted HubSpot/provider assumptions based on HubSpot's documented general error guidance. Re-check if live Contact-create behavior ever contradicts them.

## What becomes UNKNOWN

Use `UNKNOWN` when HubSpot may already have accepted the create but the local process cannot prove the outcome, including:

- read timeout after transmission may have begun,
- connection reset/loss after transmission may have begun,
- uncertain `5xx`,
- valid-looking success status with missing/malformed required response data,
- stale `SENDING` after worker crash.

Never automatically resend an `UNKNOWN` create.

## Active unique-property reconciliation

Recovery uses a single-record active lookup:

```text
GET /crm/objects/2026-03/contacts/{integration_reference}
?idProperty=integration_reference
```

Do not use CRM Search for unknown-write recovery.

Manual Postman discovery on 2026-09-02 proved:

```text
active existing reference → 200 + Contact
active nonexistent reference → 404
```

The same discovery also proved:

```text
archived=true + idProperty=integration_reference
→ 400 VALIDATION_ERROR

new active Contact may reuse an integration_reference
previously held by an archived Contact
→ 201 Created
```

Therefore an active `404` is **not proof that the create never happened**. The Contact may have been created and later archived, and the archived reference may be reusable.

This is why v1 never authorizes an automatic resend from `UNKNOWN`.

## Reconciliation evidence counters

Use:

```text
reconciliation_not_found_count
reconciliation_error_count
```

`reconciliation_not_found_count` increments **only after** a completed active unique-reference GET returns `404`.

A crash before/during the read, HTTP failure, malformed response, or ambiguous result does not increment it.

Because v1 has one worker replica, a distributed reconciliation claim/lease is unnecessary.

## Reconciliation schedule

When an uncertain write first becomes `UNKNOWN`:

```text
reconciliation_not_found_count=0
reconciliation_error_count=0
next_reconciliation_at=now+15s
```

If the active read finds the Contact, validate it before declaring success:

```text
returned integration_reference == requested integration_reference
AND returned email == normalized lead email
```

Email comparison uses the normalized trimmed/lowercased value.

If both match:

```text
→ SUCCESS
store HubSpot Contact ID
```

If either mismatches:

```text
→ remain UNKNOWN
→ global CONFIG/data-integrity pause
→ never mark SUCCESS
```

If the active read returns `404`:

```text
1st 404 → not_found_count=1 → next check +60s
2nd 404 → not_found_count=2 → next check +300s
3rd 404 → UNKNOWN_ESCALATED
```

After the third `404`, do **not** retry the create automatically. Escalate for human review because the archived-record limitation means nonexistence cannot be proven safely.

A completed active found/404 read resets `reconciliation_error_count=0`.

## Reconciliation request failures

A reconciliation failure must never authorize a resend.

Classification:

```text
401 → keep lead UNKNOWN + global AUTH pause
403 → keep lead UNKNOWN + global CONFIG pause
400 on frozen unique-property read → keep lead UNKNOWN + global CONFIG pause
404 → valid active not-found evidence
405 on frozen endpoint → keep lead UNKNOWN + global CONFIG pause
423/429/477 → remain UNKNOWN; not-found count unchanged; reconciliation_error_count += 1; use provider delay plus reconciliation backoff
5xx / timeout / transport / malformed or ambiguous result
    → remain UNKNOWN; not-found count unchanged
    → reconciliation_error_count += 1
```

Indeterminate-reconciliation backoff:

```text
error 1 → +5 minutes
error 2 → +30 minutes
error 3 → UNKNOWN_ESCALATED
```

A completed active found/404 read resets the error counter.

## 409 conflict handling

Because an `UNKNOWN` write is never automatically resent, a normal create reaching `409` is not a retry of an uncertain create.

Resolve without parsing English error prose:

```text
1. GET active Contact by integration_reference
   200 + returned reference/email match this lead → current lead PENDING + global CONFIG pause (unexpected active reference collision)
   200 but returned reference/email mismatch → current lead PENDING + global CONFIG/data-integrity pause
   401 → current lead PENDING + global AUTH pause
   400/403/405 → current lead PENDING + global CONFIG pause
   404 → continue
   indeterminate → FAILED_ESCALATED technical/unclassified

2. if normalized lead has email, GET active Contact by email
   200 + valid Contact ID + returned normalized email exactly matches requested normalized email
       → EXISTING_CONTACT; store existing HubSpot Contact ID
   200 but Contact ID/email is missing or email mismatches
       → current lead PENDING + global CONFIG/data-integrity pause
   401 → current lead PENDING + global AUTH pause
   400/403/405 → current lead PENDING + global CONFIG pause
   404 → FAILED_ESCALATED unclassified conflict
   indeterminate → FAILED_ESCALATED technical/unclassified

3. if no email exists
   → FAILED_ESCALATED unclassified conflict
```

No conflict path calls upsert.

## 400 create handling

For a create `400`, perform an active read by `integration_reference` and branch only on the follow-up result:

```text
FOUND + expected reference/email match
    → current lead PENDING + global CONFIG pause (unexpected active reference collision)

FOUND but reference/email mismatch
    → current lead PENDING + global CONFIG/data-integrity pause

ACTIVE_NOT_FOUND (404)
    → original 400 is definitive for this create
    → FAILED_ESCALATED
    → if structured fields confidently identify an ordinary per-record validation problem, exclude it from clustered technical-failure counting
    → otherwise classify technical/unrecognized

AUTH/CONFIG failure on follow-up read
    → current lead PENDING + corresponding global pause

indeterminate follow-up read
    → FAILED_ESCALATED technical/unclassified
    → no automatic resend
```

Do not parse arbitrary English message text to make business decisions.

## Other HubSpot outcomes

```text
2xx + valid Contact ID → SUCCESS
unexpected/malformed 2xx after create → UNKNOWN
401 → global AUTH pause; blocked create returns to PENDING
403 → global CONFIG pause; blocked create returns to PENDING
404/405 on frozen create endpoint → global CONFIG pause; blocked create returns to PENDING
423 → RETRY_PENDING
429 → RETRY_PENDING
477 → RETRY_PENDING
known pre-transmission connect failure → RETRY_PENDING
5xx / post-transmission transport uncertainty → UNKNOWN
unrecognized 4xx → FAILED_ESCALATED + technical/unclassified classification
```

Unrecognized responses are conservative: never assume a write is safe to repeat merely because the status was unexpected.

## Global pause

Before starting any HubSpot create/reconciliation work, check `integration_runtime_state.crm_delivery_paused`.

If true:

- do no CRM I/O,
- continue normalization,
- continue notification scheduling.

On a definite auth/config failure, update the current lead and singleton pause row in one transaction.

## Human recovery

After fixing the Service Key/scope/configuration, the human runs the non-public operator command defined in `02_database.md` to clear the singleton pause.

Leads remain in safe resumable states, so no mass requeue is required.

No public admin endpoint is required.

## Cluster-failure timestamp

When a lead experiences a qualifying technical CRM/API failure:

```text
last_technical_failure_at=now
```

A lead is eligible for a new cluster episode when:

```text
cluster_alerted_at IS NULL
OR last_technical_failure_at > cluster_alerted_at
```

Before SMTP, freeze the episode timestamp in `cluster_alert_pending_at`.

After a successful cluster email, set `cluster_alerted_at` to that frozen `cluster_alert_pending_at` value for the leads included in the alert, then clear `cluster_alert_pending_at`.

Do **not** set `cluster_alerted_at` to the later SMTP completion time; a new technical failure that occurs while the email is being sent must remain eligible for a later episode.
