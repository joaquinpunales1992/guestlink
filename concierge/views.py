"""HTTP views: WhatsApp webhook + the QR landing page."""

from __future__ import annotations

import json
import logging

from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.http import HttpRequest, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .affiliate import viator_url
from .models import Service, SiteSettings, Ticket
from .relay import handle_inbound

logger = logging.getLogger(__name__)


@csrf_exempt
@require_http_methods(["GET", "POST"])
def whatsapp_webhook(request: HttpRequest) -> HttpResponse:
    if request.method == "GET":
        return _handle_verification(request)
    return _handle_event(request)


def _handle_verification(request: HttpRequest) -> HttpResponse:
    mode = request.GET.get("hub.mode")
    token = request.GET.get("hub.verify_token")
    challenge = request.GET.get("hub.challenge", "")
    if mode == "subscribe" and token and token == settings.WHATSAPP_VERIFY_TOKEN:
        return HttpResponse(challenge, content_type="text/plain")
    return HttpResponseBadRequest("verification failed")


def _handle_event(request: HttpRequest) -> HttpResponse:
    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return HttpResponseBadRequest("invalid json")

    if payload.get("object") != "whatsapp_business_account":
        # Meta sometimes sends test pings; always 200 so they don't retry forever.
        return JsonResponse({"ignored": True})

    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            for msg in value.get("messages") or []:
                _process_message(msg, raw=payload)
            # value.statuses (delivered/read receipts) ignored for v1
    return JsonResponse({"ok": True})


def _process_message(msg: dict, *, raw: dict) -> None:
    msg_type = msg.get("type")
    if msg_type != "text":
        logger.info("Ignoring non-text message of type=%s", msg_type)
        return
    from_phone = msg.get("from", "")
    body = (msg.get("text") or {}).get("body", "")
    wa_id = msg.get("id", "")
    try:
        outcome = handle_inbound(
            from_phone=f"+{from_phone}" if from_phone and not from_phone.startswith("+") else from_phone,
            body=body,
            wa_message_id=wa_id,
            raw=raw,
        )
        if outcome.delivery_failed:
            # Persisted but undelivered. Meta still gets a 200 (a retry would
            # duplicate the ticket, not fix the send), so this log line and the
            # admin's delivery filter are the only signals.
            logger.error("Relay handled but DELIVERY FAILED: %s", outcome)
        else:
            logger.info("Relay outcome: %s", outcome)
    except Exception:
        logger.exception("Relay failed for wa_id=%s", wa_id)


def landing(request: HttpRequest) -> HttpResponse:
    services = list(Service.objects.filter(active=True))
    site = SiteSettings.load()
    business_number = (settings.WHATSAPP_BUSINESS_NUMBER or "").lstrip("+") or "REPLACE_WITH_E164_DIGITS"

    # Each service picks its own channel; cta_mode is the site-wide switch over
    # them, whose first option routes every card to WhatsApp. A referral channel
    # with no URL still falls back to WhatsApp, so a card is never a dead end
    # while the links are being filled in.
    show_referrals = site.cta_mode in (SiteSettings.CtaMode.REFERRAL, SiteSettings.CtaMode.BOTH)

    # Viator attributes bookings from query parameters on any viator.com URL, so
    # the host pastes a plain product link and the affiliate id is stamped on
    # here — one setting instead of a hand-built link per service. Resolved in
    # the view rather than as a model property so SiteSettings is read once for
    # the page, not once per card.
    for service in services:
        service.resolved_url = viator_url(
            service.referral_url,
            pid=site.viator_partner_id,
            mcid=site.viator_mcid,
            campaign=site.viator_campaign,
        )
    return render(
        request,
        "concierge/landing.html",
        {
            "services": services,
            "business_number": business_number,
            "host_name": settings.HOST_NAME,
            "apartment_label": settings.HOST_APARTMENT_LABEL,
            "site": site,
            "show_referrals": show_referrals,
            "show_whatsapp_fallback": site.cta_mode == SiteSettings.CtaMode.BOTH,
            # Only disclose when a referral link is actually on the page.
            "any_referral": show_referrals and any(s.uses_referral_link for s in services),
        },
    )


def privacy(request: HttpRequest) -> HttpResponse:
    """Public privacy policy.

    Meta requires a reachable privacy policy URL before an app can be published,
    and an unpublished app receives no production webhooks at all.
    """
    return render(
        request,
        "concierge/privacy.html",
        {
            "host_name": settings.HOST_NAME,
            "business_number": (settings.WHATSAPP_BUSINESS_NUMBER or "").lstrip("+"),
            "contact_email": settings.PRIVACY_CONTACT_EMAIL,
            "last_updated": "5 August 2026",
        },
    )


def healthz(request: HttpRequest) -> HttpResponse:
    return JsonResponse({"ok": True, "webhook": reverse("whatsapp_webhook")})


@staff_member_required
def ticket_thread(request: HttpRequest, short_code: str) -> HttpResponse:
    ticket = get_object_or_404(Ticket, short_code=short_code.upper())
    return render(
        request,
        "concierge/ticket_thread.html",
        {"ticket": ticket, "messages": ticket.messages.order_by("created_at")},
    )
