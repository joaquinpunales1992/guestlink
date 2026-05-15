"""Thin wrapper around the Meta WhatsApp Cloud API send-message endpoint.

Honors WHATSAPP_DRY_RUN — when true, messages are logged and persisted but not
actually sent to Meta. This lets us build and exercise the relay end-to-end
before we have an approved Business number.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

GRAPH_API_BASE = "https://graph.facebook.com/v21.0"


class WhatsAppError(Exception):
    pass


@dataclass
class SendResult:
    ok: bool
    wa_message_id: str = ""
    raw: dict | None = None
    dry_run: bool = False


def send_text(to_phone: str, body: str) -> SendResult:
    """Send a plain-text WhatsApp message via Cloud API. Returns SendResult.

    In dry-run mode (default until WHATSAPP_DRY_RUN=0 and creds are set), the
    message is only logged and a synthetic SendResult is returned.
    """
    to_phone = to_phone.lstrip("+")  # Meta expects digits-only
    if settings.WHATSAPP_DRY_RUN:
        logger.info("[DRY] → %s: %s", to_phone, body)
        return SendResult(ok=True, dry_run=True)

    if not (settings.WHATSAPP_PHONE_NUMBER_ID and settings.WHATSAPP_ACCESS_TOKEN):
        raise WhatsAppError(
            "WHATSAPP_DRY_RUN=0 but WHATSAPP_PHONE_NUMBER_ID or WHATSAPP_ACCESS_TOKEN is unset"
        )

    url = f"{GRAPH_API_BASE}/{settings.WHATSAPP_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {settings.WHATSAPP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "text",
        "text": {"body": body, "preview_url": False},
    }
    resp = requests.post(url, json=payload, headers=headers, timeout=15)
    if resp.status_code >= 300:
        logger.error("Cloud API error %s: %s", resp.status_code, resp.text)
        raise WhatsAppError(f"Cloud API returned {resp.status_code}: {resp.text}")

    data = resp.json()
    wa_id = ""
    messages = data.get("messages") or []
    if messages:
        wa_id = messages[0].get("id", "")
    return SendResult(ok=True, wa_message_id=wa_id, raw=data)
