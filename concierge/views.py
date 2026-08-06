"""HTTP views: WhatsApp webhook + the QR landing page."""

from __future__ import annotations

import json
import logging
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.http import (
    HttpRequest,
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseRedirect,
    JsonResponse,
)
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .links import LANGUAGES, destination, place_name
from .models import Location, LocationEvent, Service, SiteSettings, Ticket
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


def _tracked(service_slug: str, location, lang: str = "", *, whatsapp: bool = False) -> str:
    """URL of the counting redirect for one card."""
    query = {}
    if location is not None:
        query["at"] = location.slug
    if lang:
        query["lang"] = lang
    if whatsapp:
        query["ch"] = "wa"
    url = reverse("go", args=[service_slug])
    return f"{url}?{urlencode(query)}" if query else url


def go(request: HttpRequest, service_slug: str) -> HttpResponse:
    """Count the click, then forward to wherever the card points.

    A server-side hop rather than a click beacon: these numbers may settle a
    revenue share with a venue, so they should not depend on the guest's
    browser cooperating. Affiliate attribution is unaffected — the destination
    keeps its tracking parameters.
    """
    service = get_object_or_404(Service, slug=service_slug, active=True)
    location = Location.objects.filter(slug=request.GET.get("at", ""), active=True).first()
    lang = request.GET.get("lang", "en")
    lang = lang if lang in LANGUAGES else "en"
    force_whatsapp = request.GET.get("ch") == "wa"

    site = SiteSettings.load()
    business_number = (settings.WHATSAPP_BUSINESS_NUMBER or "").lstrip("+")
    target = destination(
        service, location, site,
        lang=lang, force_whatsapp=force_whatsapp, business_number=business_number,
    )

    LocationEvent.objects.create(
        location=location,
        kind=LocationEvent.Kind.CLICK,
        service=service,
        channel="whatsapp" if force_whatsapp or "wa.me" in target else service.channel,
    )
    # 302: the destination is configuration and can change at any time, so
    # nothing about this redirect should be cached.
    return HttpResponseRedirect(target)


def landing(request: HttpRequest, slug: str = "") -> HttpResponse:
    """The QR target. With a slug it is one venue's page; bare "/" is generic."""
    location = None
    if slug:
        location = get_object_or_404(Location, slug=slug, active=True)

    services = list(location.visible_services() if location else Service.objects.filter(active=True))
    site = SiteSettings.load()

    # Each service picks its own channel; cta_mode is the site-wide switch over
    # them, whose first option routes every card to WhatsApp. A referral channel
    # with no URL still falls back to WhatsApp, so a card is never a dead end
    # while the links are being filled in.
    show_referrals = site.cta_mode in (SiteSettings.CtaMode.REFERRAL, SiteSettings.CtaMode.BOTH)

    # Every outbound link goes through the counting redirect, which resolves the
    # real destination — including the Viator affiliate parameters and this
    # venue's campaign code.
    for service in services:
        service.tracked = {
            lang: _tracked(service.slug, location, lang) for lang in LANGUAGES
        }
        service.tracked_whatsapp = {
            lang: _tracked(service.slug, location, lang, whatsapp=True) for lang in LANGUAGES
        }

    LocationEvent.objects.create(location=location, kind=LocationEvent.Kind.SCAN)

    return render(
        request,
        "concierge/landing.html",
        {
            "services": services,
            "location": location,
            "place_name": place_name(location),
            "host_name": settings.HOST_NAME,
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
            "last_updated": "6 August 2026",
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
