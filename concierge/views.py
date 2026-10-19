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
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from . import paypal, qr, qr_pdf
from .links import LANGUAGE_LABELS, LANGUAGES, destination, normalize_language, place_name
from .models import (
    DamageDeposit,
    Location,
    LocationEvent,
    QrBatch,
    QrTag,
    Service,
    SiteSettings,
    Ticket,
)
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
    return _landing_response(request, location)


def _landing_response(request: HttpRequest, location) -> HttpResponse:
    """Render the guest page for `location`, or the generic page when None.

    Shared by the two things a QR can encode: a venue slug, and a pre-printed
    tag token that resolves to a venue later.
    """
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

    # A location is its own business, so its title/headline/tagline win over the
    # site-wide defaults; blanks fall through so a half-written venue still renders.
    copy = location.branding(site) if location else {f: getattr(site, f, "") for f in Location.BRANDING_FIELDS}

    LocationEvent.objects.create(location=location, kind=LocationEvent.Kind.SCAN)

    return render(
        request,
        "concierge/landing.html",
        {
            "services": services,
            "location": location,
            "place_name": place_name(location),
            "copy": copy,
            "host_name": settings.HOST_NAME,
            "site": site,
            "show_referrals": show_referrals,
            "show_whatsapp_fallback": site.cta_mode == SiteSettings.CtaMode.BOTH,
            # Only disclose when a referral link is actually on the page.
            "any_referral": show_referrals and any(s.uses_referral_link for s in services),
        },
    )


@staff_member_required
def location_qr(request: HttpRequest, slug: str, fmt: str) -> HttpResponse:
    """The venue's QR code, as SVG (for print) or PNG.

    Staff-only: these are not secret, but they are operational assets rather
    than part of the guest-facing site.
    """
    location = get_object_or_404(Location, slug=slug)
    data = qr.payload(request, location)

    try:
        if fmt == "svg":
            body, content_type = qr.svg_bytes(data), "image/svg+xml"
        else:
            body, content_type = qr.png_bytes(data), "image/png"
    except qr.QrUnavailable as exc:
        # A missing optional dependency should read as an instruction, not a 500.
        return HttpResponse(str(exc), status=503, content_type="text/plain")

    response = HttpResponse(body, content_type=content_type)
    # inline so the admin can preview it; the download links add ?download=1
    disposition = "attachment" if request.GET.get("download") else "inline"
    response["Content-Disposition"] = f'{disposition}; filename="qr-{location.slug}.{fmt}"'
    return response


def tag_landing(request: HttpRequest, token: str) -> HttpResponse:
    """A pre-printed card was scanned.

    Assigned to an active venue, this is that venue's page — identical to what
    its /<slug> code would have shown. Unassigned, it is the generic page:
    a guest holding a real card in a real venue gets a working service menu
    rather than an apology, at the cost of the booking carrying no venue
    campaign (so no revenue share is owed to anyone, which is correct — we do
    not yet know whose card it is).

    A deactivated venue is treated the same way, for the same reason.
    """
    tag = get_object_or_404(QrTag, token=token.upper())
    location = tag.location if (tag.location and tag.location.active) else None

    # Staff scanning an unassigned card are here to assign it, not to read the
    # menu; ?preview=1 gets the guest's view of the same card.
    wants_assign = request.GET.get("assign") or (location is None and not request.GET.get("preview"))
    if request.user.is_staff and wants_assign:
        return _assign_screen(request, tag)

    return _landing_response(request, location)


@staff_member_required
def tag_assign(request: HttpRequest, token: str) -> HttpResponse:
    """Point a printed card at a venue, or release it back to the pile."""
    tag = get_object_or_404(QrTag, token=token.upper())

    if request.method != "POST":
        return _assign_screen(request, tag)

    raw = (request.POST.get("location") or "").strip()
    if raw:
        tag.location = get_object_or_404(Location, pk=raw)
    else:
        tag.location = None
    tag.save()

    return HttpResponseRedirect(f"{tag.path()}?assigned=1")


def _assign_screen(request: HttpRequest, tag: QrTag) -> HttpResponse:
    return render(
        request,
        "concierge/qr_assign.html",
        {
            "tag": tag,
            "locations": Location.objects.filter(active=True),
            "just_assigned": bool(request.GET.get("assigned")),
        },
    )


