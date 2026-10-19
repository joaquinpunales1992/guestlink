"""Where a service card sends a guest, and the pre-filled message it carries.

Built here rather than in the template because the tracked redirect has to
reproduce exactly the same destination the card advertises — the link the guest
sees and the link they land on must not drift apart.
"""

from __future__ import annotations

from urllib.parse import quote

from django.conf import settings

from .affiliate import viator_url

LANGUAGES = ("en", "es", "fr")

# What each language is called in itself, for the admin's dropdown — a host
# picking the guest's language should read "Français", not "FR".
LANGUAGE_LABELS = (
    ("en", "English"),
    ("es", "Español"),
    ("fr", "Français"),
)


def normalize_language(value: str) -> str:
    """Coerce anything to a language this site actually renders.

    One place, because the deposit page takes a language from three sources —
    the URL, the stored record, and the default — and a code that slips
    through unvalidated renders a page with every translated string missing.
    """
    value = (value or "").strip().lower()
    # "es-419", "fr-CA" and "en-GB" all arrive from real browsers and booking
    # systems; the base language is what we have copy for.
    base = value.split("-")[0]
    return base if base in LANGUAGES else "en"


# "Staying at" reads right for somewhere you sleep and wrong for a supermarket.
_STAYING_KINDS = {"apartment", "hotel", "residence"}

_TEMPLATES = {
    "en": ("Hi! I'm staying at {place}. I'd like info about {service}.",
           "Hi! I'm at {place}. I'd like info about {service}."),
    "es": ("¡Hola! Me alojo en {place}. Quisiera info sobre {service}.",
           "¡Hola! Estoy en {place}. Quisiera info sobre {service}."),
    "fr": ("Bonjour ! Je séjourne à {place}. Je voudrais des informations sur {service}.",
           "Bonjour ! Je suis à {place}. Je voudrais des informations sur {service}."),
}


def place_name(location) -> str:
    """What the guest calls where they are."""
    if location is not None:
        return location.name
    # No location: the original single-apartment wording.
    return f"Apto {settings.HOST_APARTMENT_LABEL}"


def service_name(service, lang: str) -> str:
    return {
        "en": service.name_en,
        "es": service.display_name_es,
        "fr": service.display_name_fr,
    }.get(lang, service.name_en)


def whatsapp_message(service, location, lang: str) -> str:
    staying, visiting = _TEMPLATES.get(lang, _TEMPLATES["en"])
    kind = getattr(location, "kind", None)
    template = staying if (location is None or kind in _STAYING_KINDS) else visiting
    return template.format(place=place_name(location), service=service_name(service, lang))


def whatsapp_url(service, location, lang: str, business_number: str) -> str:
    text = quote(whatsapp_message(service, location, lang))
    return f"https://wa.me/{business_number}?text={text}"


def referral_destination(service, location, site) -> str:
    """The booking link, with Viator affiliate parameters applied.

    A location's campaign code wins over the site-wide one so bookings can be
    attributed to the venue whose QR produced them.
    """
    campaign = (location.viator_campaign if location is not None else "") or site.viator_campaign
    return viator_url(
        service.referral_url,
        pid=site.viator_partner_id,
        mcid=site.viator_mcid,
        campaign=campaign,
    )


def destination(service, location, site, *, lang: str, force_whatsapp: bool, business_number: str) -> str:
    """Resolve where a card actually points, honouring every fallback.

    Mirrors the template's decision so the redirect can't send a guest
    somewhere other than the button promised.
    """
    from .models import SiteSettings

    referrals_on = site.cta_mode in (SiteSettings.CtaMode.REFERRAL, SiteSettings.CtaMode.BOTH)
    if not force_whatsapp and referrals_on and service.uses_referral_link:
        return referral_destination(service, location, site)
    return whatsapp_url(service, location, lang, business_number)


# --- damage deposits ------------------------------------------------------

_DEPOSIT_TEMPLATES = {
    "en": (
        "Hi {guest}, this is {host}. Before check-in we hold a refundable "
        "{currency} {amount} security deposit. Your card is not charged — PayPal "
        "only reserves it, and we release it after check-out: {url}"
    ),
    "es": (
        "Hola {guest}, soy {host}. Antes de la entrada retenemos un depósito de "
        "garantía reembolsable de {currency} {amount}. No se cobra nada a tu "
        "tarjeta — PayPal solo lo reserva, y lo liberamos tras la salida: {url}"
    ),
    "fr": (
        "Bonjour {guest}, ici {host}. Avant l'arrivée nous bloquons une caution "
        "remboursable de {currency} {amount}. Votre carte n'est pas débitée — "
        "PayPal ne fait que la réserver, et nous la libérons après le départ : {url}"
    ),
}


def deposit_message(deposit, url: str, host_name: str) -> str:
    """The WhatsApp text asking a guest for their deposit, in their language.

    Lives here with the other guest-facing copy rather than in the admin, so
    the wording a guest receives is not buried in a view.
    """
    lang = normalize_language(getattr(deposit, "language", ""))
    return _DEPOSIT_TEMPLATES[lang].format(
        guest=deposit.guest_name,
        host=host_name,
        currency=deposit.currency,
        amount=f"{deposit.amount:.2f}",
        url=url,
    )
