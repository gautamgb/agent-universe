"""Slack notifications for human-in-the-loop gates."""

from __future__ import annotations

import json
import os
from typing import Optional

import httpx


def notify_human_approval(
    *,
    thread_id: str,
    topic: str,
    project_name: str,
    architecture_excerpt: str,
    dashboard_hint: Optional[str] = None,
) -> None:
    """POST approval-needed message to SLACK_WEBHOOK_URL with optional interactive buttons."""
    url = os.environ.get("SLACK_WEBHOOK_URL")
    if not url:
        return

    base = (dashboard_hint or os.environ.get("AGENT_UNIVERSE_DASHBOARD_URL") or "").strip()
    lines = [
        "*Agent Universe — approval required*",
        f"Thread: `{thread_id}`",
        f"Project: `{project_name}`",
        f"Topic: {topic}",
        "",
        "*Architecture (excerpt)*",
        architecture_excerpt[:3500],
    ]
    if base:
        lines.extend(["", f"Open dashboard: {base}"])

    text = "\n".join(lines)
    value_base = {"thread_id": thread_id}
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "style": "primary",
                    "action_id": "au_approve",
                    "value": json.dumps({**value_base, "action": "approve"}),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Reject"},
                    "style": "danger",
                    "action_id": "au_reject",
                    "value": json.dumps({**value_base, "action": "reject"}),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Cancel"},
                    "action_id": "au_cancel",
                    "value": json.dumps({**value_base, "action": "cancel"}),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Recover"},
                    "action_id": "au_recover",
                    "value": json.dumps({**value_base, "action": "recover"}),
                },
            ],
        },
    ]
    httpx.post(url, json={"text": text, "blocks": blocks}, timeout=30.0)
