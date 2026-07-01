#input_type_name: NotifyDiscordInput
#output_type_name: NotifyDiscordResult
#function_name: notify_discord

import httpx
from pydantic import BaseModel
from typing import Optional
from lemma_sdk import FunctionContext, Pod
from datetime import date


class NotifyDiscordInput(BaseModel):
    credential_name: str
    message_type: Optional[str] = "alert"  # alert | rotated


class NotifyDiscordResult(BaseModel):
    ok: bool
    message: str


async def notify_discord(ctx: FunctionContext, data: NotifyDiscordInput) -> NotifyDiscordResult:

    pod = Pod.from_env()

    response = pod.query(f"SELECT * FROM credentials WHERE name = '{data.credential_name}'")
    items = response.to_dict().get("items", [])

    if not items:
        return NotifyDiscordResult(ok=False, message=f"Credential '{data.credential_name}' not found — skipping")

    cred = items[0]

    expiry = cred.get("expiry_date")
    days_left = None
    if expiry:
        expiry_date = date.fromisoformat(str(expiry))
        days_left = (expiry_date - date.today()).days

    provider = cred.get("provider", "unknown")
    status = cred.get("status", "active")
    owner_slack = cred.get("owner_slack", "@team")

    webhook_response = pod.query("SELECT value FROM webhook_config WHERE key = 'discord_webhook_url'")
    webhook_items = webhook_response.to_dict().get("items", [])
    if not webhook_items:
        return NotifyDiscordResult(ok=False, message="Discord webhook not configured — missing 'discord_webhook_url' in webhook_config")
    DISCORD_WEBHOOK_URL = webhook_items[0]["value"]

    if data.message_type == "rotated":
        embed = {
            "title": f"✅ KeyWatch — {data.credential_name} Rotated",
            "color": 3066993,
            "fields": [
                {"name": "Provider", "value": provider, "inline": True},
                {"name": "New Status", "value": "ACTIVE", "inline": True},
                {"name": "New Expiry", "value": str(expiry), "inline": True},
                {"name": "Days Until Expiry", "value": str(days_left), "inline": True},
                {"name": "Owner", "value": owner_slack, "inline": True},
                {"name": "Rotated By", "value": "Manual (Dashboard)", "inline": True},
            ],
            "footer": {"text": "KeyWatch — Credential Lifecycle Operator"}
        }
    else:
        if status not in ("critical", "warning"):
            return NotifyDiscordResult(ok=False, message=f"Credential '{data.credential_name}' is {status} — no alert needed")

        color = 15158332 if status == "critical" else 15105570
        emoji = "🔴" if status == "critical" else "🟡"
        urgency = "ROTATE NOW" if status == "critical" else "ROTATE SOON"

        embed = {
            "title": f"{emoji} KeyWatch Alert — {data.credential_name}",
            "color": color,
            "fields": [
                {"name": "Provider", "value": provider, "inline": True},
                {"name": "Status", "value": status.upper(), "inline": True},
                {"name": "Expires", "value": str(expiry), "inline": True},
                {"name": "Days Left", "value": str(days_left), "inline": True},
                {"name": "Owner", "value": owner_slack, "inline": True},
                {"name": "Action", "value": urgency, "inline": True},
            ],
            "footer": {"text": "KeyWatch — Credential Lifecycle Operator"}
        }

    discord_message = {"embeds": [embed]}

    async with httpx.AsyncClient() as client:
        discord_response = await client.post(DISCORD_WEBHOOK_URL, json=discord_message, timeout=10.0)

    if discord_response.status_code in (200, 204):
        pod.table("audit_log").create({
            "credential_name": data.credential_name,
            "provider": provider,
            "action": "discord_notified" if data.message_type != "rotated" else "rotated",
            "triggered_by": "notify_discord_function",
            "slack_notified": False,
            "notes": f"Discord alert sent for {status} credential" if data.message_type != "rotated" else "Discord rotation confirmation sent",
        })
        return NotifyDiscordResult(ok=True, message="Discord notification sent successfully")
    else:
        return NotifyDiscordResult(ok=False, message=f"Discord webhook failed: {discord_response.status_code}")
