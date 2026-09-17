# 09 — Testing

## Philosophy

Test the reliability boundaries, not only happy-path CRUD.

Default automated tests use mocked HubSpot/SMTP and a test PostgreSQL database. Live external tests are opt-in/manual.

## Webhook ingestion tests

Cover:

- supported route source selects the correct authenticator/model/adapter,
- unsupported source → 404,
- body >256 KiB → 413,
- source JSON field cannot override route source,
- website valid HMAC,
- website bad signature,
- website expired timestamp,
- linkedin valid/invalid bearer and configured comparison path uses constant-time comparison,
- partner valid/invalid API key and configured comparison path uses constant-time comparison,
- invalid JSON → 400,
- event ID missing,
- event ID number instead of string rejected,
- event ID leading/trailing whitespace rejected,
- event ID >128 rejected,
- exact case-sensitive `(source,event_id)` uniqueness,
- same event delivered twice → one DB row + both responses 202,
- persistence failure → non-2xx/503,
- no full normalization before 202.

## Normalization tests

One fixture per frozen source verifies:

```text
source event ID
submitted_at
common contact fields
campaign/lead_source
source_metadata
```

Also test:

- unknown provider fields do not break the model and remain only in raw payload,
- core scalar numbers/booleans are not silently coerced to strings,
- mapped strings trim surrounding whitespace,
- whitespace-only required strings fail,
- optional whitespace-only strings become `None`,
- email is normalized to trimmed lowercase,
- source-valid payloads may omit both optional contact fields,
- naive timestamps without timezone are rejected,
- timezone-aware timestamps normalize to UTC,
- over-length non-contact mapped fields fail normalization,
- an over-length email or phone invalidates only that contact method; normalization
  may still succeed when the other contact method is valid, while a supplied invalid
  contact with no other valid contact fails technical validation,
- validation diagnostics omit rejected values/PII,
- `NORMALIZATION_FAILED` preserves raw event/error and is not automatically retried,
- one webhook event cannot create two normalized rows.

## Integration-reference tests

Verify:

```text
id=1      → IR-00001
id=57     → IR-00057
id=99999  → IR-99999
id=100000 → IR-100000
```

Verify ID reservation + normalized insert + parent NORMALIZED update are one transaction.

## Database constraint tests

Cover:

- unsupported source rejected by DB constraint,
- unsupported statuses rejected,
- negative counters rejected,
- invalid integration-reference format rejected,
- duplicate `(source,event_id)` rejected,
- duplicate `webhook_event_id` rejected,
- duplicate `integration_reference` rejected,
- `SUCCESS` without CRM record ID rejected,
- `EXISTING_CONTACT` without CRM record ID rejected,
- `RETRY_PENDING` without due time rejected,
- `UNKNOWN` without `next_reconciliation_at` rejected.

## CRM adapter tests

Mocked create behavior:

```text
2xx + valid Contact ID → SUCCESS
malformed/unexpected 2xx → UNKNOWN
401 → AUTH_FAILURE
403 → CONFIG_FAILURE
404/405 endpoint failure → CONFIG_FAILURE
423 → retryable
429 → rate limited
477 → retryable using provider delay
known pre-transmission connect failure → retryable
5xx/post-transmission timeout or transport uncertainty → UNKNOWN
409 → conflict lookup flow; never upsert
400 → safe classification flow
unrecognized 4xx → conservative terminal technical/unclassified outcome
```

Mocked active-read behavior:

```text
200 by integration_reference → FOUND
404 by integration_reference → ACTIVE_NOT_FOUND
200 by email → FOUND
404 by email → ACTIVE_NOT_FOUND
401 → AUTH_FAILURE
400/403/405 → CONFIG_FAILURE
423/429/477 → retryable/indeterminate read
5xx/timeout/malformed → INDETERMINATE_READ
```

Do not implement archived unique-property lookup as a recovery mechanism. Manual discovery proved it returns `400` and archived references can be reused.

Only a completed active `404` may increment `reconciliation_not_found_count`, and that count never authorizes an automatic create retry.

