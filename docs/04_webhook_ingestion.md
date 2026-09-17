# 04 — Webhook Ingestion

## Endpoint

```text
POST /webhooks/{source}
```

Supported literal route values:

```text
website
linkedin
partner
```

Unsupported source → `404`.

The source is case-sensitive. Do not normalize `Website` to `website`.

## Request-size guardrail

Maximum raw webhook body:

```text
256 KiB (262144 bytes) default
```

Configurable if desired.

Reject larger bodies with `413 Payload Too Large` before persistence.

This protects application memory and PostgreSQL from oversized authenticated requests.

## Exact request-stage sequence

```text
1. Read literal source from /webhooks/{source}.
2. Verify source is exactly website/linkedin/partner.
3. Read raw body with size limit.
4. Authenticate using that source's mechanism.
5. Auth failure → 401; do not persist payload.
6. Parse JSON object.
7. Invalid JSON/object/minimal envelope → 400.
8. Extract source-specific event ID.
9. Insert webhook_events(RECEIVED).
10. UNIQUE(source,event_id) resolves races.
11. Commit.
12. Return 202.
13. Full provider validation/normalization happens later in worker.
```

## Event-ID rules

The extracted source event ID must:

- already be a JSON string,
- be 1–128 characters,
- have no leading/trailing whitespace,
- be used exactly as supplied,
- be case-sensitive,
- never be lowercased, uppercased, or trimmed.

Examples:

```text
WEB-001  != web-001
" WEB-001" is rejected
12345 as a JSON number is rejected
```

This prevents accidental idempotency collisions caused by coercion/canonicalization.

## Minimal envelope before 202

Pre-ack validation proves only:

- authenticated request,
- JSON object,
- valid source event ID under the rules above.

Do not run business/CRM validation before persistence.

Full provider models run later so an accepted raw event remains auditable even if normalization fails.

## Responses

First valid delivery:

```http
202 Accepted
```

Suggested:

```json
{"status":"accepted","duplicate":false}
```

Duplicate `(source,event_id)`:

```http
202 Accepted
```

Suggested:

```json
{"status":"accepted","duplicate":true}
```

Persistence unavailable/fails:

```http
503 Service Unavailable
```

Do not expose internal exceptions.

## Idempotency

Final correctness guarantee:

```text
UNIQUE(source,event_id)
```

A Python pre-check is optional optimization only.

Duplicate behavior:

- no second raw row,
- no second normalized lead,
- no second HubSpot call,
- return `202`.

A corrected provider submission must use a new event ID.

## Raw payload

Persist the authenticated parsed JSON object in JSONB.

Do not persist:

- Authorization header,
- HMAC signature,
- API key,
- provider secret.

Provider fields not modeled by Pydantic remain preserved in `raw_payload` even if ignored by the normalization model.
