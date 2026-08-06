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
