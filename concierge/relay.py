"""Relay routing: incoming WhatsApp messages → outbound, persisted along the way.

Single entry point: handle_inbound(from_phone, body, ...)

Routing rules:
  - If from_phone matches an active Provider, treat as provider message:
      * Look for [CODE] prefix → forward to that ticket's guest.
      * No prefix but provider has exactly one active ticket → use it.
      * Otherwise → reply asking for the code.
  - Else treat as guest:
      * If guest has an active ticket → forward to its provider (with code prefix).
      * Else → classify, pick Service + Provider, create Ticket, intro the
        provider and ack the guest.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from django.conf import settings
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from .classifier import classify
from .models import Guest, Location, Message, Provider, Service, Ticket, normalize_phone
from .whatsapp import send_text

logger = logging.getLogger(__name__)

CODE_RE = re.compile(r"\[\s*([A-Z0-9]{4,8})\s*\]", re.IGNORECASE)


@dataclass
class RelayOutcome:
    handled: bool
    ticket: Ticket | None = None
    note: str = ""
    # True when something was persisted but never reached its recipient. The
    # webhook still returns 200 (Meta must not retry), so this is how the
    # caller learns delivery broke.
    delivery_failed: bool = False


def detect_location(body: str) -> Location | None:
    """Work out which venue's QR started this conversation.

    The landing page pre-fills "I'm at <venue>", so the venue's name is usually
    sitting in the guest's first message. Longest name first, so "Hotel Bahía
    Príncipe" is not mistaken for "Bahía" when both exist.
    """
    lowered = (body or "").lower()
    candidates = [
        loc for loc in Location.objects.filter(active=True) if loc.name and loc.name.lower() in lowered
    ]
    return max(candidates, key=lambda loc: len(loc.name), default=None)


def _strip_code(body: str) -> str:
    return CODE_RE.sub("", body, count=1).strip()


def _format_provider_intro(ticket: Ticket, original_body: str) -> str:
    # Never the guest's phone number. The whole point of relaying through the
    # business number is that neither side gets the other's contact details —
    # leaking it here would let the provider book the guest directly next time.
    # Guests are created without a name on first contact, so the ticket code is
    # the usual fallback and is enough for the provider to refer to them.
    guest_label = ticket.guest.name.strip() or ticket.short_code
    fields = ticket.extracted_fields or {}
    field_lines = []
    for key in ("date", "time", "party_size", "language", "notes"):
        if fields.get(key):
            field_lines.append(f"  - {key}: {fields[key]}")
    fields_block = ("\n" + "\n".join(field_lines)) if field_lines else ""
    return (
        f"[{ticket.short_code}] Nueva referencia vía {settings.HOST_NAME} "
        f"(Apto {settings.HOST_APARTMENT_LABEL})\n"
        f"Servicio: {ticket.service.name_es}\n"
        f"Huésped: {guest_label}{fields_block}\n\n"
        f"Mensaje original:\n«{original_body}»\n\n"
        f"Por favor responde empezando con [{ticket.short_code}] para mantener el hilo."
    )


def _format_guest_ack(ticket: Ticket) -> str:
    return (
        f"Hi! I'm {settings.HOST_NAME}, your host. I'm connecting you with "
        f"{ticket.provider.name} who handles {ticket.service.name_en.lower()}. "
        f"They'll reply here in a moment."
    )


def _format_guest_no_match() -> str:
    return (
        f"Hi! I'm {settings.HOST_NAME}, your host. I got your message — I don't "
        f"have an exact match for that yet, but I'll get back to you personally."
    )


def _format_provider_missing_code() -> str:
    return (
        "Hola, no pude identificar a qué huésped responde tu mensaje. "
        "Por favor responde empezando con el código entre corchetes (ej. [A47B3])."
    )


@transaction.atomic
def handle_inbound(
    from_phone: str,
    body: str,
    *,
    wa_message_id: str = "",
    raw: dict | None = None,
) -> RelayOutcome:
    from_phone = normalize_phone(from_phone)
    body = (body or "").strip()
    if not from_phone or not body:
        return RelayOutcome(handled=False, note="empty from/body")

    # Watermark rather than threading a flag through every handler: catches
    # each send path, including ones added later.
    last_id = Message.objects.aggregate(m=Max("id"))["m"] or 0

    provider = Provider.objects.filter(phone=from_phone, active=True).first()
    if provider is not None:
        outcome = _handle_provider_message(provider, body, wa_message_id, raw)
    else:
        outcome = _handle_guest_message(from_phone, body, wa_message_id, raw)

    outcome.delivery_failed = Message.objects.filter(
        id__gt=last_id, delivery_status=Message.Delivery.FAILED
    ).exists()
    return outcome


def _persist_inbound(
    *, ticket: Ticket | None, direction: str, from_phone: str, to_phone: str,
    body: str, wa_message_id: str, raw: dict | None,
) -> Message:
    return Message.objects.create(
        ticket=ticket,
        direction=direction,
        from_phone=from_phone,
        to_phone=to_phone,
        body=body,
        wa_message_id=wa_message_id,
        raw_payload=raw,
    )


def _send_and_log(
    *, ticket: Ticket | None, direction: str, to_phone: str, body: str,
) -> Message:
    """Send, and record the attempt whether or not it succeeded.

    A delivery failure must never propagate. handle_inbound runs in a
    transaction and the webhook view swallows exceptions, so an exception here
    used to roll back the guest's inbound message and their ticket, return 200
    to Meta, and leave nothing behind but a log line — the request simply
    vanished. An expired access token was enough to do it.

    Recording the failed row instead keeps the ticket, keeps the conversation,
    and makes the breakage visible in the admin so the host can follow up by
    hand.
    """
    status = Message.Delivery.SENT
    error = ""
    wa_message_id = ""
    try:
        result = send_text(to_phone, body)
    except Exception as exc:  # noqa: BLE001 - losing the message is worse
        logger.exception("Delivery to %s failed (ticket=%s)", to_phone, ticket)
        status = Message.Delivery.FAILED
        error = f"{type(exc).__name__}: {exc}"[:2000]
    else:
        wa_message_id = result.wa_message_id
        if result.dry_run:
            status = Message.Delivery.DRY_RUN

    return Message.objects.create(
        ticket=ticket,
        direction=direction,
        from_phone="",  # business number; we don't track it here
        to_phone=to_phone,
        body=body,
        wa_message_id=wa_message_id,
        delivery_status=status,
        delivery_error=error,
    )


def _handle_provider_message(
    provider: Provider, body: str, wa_message_id: str, raw: dict | None
) -> RelayOutcome:
    match = CODE_RE.search(body)
    ticket: Ticket | None = None
    if match:
        code = match.group(1).upper()
        ticket = Ticket.objects.filter(short_code=code, provider=provider).first()

    if ticket is None:
        active = list(provider.tickets.filter(status__in=[Ticket.Status.OPEN, Ticket.Status.IN_PROGRESS]))
        if len(active) == 1:
            ticket = active[0]

    if ticket is None:
        _persist_inbound(
            ticket=None,
            direction=Message.Direction.PROVIDER_IN,
            from_phone=provider.phone,
            to_phone="",
            body=body,
            wa_message_id=wa_message_id,
            raw=raw,
        )
        _send_and_log(
            ticket=None,
            direction=Message.Direction.SYSTEM_OUT,
            to_phone=provider.phone,
            body=_format_provider_missing_code(),
        )
        return RelayOutcome(handled=True, note="provider message without resolvable ticket")

    _persist_inbound(
        ticket=ticket,
        direction=Message.Direction.PROVIDER_IN,
        from_phone=provider.phone,
        to_phone="",
        body=body,
        wa_message_id=wa_message_id,
        raw=raw,
    )
    if ticket.status == Ticket.Status.OPEN:
        ticket.status = Ticket.Status.IN_PROGRESS
        ticket.save(update_fields=("status",))
    ticket.guest.last_seen = timezone.now()
    ticket.guest.save(update_fields=("last_seen",))

    forwarded = _strip_code(body) or body
    _send_and_log(
        ticket=ticket,
        direction=Message.Direction.GUEST_OUT,
        to_phone=ticket.guest.phone,
        body=forwarded,
    )
    return RelayOutcome(handled=True, ticket=ticket, note="forwarded provider→guest")


def _handle_guest_message(
    from_phone: str, body: str, wa_message_id: str, raw: dict | None
) -> RelayOutcome:
    guest, _ = Guest.objects.get_or_create(phone=from_phone)
    guest.last_seen = timezone.now()
    guest.save(update_fields=("last_seen",))

    active = (
        Ticket.objects.filter(guest=guest, status__in=[Ticket.Status.OPEN, Ticket.Status.IN_PROGRESS])
        .order_by("-created_at")
        .first()
    )
    if active is not None:
        _persist_inbound(
            ticket=active,
            direction=Message.Direction.GUEST_IN,
            from_phone=from_phone,
            to_phone="",
            body=body,
            wa_message_id=wa_message_id,
            raw=raw,
        )
        prefixed = f"[{active.short_code}] {body}"
        _send_and_log(
            ticket=active,
            direction=Message.Direction.PROVIDER_OUT,
            to_phone=active.provider.phone,
            body=prefixed,
        )
        return RelayOutcome(handled=True, ticket=active, note="forwarded guest→provider (existing ticket)")

    # First contact (or no active ticket) — classify + route.
    cls = classify(body)
    service: Service | None = None
    if cls.service_slug:
        service = Service.objects.filter(slug=cls.service_slug, active=True).first()

    if service is None or service.default_provider is None:
        _persist_inbound(
            ticket=None,
            direction=Message.Direction.GUEST_IN,
            from_phone=from_phone,
            to_phone="",
            body=body,
            wa_message_id=wa_message_id,
            raw=raw,
        )
        _send_and_log(
            ticket=None,
            direction=Message.Direction.SYSTEM_OUT,
            to_phone=from_phone,
            body=_format_guest_no_match(),
        )
        return RelayOutcome(
            handled=True,
            note=f"no service match (slug={cls.service_slug}, backend={cls.backend})",
        )

    ticket = Ticket.objects.create(
        guest=guest,
        provider=service.default_provider,
        service=service,
        location=detect_location(body),
        short_code=Ticket.new_short_code(),
        status=Ticket.Status.OPEN,
        raw_first_message=body,
        extracted_fields=cls.extracted_fields,
        expected_commission_usd=service.expected_commission_usd,
    )
    _persist_inbound(
        ticket=ticket,
        direction=Message.Direction.GUEST_IN,
        from_phone=from_phone,
        to_phone="",
        body=body,
        wa_message_id=wa_message_id,
        raw=raw,
    )
    _send_and_log(
        ticket=ticket,
        direction=Message.Direction.PROVIDER_OUT,
        to_phone=ticket.provider.phone,
        body=_format_provider_intro(ticket, body),
    )
    _send_and_log(
        ticket=ticket,
        direction=Message.Direction.GUEST_OUT,
        to_phone=guest.phone,
        body=_format_guest_ack(ticket),
    )
    return RelayOutcome(handled=True, ticket=ticket, note="new ticket created")
