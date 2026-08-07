"""Translate admin-entered copy into the guest-facing languages.

The host writes a service name once in English; this fills in the Spanish and
French columns so the landing page reads properly in all three. Anything the
host typed by hand is left alone — only blank fields are filled.

Backed by MyMemory's public translation API over `requests`, which is already a
dependency. No API key is needed and no new package is installed: a missing
dependency has taken this site down once already.

Machine translation of a three-word label is a starting point, not an authority
— "Airport taxi" can come back as "trabajo de taxi de aeropuerto". The fields
stay editable for exactly that reason, and the admin says so when it fills them.
"""

from __future__ import annotations

import html
import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

ENDPOINT = "https://api.mymemory.translated.net/get"
TIMEOUT = 15

LANGUAGE_NAMES = {"es": "Spanish", "fr": "French"}
SOURCE_LANGUAGE = "en"

# MyMemory reports an exhausted quota inside the translated text rather than as
# an HTTP error, so the warning has to be recognised by content.
QUOTA_MARKERS = ("MYMEMORY WARNING", "YOU USED ALL AVAILABLE FREE TRANSLATIONS")


class TranslationError(Exception):
    """Translation could not be produced, with a host-readable reason."""


def _request(text: str, target: str) -> str:
    params = {"q": text, "langpair": f"{SOURCE_LANGUAGE}|{target}"}
    # An email raises MyMemory's anonymous daily character limit; optional.
    if getattr(settings, "MYMEMORY_EMAIL", ""):
        params["de"] = settings.MYMEMORY_EMAIL

    try:
        response = requests.get(ENDPOINT, params=params, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise TranslationError(f"could not reach the translation service ({exc})") from exc
    if response.status_code >= 300:
        raise TranslationError(f"translation service returned HTTP {response.status_code}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise TranslationError("translation service returned a non-JSON response") from exc

    translated = html.unescape(((payload.get("responseData") or {}).get("translatedText") or "").strip())
    if any(marker in translated.upper() for marker in QUOTA_MARKERS):
        raise TranslationError(
            "the free translation quota for today is used up — try again tomorrow, "
            "or set MYMEMORY_EMAIL in .env to raise it"
        )
    if not translated:
        details = payload.get("responseDetails") or "no translation returned"
        raise TranslationError(f"no {LANGUAGE_NAMES.get(target, target)} translation: {details}")
    return translated


def _match_leading_case(source: str, translated: str) -> str:
    """MyMemory often lower-cases the first letter; a card title should not."""
    if source[:1].isupper() and translated[:1].islower():
        return translated[:1].upper() + translated[1:]
    return translated


def translate(text: str, targets: tuple[str, ...] = ("es", "fr")) -> dict[str, str]:
    """Translate one short string. Returns {lang_code: translation}."""
    text = (text or "").strip()
    if not text:
        raise TranslationError("nothing to translate")

    unknown = [t for t in targets if t not in LANGUAGE_NAMES]
    if unknown:
        raise TranslationError(f"unsupported target language(s): {', '.join(unknown)}")

    return {target: _match_leading_case(text, _request(text, target)) for target in targets}


def fill_missing_names(service) -> list[str]:
    """Fill blank name_es / name_fr from name_en. Returns the codes filled.

    Never overwrites a name the host typed — machine translation is a draft,
    and their wording wins.
    """
    targets = tuple(
        code
        for code, value in (("es", service.name_es), ("fr", service.name_fr))
        if not (value or "").strip()
    )
    if not targets or not (service.name_en or "").strip():
        return []

    for code, value in translate(service.name_en, targets).items():
        setattr(service, f"name_{code}", value)
    return list(targets)