@staff_member_required
def tag_qr(request: HttpRequest, token: str, fmt: str) -> HttpResponse:
    """One pre-printed card's code, as SVG or PNG.

    The batch PDF is a finished sheet; this is the raw artwork for dropping a
    single card into a card template elsewhere. SVG for that — it stays sharp
    at whatever size the template places it.
    """
    tag = get_object_or_404(QrTag, token=token.upper())
    data = request.build_absolute_uri(tag.path())

    try:
        if fmt == "svg":
            body, content_type = qr.svg_bytes(data), "image/svg+xml"
        else:
            body, content_type = qr.png_bytes(data), "image/png"
    except qr.QrUnavailable as exc:
        return HttpResponse(str(exc), status=503, content_type="text/plain")

    response = HttpResponse(body, content_type=content_type)
    disposition = "attachment" if request.GET.get("download") else "inline"
    response["Content-Disposition"] = f'{disposition}; filename="qr-{tag.token}.{fmt}"'
    return response


@staff_member_required
def qr_batch_pdf(request: HttpRequest, pk: int) -> HttpResponse:
    """The whole batch as an A4 PDF, ready to print and guillotine."""
    batch = get_object_or_404(QrBatch, pk=pk)
    cards = [
        (tag.token, request.build_absolute_uri(tag.path()))
        for tag in batch.tags.order_by("token")
    ]

    try:
        body = qr_pdf.batch_pdf_bytes(
            cards,
            title=f"QR cards — {batch.label}",
            caption=request.get_host(),
        )
    except qr_pdf.PdfUnavailable as exc:
        # As with the QR images: a missing optional dependency should read as
        # an instruction, not a 500.
        return HttpResponse(str(exc), status=503, content_type="text/plain")

    response = HttpResponse(body, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="qr-batch-{batch.pk}.pdf"'
    return response


def privacy(request: HttpRequest) -> HttpResponse:
    """Public privacy policy.

    Meta requires a reachable privacy policy URL before an app can be published,
    and an unpublished app receives no production webhooks at all.
    """
    return render(
        request,
        "concierge/privacy.html",
        {
            # Deliberately no personal name and no phone number: the policy
            # speaks for the service, not for an individual, and the WhatsApp
            # number is the host's personal line. It is not passed to the
            # template at all so it cannot reach the page source either.
            "contact_email": settings.PRIVACY_CONTACT_EMAIL,
            "last_updated": "7 August 2026",
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


# --- damage deposits ------------------------------------------------------
#
# Three views, all keyed on the deposit's secret token: the page the guest
# opens, and the two JSON endpoints PayPal's button calls. No card details
# ever reach this server — the guest approves inside PayPal's own popup and
# all that comes back is an order id to authorize.

# Statuses from which a guest may still place a hold. Anything else means the
# deposit is already settled, and a second hold would be a second reservation
# against their card.
_DEPOSIT_OPEN = (
    DamageDeposit.Status.DRAFT,
    DamageDeposit.Status.SENT,
    DamageDeposit.Status.FAILED,
)


def _deposit_terms(site: SiteSettings, lang: str) -> str:
    """This site's deposit wording, falling back to English when unwritten."""
    return getattr(site, f"deposit_terms_{lang}", "") or site.deposit_terms_en


def deposit(request: HttpRequest, token: str) -> HttpResponse:
    """The page the guest opens from the link pasted into Booking.com.

    Public and unauthenticated — the token is the only thing guarding it,
    which is why it is long and random. It shows the guest their own name and
    booking reference so they can tell a real link from a phishing one.
    """
    record = get_object_or_404(DamageDeposit, token=token)
    site = SiteSettings.load()
    # The guest's own choice wins; otherwise the language the host recorded for
    # them, so a link pasted into Booking.com opens already translated without
    # the host having to remember to append ?lang=.
    lang = normalize_language(request.GET.get("lang") or record.language)

    return render(
        request,
        "concierge/deposit.html",
        {
            "deposit": record,
            "site": site,
            "lang": lang,
            "terms": _deposit_terms(site, lang),
            "languages": LANGUAGE_LABELS,
            "host_name": settings.HOST_NAME,
            "can_pay": record.status in _DEPOSIT_OPEN and paypal.is_configured(),
            "already_held": record.status == DamageDeposit.Status.HELD,
            "paypal_client_id": settings.PAYPAL_CLIENT_ID,
            # Shown as a banner so a test link is never mistaken for a real one.
            "sandbox": not paypal.is_live(),
            "honor_days": paypal.HONOR_PERIOD_DAYS,
        },
    )


@require_http_methods(["POST"])
def deposit_create_order(request: HttpRequest, token: str) -> HttpResponse:
    """Called by the PayPal button to get an order id. Holds nothing yet."""
    record = get_object_or_404(DamageDeposit, token=token)
    if record.status not in _DEPOSIT_OPEN:
        return JsonResponse(
            {"error": f"This deposit is already {record.get_status_display().split(' — ')[0].lower()}."},
            status=409,
        )
    if record.amount <= 0:
        return JsonResponse({"error": "This deposit has no amount set."}, status=400)

    reference = record.booking_reference or f"deposit-{record.pk}"
    try:
        order = paypal.create_order(
            amount=record.amount,
            currency=record.currency,
            reference=reference,
            description=f"Refundable damage deposit — {record.guest_name}",
            brand_name=record.location.name if record.location else settings.HOST_NAME,
        )
    except paypal.PayPalError as exc:
        return _deposit_failure(record, exc, stage="create_order")

    record.paypal_order_id = order.get("id", "")
    record.environment = settings.PAYPAL_ENV
    record.last_error = ""
    record.save(update_fields=("paypal_order_id", "environment", "last_error"))
    return JsonResponse({"id": record.paypal_order_id})


@require_http_methods(["POST"])
def deposit_authorize(request: HttpRequest, token: str) -> HttpResponse:
    """Place the hold, once the guest has approved the order in PayPal.

    The order id is read from the stored record rather than from the request
    body: the body is attacker-controlled, and authorizing an arbitrary order
    id would attach someone else's payment to this deposit.
    """
    record = get_object_or_404(DamageDeposit, token=token)
    if record.status == DamageDeposit.Status.HELD:
        # The guest double-tapped, or PayPal retried. Report the existing hold
        # rather than placing a second one against their card.
        return JsonResponse({"status": "held", "amount": str(record.amount)})
    if record.status not in _DEPOSIT_OPEN:
        return JsonResponse({"error": "This deposit is no longer open."}, status=409)
    if not record.paypal_order_id:
        return JsonResponse({"error": "No PayPal order to authorize."}, status=400)

    try:
        authorization = paypal.authorize_order(record.paypal_order_id)
    except paypal.PayPalError as exc:
        return _deposit_failure(record, exc, stage="authorize")

    record.paypal_authorization_id = authorization.id
    record.expires_at = authorization.expires_at
    record.authorized_at = timezone.now()
    record.status = DamageDeposit.Status.HELD
    # Stamped here, not at creation. The record is created before anyone knows
    # which PayPal will be called, and PAYPAL_ENV can change in between — a
    # deposit created under sandbox and approved after a switch to live holds
    # real money while reading "sandbox", which is the one lie this field must
    # never tell.
    record.environment = settings.PAYPAL_ENV
    record.last_error = ""
    record.save(update_fields=(
        "paypal_authorization_id", "expires_at", "authorized_at", "status",
        "environment", "last_error",
    ))
    logger.info(
        "Deposit %s held: %s %s, authorization %s expiring %s",
        record.pk, record.currency, record.amount, authorization.id, record.expires_at,
    )
    return JsonResponse({"status": "held", "amount": str(record.amount)})


def _deposit_failure(record: DamageDeposit, exc: "paypal.PayPalError", *, stage: str) -> JsonResponse:
    """Record why a hold could not be placed, and tell the guest plainly.

    The stored message is for the host — it carries PayPal's debug id, which is
    the first thing their support asks for. The guest gets a generic line,
    because PayPal's own wording leaks why a card was declined.
    """
    record.status = DamageDeposit.Status.FAILED
    record.last_error = f"{stage}: {exc}" + (f" (debug id {exc.debug_id})" if exc.debug_id else "")
    record.save(update_fields=("status", "last_error"))
    logger.error("Deposit %s failed at %s: %s", record.pk, stage, exc)
    return JsonResponse(
        {"error": "PayPal could not complete the hold. Please try again or contact your host."},
        status=502,
    )
