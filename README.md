# KeyWatch

KeyWatch is a credential lifecycle management pod built on [Lemma](https://lemma.ai). It tracks API key expiry dates across providers (Stripe, Resend, GitHub), runs a daily scan to classify urgency, fires Slack alerts for credentials approaching expiration, and provides a human-in-the-loop rotation workflow.

---

## What it does

- **Daily scan** — at 04:30 UTC, the `expiry-scanner` agent queries every credential, calculates days until expiry, and classifies each as `critical` (≤ 2 days), `warning` (3–7 days), or `active` (> 7 days).
- **Slack alerts** — for every critical/warning credential, a rich Block Kit message is posted to a Slack channel with provider, status, expiry date, days remaining, and owner.
- **Audit trail** — every scan event and notification is recorded in `audit_log` with full context (action, triggered_by, old/new expiry, slack_notified).
- **Rotation workflow** — a manual, approval-gated workflow lets an operator enter a credential name, receive a Slack pre-notification, and approve or reject rotation — all tracked in the audit log.
- **Chat interface** — the `expiry-scanner` agent can answer ad-hoc questions about credential status without performing any writes.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Schedule: daily-expiry-scan  (cron: 30 4 * * *)            │
│                 │                                           │
│                 ▼                                           │
│  Agent: expiry-scanner ──reads/writes──► Table: credentials │
│                 │                                           │
│                 ├──creates──────────────► Table: audit_log  │
│                 │                                           │
│                 └──calls────────────────► Function: notify_slack
│                                                   │         │
│                                                   └──POST──► Slack
│                                                             │
│  Workflow: credential-rotation (manual trigger)             │
│    FORM → Function: check_credential                        │
│         → Function: notify_slack                            │
│         → FORM (approval)                                   │
│         → END                                               │
└─────────────────────────────────────────────────────────────┘
```

---

## Pod structure

```
keywatch/
├── agents/
│   └── expiry-scanner/
│       ├── expiry-scanner.json     # Agent definition & permissions
│       └── instruction.md          # System prompt / behavioral spec
├── functions/
│   ├── check_credential/
│   │   ├── check_credential.json   # Function manifest
│   │   └── code.py                 # Looks up a credential by name
│   └── notify_slack/
│       ├── notify_slack.json       # Function manifest
│       └── code.py                 # Posts Block Kit messages to Slack
├── tables/
│   ├── credentials/
│   │   └── credentials.json        # Schema: API key registry
│   └── audit_log/
│       └── audit_log.json          # Schema: immutable event log
├── workflows/
│   └── credential-rotation/
│       └── credential-rotation.json  # DAG for manual rotation
└── schedules/
    └── daily-expiry-scan/
        └── daily-expiry-scan.json  # Cron trigger for expiry-scanner
```

---

## Data model

### `credentials`

| Column | Type | Notes |
|---|---|---|
| `id` | PK | Auto-generated primary key |
| `name` | TEXT (required) | Human-readable credential name |
| `provider` | ENUM | `stripe` \| `resend` \| `github` |
| `key_hint` | TEXT | Last few characters of the key (display only — never store the full key) |
| `expiry_date` | DATE (required) | When the credential expires |
| `status` | ENUM | `active` \| `warning` \| `critical` \| `rotated` |
| `last_rotated` | DATE | Date of last rotation |
| `vault_reference` | TEXT | Reference ID in an external secrets vault |
| `owner_slack` | TEXT | Slack handle of the credential owner (e.g. `@alice`) |
| `notes` | TEXT | Free-form notes |

### `audit_log`

| Column | Type | Notes |
|---|---|---|
| `id` | PK | Auto-generated primary key |
| `credential_name` | TEXT (required) | Name of the affected credential |
| `provider` | ENUM | Same enum as `credentials.provider` |
| `action` | TEXT (required) | e.g. `slack_notified`, `rotated`, `scan_detected` |
| `triggered_by` | TEXT (required) | Who/what caused the event |
| `old_expiry` | DATE | Expiry before rotation |
| `new_expiry` | DATE | Expiry after rotation |
| `slack_notified` | BOOLEAN | Whether Slack was successfully notified |
| `notes` | TEXT | Free-form context |

Records are only ever **created**, never updated — the audit log is append-only.

---

## Components

### Agent: `expiry-scanner`

The core intelligence of the pod. Operates in two modes:

**Scan mode** (triggered by schedule or `"run scan"`)
1. Queries all credentials
2. Calculates days until expiry for each
3. Classifies: `critical` (≤ 2 days), `warning` (3–7 days), `active` (> 7 days)
4. Updates the `status` field on each credential record
5. Creates an `audit_log` entry for each critical/warning credential
6. Calls `notify_slack` for each critical/warning credential
7. Responds with a formatted scan summary

**Chat mode** (all other messages)
Read-only. Answers questions about credential status without performing any writes.

**Scan summary format:**
```
Scan Summary — 2026-06-28
🔴 Critical (< 2 days): 1
🟡 Warning (2-7 days): 3
🟢 Active (> 7 days): 12

Name / Provider / Expires / Days left / Action
```

**Permissions:** read + write on `credentials` and `audit_log`; execute on `notify_slack`.

---

### Function: `notify_slack`

Posts a Slack Block Kit message via webhook. Supports two message types:

- **`alert`** (default) — fires for `critical` or `warning` credentials. Includes provider, status, expiry, days left, owner, and a call-to-action (`ROTATE NOW` or `ROTATE SOON`). Returns early without posting if the credential is `active`.
- **`rotated`** — fires after a successful manual rotation to confirm the new state.

After a successful post, the function creates an `audit_log` record marking the notification.

**Input:**
```json
{
  "credential_name": "my-stripe-key",
  "message_type": "alert"
}
```

**Output:**
```json
{
  "ok": true,
  "message": "Slack notification sent successfully"
}
```

---

### Function: `check_credential`

Point lookup for a single credential by name. Used by the rotation workflow to validate that a credential exists before proceeding.

**Input:**
```json
{
  "credential_name": "my-stripe-key"
}
```

**Output:**
```json
{
  "found": true,
  "credential_name": "my-stripe-key",
  "provider": "stripe",
  "expiry_date": "2026-07-01",
  "days_left": 3,
  "status": "warning",
  "owner_slack": "@alice"
}
```

---

### Workflow: `credential-rotation`

A manually triggered, human-in-the-loop workflow for rotating a credential.

**Steps:**
1. **Intake form** — operator enters the `credential_name`
2. **Check credential** — calls `check_credential`; if not found, ends with `Credential Not Found`
3. **Notify Slack** — posts an alert to Slack so the team is aware
4. **Approval form** — operator approves or rejects the rotation, with optional notes
5. **Decision** — approved → `Rotation Complete`; rejected → `Rotation Rejected`

All outcomes are captured in the audit log by `notify_slack`.

---

### Schedule: `daily-expiry-scan`

Triggers `expiry-scanner` every day at **04:30 UTC** (`30 4 * * *`). The agent detects the schedule trigger and automatically enters scan mode.

---

## Security boundaries

- **API key values are never stored.** Only `key_hint` (a short suffix) and a `vault_reference` are kept.
- The `expiry-scanner` agent never performs rotation directly — it only classifies and alerts.
- The audit log is append-only; the agent is instructed never to update existing records.
- All components are scoped to `POD` visibility, meaning they are not exposed outside the pod without explicit grants.

---

## Getting started

### Prerequisites

- A [Lemma](https://lemma.ai) account with the Lemma CLI installed
- A Slack incoming webhook URL

### 1. Configure the Slack webhook

Open [functions/notify_slack/code.py](functions/notify_slack/code.py) and replace the `SLACK_WEBHOOK_URL` constant with your own Slack incoming webhook URL.

### 2. Import the pod

```bash
lemma pod import ./keywatch
```

### 3. Add credentials

Use the Lemma dashboard or the `expiry-scanner` chat interface to add rows to the `credentials` table.

### 4. Run a manual scan

Open the `expiry-scanner` agent and send:

```
run scan
```

The agent will classify all credentials, update statuses, write audit log entries, and post Slack alerts for anything critical or warning.

### 5. Rotate a credential

Trigger the `credential-rotation` workflow manually from the Lemma dashboard, enter the credential name, review the Slack notification, and approve or reject rotation.

---

## Extending KeyWatch

| Goal | How |
|---|---|
| Add a new provider | Add the provider name to the `ENUM` options in `credentials.json` and `audit_log.json` |
| Add a new alert channel | Create a new function alongside `notify_slack` and call it from `expiry-scanner` |
| Change scan frequency | Edit the `cron` expression in `schedules/daily-expiry-scan/daily-expiry-scan.json` |
| Change urgency thresholds | Edit the scan mode logic in `agents/expiry-scanner/instruction.md` |
| Connect to a secrets vault | Populate `vault_reference` on each credential and add a rotation step to the workflow that calls your vault's API |
