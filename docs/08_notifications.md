# 08 — SMTP Notifications and Operational Alerts

## Scope

Notifications are operational support, not CRM business workflow.

Use generic SMTP configured through environment variables.

No exactly-once email subsystem is required.

## SMTP transport security

For Railway/live testing with real credentials:

```text
SMTP_STARTTLS=true
```

Use verified TLS with normal certificate verification enabled. Do not disable certificate checks.

This project does not support authenticated plaintext SMTP in deployed environments. Gmail testing should use STARTTLS (for example port 587) with an App Password, never the normal Gmail password.

## Recipients

Configuration:

```text
OPS_EMAIL_TO
AUTOMATION_EMAIL_TO
```

Terminal CRM failures:

```text
TO: Ops
CC: Automation/Integration
```

Auth/config and clustered technical alerts:

```text
TO: Automation/Integration
CC: Ops
```

Normalization failures:

```text
TO: Automation/Integration
```

## Safe content

Emails may include:

```text
integration_reference
source
source_event_id
normalized_lead_id
delivery status
write attempt count
first/last failure timestamps
safe error category/code
HubSpot correlation ID
```

Do not include secrets or full raw payloads.

Only include contact/business fields when required for Ops recovery; do not dump arbitrary `source_metadata`.

## 2-hour terminal-failure batch

Eligible normalized leads:

```text
FAILED_ESCALATED
UNKNOWN_ESCALATED
```

and:

```text
escalation_notified_at IS NULL
```

Each email contains at most `NOTIFICATION_BATCH_MAX_ITEMS` eligible records
(default `100`), selected oldest first with a stable ID tie-breaker. Only records
included in a successfully sent email are marked notified; any remainder stays
pending for a later due batch.

The CRM-terminal stream and normalization-failure stream use **independent** scheduler state so success in one cannot delay retry of the other.

CRM terminal email:

```text
last_crm_batch_attempt_at
last_crm_batch_success_at
last_crm_batch_error
```

Normalization email:

```text
last_normalization_batch_attempt_at
last_normalization_batch_success_at
last_normalization_batch_error
```

Each stream is due independently when it has unsent rows/events and either:

- it has no previous successful batch, or
- at least about 2 hours have passed since that stream's last successful batch.

Before an SMTP attempt, set that stream's `*_attempt_at=now`.

If that stream's previous attempt failed, do not retry it more often than every 5 minutes.

If a stream succeeds:

```text
set escalation_notified_at=now for only the rows/events included in that email
set that stream's *_success_at=now
clear that stream's *_error
```

If a stream fails:

```text
do not set escalation_notified_at for that stream
persist that stream's safe *_error
retry that stream after at least 5 minutes
```

A successful CRM-terminal email does not change normalization scheduling, and vice versa. A failed SMTP attempt must never be recorded as sent.

## Normalization-failure notification

`NORMALIZATION_FAILED` events use the same approximately 2-hour scheduler cadence, but they are sent as a separate technical email:

```text
TO: Automation/Integration
```

Use `webhook_events.escalation_notified_at` with the same success-only marking rule.

Use the stable `webhook_events.completed_at` timestamp for normalization-failure
batch ordering and completion context. Do not use the mutable `updated_at` field
for that purpose.

Do not send normalization failures to Ops by default; they are adapter/source-contract technical issues in this project.

## Immediate auth/config alert

When the singleton runtime state enters a new paused episode:

```text
AUTH or CONFIG
→ attempt immediate SMTP alert
```

If send succeeds:

```text
pause_alert_sent_at=now
```

If send fails:

```text
leave pause_alert_sent_at NULL
persist safe error
retry on the worker's 5-minute notification check
```

A new human recovery clears the paused episode. A later new pause is a new alert episode.

## Clustered technical alert

Default rule:

```text
5 distinct normalized leads
with qualifying technical failures
inside rolling 10 minutes
```

Qualifying failures are defined in the project contract.

A lead qualifies for a new episode when:

```text
last_technical_failure_at within 10 minutes
AND (
  cluster_alerted_at IS NULL
  OR last_technical_failure_at > cluster_alerted_at
)
```

If at least five distinct leads qualify, first persist:

```text
cluster_alert_pending_at=now
```

for the selected leads. This freezes the alert episode before SMTP I/O.

Then send one immediate cluster alert.

After SMTP success only:

```text
set cluster_alerted_at = the frozen cluster_alert_pending_at value
clear cluster_alert_pending_at
```

Using the frozen episode timestamp rather than SMTP completion time ensures that a newer failure occurring while the email is in flight remains eligible for a later alert episode.

If SMTP fails, keep `cluster_alert_pending_at` so the episode remains retryable even after the original 10-minute detection window has passed. Retry on the next 5-minute notification check.

This allows the same lead to contribute to a later genuinely new outage after it experiences a new qualifying failure.

## Concurrency and delivery semantics

Railway v1 has one worker replica, so two schedulers cannot select/send the same batch concurrently.

SMTP is **at-least-once**:

- if SMTP clearly fails, retry,
- if SMTP accepts the email and the worker crashes before the DB timestamp commits, a duplicate message may be sent after restart,
- that rare duplicate is acceptable for v1.

Do not add distributed locks or an exactly-once notification outbox solely to eliminate this rare duplicate.

## Alert-channel failure

If SMTP remains unavailable:

- keep unsent state in PostgreSQL,
- log the safe failure prominently,
- keep retrying at the defined cadence.

The system cannot guarantee notification if the notification channel itself is unavailable.
