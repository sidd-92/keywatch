#input_type_name: NotifySlackInput
#output_type_name: NotifySlackResult
#function_name: notify_slack

import httpx
from pydantic import BaseModel
from typing import Optional
from lemma_sdk import FunctionContext, Pod
from datetime import date


class NotifySlackInput(BaseModel):
    credential_name: str
    message_type: Optional[str] = "alert"  # alert | rotated


class NotifySlackResult(BaseModel):
    ok: bool
    message: str


async def notify_slack(ctx: FunctionContext, data: NotifySlackInput) -> NotifySlackResult:

    pod = Pod.from_env()

    response = pod.query(f"SELECT * FROM credentials WHERE name = '{data.credential_name}'")
    items = response.to_dict().get("items", [])

    if not items:
        return NotifySlackResult(ok=False, message=f"Credential '{data.credential_name}' not found — skipping")

    cred = items[0]

    expiry = cred.get("expiry_date")
    days_left = None
    if expiry:
        expiry_date = date.fromisoformat(str(expiry))
        days_left = (expiry_date - date.today()).days

    provider = cred.get("provider", "unknown")
    status = cred.get("status", "active")
    owner_slack = cred.get("owner_slack", "@team")

    webhook_response = pod.query("SELECT value FROM webhook_config WHERE key = 'slack_webhook_url'")
    webhook_items = webhook_response.to_dict().get("items", [])
    if not webhook_items:
        return NotifySlackResult(ok=False, message="Slack webhook not configured — missing 'slack_webhook_url' in webhook_config")
    SLACK_WEBHOOK_URL = webhook_items[0]["value"]

    # ── Rotated notification ──
    if data.message_type == "rotated":
        slack_message = {
            "blocks": [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": f"✅ KeyWatch — {data.credential_name} Rotated"
                    }
                },
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*Provider:*\n{provider}"},
                        {"type": "mrkdwn", "text": f"*New Status:*\nACTIVE"},
                        {"type": "mrkdwn", "text": f"*New Expiry:*\n{expiry}"},
                        {"type": "mrkdwn", "text": f"*Days Until Expiry:*\n{days_left}"},
                        {"type": "mrkdwn", "text": f"*Owner:*\n{owner_slack}"},
                        {"type": "mrkdwn", "text": f"*Rotated By:*\nManual (Dashboard)"}
                    ]
                },
                {"type": "divider"},
                {
                    "type": "context",
                    "elements": [
                        {"type": "mrkdwn", "text": "🔐 KeyWatch — Credential Lifecycle Operator"}
                    ]
                }
            ]
        }
    else:
        # ── Alert notification ──
        if status not in ("critical", "warning"):
            return NotifySlackResult(ok=False, message=f"Credential '{data.credential_name}' is {status} — no alert needed")

        emoji = "🔴" if status == "critical" else "🟡"
        urgency = "ROTATE NOW" if status == "critical" else "ROTATE SOON"

        slack_message = {
            "blocks": [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": f"{emoji} KeyWatch Alert — {data.credential_name}"
                    }
                },
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*Provider:*\n{provider}"},
                        {"type": "mrkdwn", "text": f"*Status:*\n{status.upper()}"},
                        {"type": "mrkdwn", "text": f"*Expires:*\n{expiry}"},
                        {"type": "mrkdwn", "text": f"*Days Left:*\n{days_left}"},
                        {"type": "mrkdwn", "text": f"*Owner:*\n{owner_slack}"},
                        {"type": "mrkdwn", "text": f"*Action:*\n{urgency}"}
                    ]
                },
                {"type": "divider"},
                {
                    "type": "context",
                    "elements": [
                        {"type": "mrkdwn", "text": "🔐 KeyWatch — Credential Lifecycle Operator"}
                    ]
                }
            ]
        }

    async with httpx.AsyncClient() as client:
        slack_response = await client.post(SLACK_WEBHOOK_URL, json=slack_message, timeout=10.0)

    if slack_response.status_code == 200:
        pod.table("audit_log").create({
            "credential_name": data.credential_name,
            "provider": provider,
            "action": "rotated" if data.message_type == "rotated" else "slack_notified",
            "triggered_by": "notify_slack_function",
            "slack_notified": True,
            "notes": f"Manual rotation confirmed" if data.message_type == "rotated" else f"Slack alert sent — {status} expiring in {days_left} days"
        })
        return NotifySlackResult(ok=True, message="Slack notification sent successfully")
    else:
        return NotifySlackResult(ok=False, message=f"Slack webhook failed: {slack_response.status_code}")
