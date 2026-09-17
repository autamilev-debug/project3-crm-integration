# 03 — Authentication and Secrets

## Two auth boundaries

```text
source → FastAPI webhook
integration → HubSpot
```

The route source selects the inbound authenticator. Never trust a JSON `source` field to select authentication.

## website — HMAC-SHA256

Headers:

```text
X-Webhook-Timestamp: <unix-seconds>
X-Webhook-Signature: sha256=<lowercase-hex>
```

Secret:

```text
WEBSITE_HMAC_SECRET
```

Canonical bytes:

```text
ASCII(timestamp) + b"." + exact_raw_request_body
```

Digest:

```text
HMAC-SHA256(secret, canonical_bytes)
```

Rules:

- verify exact raw request bytes,
- constant-time digest comparison,
- timestamp must be Unix seconds,
- reject timestamps more than 300 seconds from server time by default,
- reject missing/invalid/expired signature with `401`,
- do not log signature or secret.

Config:

```text
WEBSITE_HMAC_MAX_SKEW_SECONDS=300
```

## linkedin — Bearer token

Header:

```text
Authorization: Bearer <token>
```

Secret:

```text
LINKEDIN_BEARER_TOKEN
```

Missing/invalid token → `401`.

Compare the configured token with the received token using constant-time comparison (`hmac.compare_digest`).

This is a simulated LinkedIn-like provider, not a claim about LinkedIn's real webhook authentication.

## partner — API key

Header:

```text
X-API-Key: <key>
```

Secret:

```text
PARTNER_API_KEY
```

Missing/invalid key → `401`.

Compare the configured API key with the received key using constant-time comparison (`hmac.compare_digest`).

## HubSpot — Service Key

Use:

```text
Authorization: Bearer <HUBSPOT_SERVICE_KEY>
```

Required v1 Contact scopes already tested:

```text
crm.objects.contacts.read
crm.objects.contacts.write
```

OAuth is out of scope because this is one controlled account, not an installable multi-tenant app.

HubSpot Service Keys are an explicitly accepted external beta dependency for this portfolio project. Re-check official status before deployment.

## HubSpot auth/config classification

Safe primary rules:

```text
401 → AUTH_FAILURE
403 → CONFIG_FAILURE (permissions/scopes)
404/405 on a frozen HubSpot endpoint → CONFIG_FAILURE
```

If HubSpot exposes a documented structured error code/context that clearly identifies a missing required CRM property/configuration, it may also map to `CONFIG_FAILURE`.

Do not infer auth/config failure by searching arbitrary English message text.

## Durable pause behavior

On `AUTH_FAILURE` or `CONFIG_FAILURE`, preserve the current lead in a resumable state and set the singleton runtime pause in the same transaction.

```text
blocked create → lead returns to PENDING
blocked reconciliation/read → lead remains UNKNOWN
```

The normal transient retry budget is not consumed by auth/configuration blockers.

Runtime state:

```text
AUTH_FAILURE   → crm_delivery_paused=true, reason=AUTH
CONFIG_FAILURE → crm_delivery_paused=true, reason=CONFIG
```

Attempt an immediate technical SMTP alert.

Because v1 has one sequential CRM worker, once the pause is persisted no further HubSpot create/reconciliation work is started until human recovery.

Normalization may continue.

Recovery clears the singleton pause using the non-public operator command defined in `02_database.md`; it does not mass-rewrite leads.

## Secret rules

Never:

- hard-code real secrets,
- commit real `.env`,
- persist Service Keys/source auth values as application data,
- log Authorization headers, HMAC signatures, API keys, SMTP passwords,
- include secrets in email diagnostics.

`.env.example` contains variable names and safe placeholders only.
