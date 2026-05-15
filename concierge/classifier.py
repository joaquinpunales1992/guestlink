"""First-message classifier: pick the service a guest is asking about.

Two backends:
  - Claude (Anthropic SDK) when ANTHROPIC_API_KEY is set.
  - Keyword fallback otherwise — scans Service.keywords for substring matches.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Iterable

from django.conf import settings

from .models import Service

logger = logging.getLogger(__name__)

CLASSIFIER_MODEL = "claude-haiku-4-5-20251001"


@dataclass
class Classification:
    service_slug: str | None
    confidence: float = 0.0
    extracted_fields: dict = field(default_factory=dict)
    backend: str = "unknown"


def classify(message: str, services: Iterable[Service] | None = None) -> Classification:
    """Return the best-guess Service for a first guest message.

    Falls back to keyword matching if Anthropic isn't configured or errors out.
    """
    services = list(services) if services is not None else list(Service.objects.filter(active=True))
    if not services:
        return Classification(service_slug=None, backend="empty")

    if settings.ANTHROPIC_API_KEY:
        try:
            return _classify_with_claude(message, services)
        except Exception:
            logger.exception("Claude classifier failed; falling back to keywords")

    return _classify_with_keywords(message, services)


def _classify_with_keywords(message: str, services: list[Service]) -> Classification:
    lowered = message.lower()
    best: tuple[int, Service] | None = None
    for svc in services:
        hits = sum(1 for kw in svc.keyword_list if kw and kw in lowered)
        if hits and (best is None or hits > best[0]):
            best = (hits, svc)
    if best is None:
        return Classification(service_slug=None, backend="keywords")
    return Classification(
        service_slug=best[1].slug,
        confidence=min(1.0, best[0] / 3.0),
        backend="keywords",
    )


def _classify_with_claude(message: str, services: list[Service]) -> Classification:
    import anthropic

    catalog = [
        {
            "slug": s.slug,
            "name_en": s.name_en,
            "name_es": s.name_es,
            "description": s.description_en or s.description_es,
        }
        for s in services
    ]
    slugs = [s["slug"] for s in catalog] + ["unknown"]

    tool_schema = {
        "type": "object",
        "properties": {
            "service_slug": {
                "type": "string",
                "enum": slugs,
                "description": "Slug of the matching service, or 'unknown' if the request doesn't match any.",
            },
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "extracted_fields": {
                "type": "object",
                "description": "Optional fields parsed from the message.",
                "properties": {
                    "date": {"type": "string", "description": "ISO date if mentioned (e.g. 2026-05-20)."},
                    "time": {"type": "string", "description": "Time of day if mentioned (e.g. 14:00 or 'morning')."},
                    "party_size": {"type": "integer", "minimum": 1},
                    "language": {"type": "string", "description": "Guest's language: en, es, fr, de, pt, etc."},
                    "notes": {"type": "string", "description": "Anything else worth flagging to the host."},
                },
            },
        },
        "required": ["service_slug", "confidence"],
    }

    system = (
        "You classify short WhatsApp messages from Airbnb guests asking for local services "
        "in Bayahibe, Dominican Republic. Pick the best matching service slug from the catalog, "
        "or 'unknown' if nothing matches. Extract any concrete details (date, time, party size, "
        "language). Be conservative with confidence — if the request is vague, use < 0.5."
    )
    user = (
        f"Service catalog:\n{json.dumps(catalog, ensure_ascii=False, indent=2)}\n\n"
        f"Guest message:\n\"\"\"\n{message}\n\"\"\""
    )

    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model=CLASSIFIER_MODEL,
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": user}],
        tools=[{"name": "classify_request", "description": "Record the classification.", "input_schema": tool_schema}],
        tool_choice={"type": "tool", "name": "classify_request"},
    )

    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "classify_request":
            data = block.input
            slug = data.get("service_slug")
            if slug == "unknown":
                slug = None
            return Classification(
                service_slug=slug,
                confidence=float(data.get("confidence", 0.0)),
                extracted_fields=data.get("extracted_fields", {}) or {},
                backend="claude",
            )

    return Classification(service_slug=None, backend="claude_no_tool_use")