## CREATE_ONLY tests

Required:

1. normal create → SUCCESS.
2. no path calls upsert.
3. first-attempt 409 + active integration-reference found with expected reference/email → lead PENDING + CONFIG pause.
4. first-attempt 409 + active integration-reference found but reference/email mismatch → lead PENDING + CONFIG/data-integrity pause.
5. 409 + reference follow-up `401` → lead PENDING + AUTH pause.
6. 409 + reference follow-up `400/403/405` → lead PENDING + CONFIG pause.
7. 409 + reference active-404 + email found with valid Contact ID + exact normalized-email match → EXISTING_CONTACT.
8. 409 + reference active-404 + email found but ID/email missing or email mismatches → lead PENDING + CONFIG/data-integrity pause.
9. 409 + reference active-404 + email follow-up `401` → lead PENDING + AUTH pause.
10. 409 + reference active-404 + email follow-up `400/403/405` → lead PENDING + CONFIG pause.
11. 409 + reference active-404 + email active-404 → FAILED_ESCALATED unclassified conflict; no retry/upsert.
12. 409 with no email and no reference collision → FAILED_ESCALATED unclassified conflict.
13. 400 + active integration-reference found with expected reference/email → lead PENDING + CONFIG pause.
14. 400 + active integration-reference found but reference/email mismatch → lead PENDING + CONFIG/data-integrity pause.
15. 400 + reference active-404 → FAILED_ESCALATED.
16. 400 follow-up read auth/config failure → lead PENDING + global pause.
17. 400 follow-up read indeterminate → FAILED_ESCALATED technical/unclassified; no resend.

## Retry/reconciliation tests

Verify safe write attempts:

```text
1 initial
retry 1 after 5m
retry 2 after 30m
retry 3 after 2h
```

These automatic retries apply only to safe-to-repeat outcomes (`423`, `429`, `477`, or known pre-transmission transport failure).

Verify exact retry budget mutations:

```text
after initial create attempt: automatic_retry_count=0
after scheduling retry 1: automatic_retry_count=1
after scheduling retry 2: automatic_retry_count=2
after scheduling retry 3: automatic_retry_count=3
next safe-retry-required failure with count=3: FAILED_ESCALATED
no fourth automatic retry
human auth/config pause-clear does not reset or increment the counter
```

Verify:

- write counter increments before HTTP call,
- `httpx.ConnectError` / `ConnectTimeout` / `PoolTimeout` are safe-retry paths,
- `httpx.WriteError` / `WriteTimeout` / `ReadError` / `ReadTimeout` / `RemoteProtocolError` become UNKNOWN,
- stale SENDING → UNKNOWN, never direct PENDING,
- UNKNOWN first active lookup due +15s,
- failed/crashed read does not increment not-found counter,
- first active 404 → not_found=1 +60s,
- second active 404 → not_found=2 +300s,
- third active 404 → UNKNOWN_ESCALATED, **not RETRY_PENDING**,
- found at any stage → SUCCESS,
- reconciliation failure 1 → +5m,
- reconciliation failure 2 → +30m,
- third consecutive reconciliation failure → UNKNOWN_ESCALATED,
- successful active found/404 resets reconciliation error counter,
- 401/403 during reconciliation activate global pause,
- UNKNOWN never automatically resends a Contact create.

## Crash/restart tests

Required:

1. raw event committed + API returned 202 + worker stopped → restart processes it.
2. stale PROCESSING with no normalized row → RECEIVED.
3. stale PROCESSING with normalized row → NORMALIZED repair.
4. fresh orphaned SENDING at worker startup blocks all new CRM I/O until it reaches the 15-minute lease.
5. once that SENDING row is stale → UNKNOWN with reconciliation due immediately.
6. HubSpot create succeeded but local SUCCESS commit was lost → unique-reference read finds the expected reference/email → SUCCESS without duplicate create.
7. found reference with mismatching reference/email never becomes SUCCESS and activates CONFIG/data-integrity pause.
8. crash before reconciliation HTTP call does not consume not-found evidence.
9. crash after a successful active 404 but before DB commit does not consume not-found evidence; the read is safely repeated.
10. three active 404 checks after UNKNOWN end in UNKNOWN_ESCALATED and never create a duplicate retry.
11. an unexpected item-level exception that leaves a fresh row SENDING does not terminate the worker, but the next CRM selection sees SENDING and starts no new CRM I/O until the lease/reconciliation path resolves it.

## Global pause tests

- first 401 during create returns the lead to PENDING and sets singleton AUTH pause.
- first 403 during create returns the lead to PENDING and sets singleton CONFIG pause.
- auth/config failure during reconciliation keeps the lead UNKNOWN and preserves reconciliation counters.
- restart does not clear pause.
- while paused, normalization continues but no create/read CRM I/O starts.
- operator recovery clears only the singleton pause; it does not mass-rewrite leads.
- auth/config blockers do not consume `automatic_retry_count`.
- no public admin endpoint is required.

## Cluster alert tests

- 5 distinct qualifying leads in 10m → alert.
- 4 → no alert.
- EXISTING_CONTACT excluded.
- confirmed per-record permanent validation excluded.
- crossing threshold persists `cluster_alert_pending_at` before SMTP.
- failed SMTP leaves the cluster episode pending even after the 10-minute detection window expires.
- after successful cluster alert, `cluster_alerted_at` equals the frozen pending episode timestamp, not SMTP completion time.
- a new failure occurring after the frozen pending timestamp remains eligible for a later episode.
- a later new failure can make the same lead eligible for a later episode.

## SMTP tests

- deployed SMTP with credentials requires verified STARTTLS; certificate verification is not disabled.
- normalization failures use a separate Automation-only email at the same ~2h cadence.
- terminal CRM rows combine into one ~2h Ops email.
- CRM-terminal and normalization streams have independent attempt/success/error scheduler fields.
- CRM-terminal and normalization batches are bounded and mark only the selected
  successfully sent rows/events.
- success of one stream does not advance the other stream's success timestamp.
- failure of one stream remains eligible for its 5-minute retry even if the other stream succeeds.
- mark notification only after SMTP success.
- failed SMTP leaves rows eligible.
- immediate auth/config alert retries after failure.
- cluster alert marks rows only after success.
- one-worker topology prevents concurrent schedulers.
- simulate SMTP accepted + DB commit crash and accept possible duplicate email as documented at-least-once behavior.

## Manual Postman discovery already completed

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

The upsert result is evidence supporting the deliberate CREATE_ONLY decision; upsert is not used in implementation.

### 2026-09-02 — unique-property and archive behavior

| Check | Observed |
|---|---|
| batch read existing `INT-000001` by custom unique property | 200 COMPLETE; Contact returned |
| batch read nonexistent `INT-999999` | 207 COMPLETE; `OBJECT_NOT_FOUND` |
| batch read with `archived=true` + custom unique property | 400 VALIDATION_ERROR |
| single GET existing `INT-000001` by custom unique property | 200; Contact returned |
| single GET nonexistent `INT-999999` by custom unique property | 404 |
| repeat single GET existing `INT-000001` before Ticket 1B | 200; Contact ID `858179816647` returned again |
| repeat single GET nonexistent `INT-999999` before Ticket 1B | 404 again |
| create disposable archive-test Contact | 201 |
| archive disposable Contact by Record ID | 204 No Content |
| single GET archived record by custom unique property + `archived=true` | 400 VALIDATION_ERROR |
| create new active Contact reusing archived unique value | 201 Created |

The final two observations prove that HubSpot custom unique values are not a permanent idempotency key across archived records. Therefore repeated active `404` results after an UNKNOWN write can never authorize an automatic create retry in v1.

DELETE was used only to manufacture the archive test and is not part of the implementation flow.

## Live tests

Live HubSpot/SMTP tests remain manual/opt-in.

Before deployment:

- re-check Service Key status,
- re-check current supported API version,
- verify Contact scopes,
- verify `integration_reference` internal name/type/uniqueness,
- rerun critical create/read discovery calls.
