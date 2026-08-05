"""Translate admin-entered copy into the guest-facing languages.

The host writes a service name once in English; this fills in the Spanish and
French columns so the landing page reads properly in all three. Anything the
host typed by hand is left alone — only blank fields are filled.

Uses forced tool use for structured output rather than `output_config.format`,
matching `classifier.py` and keeping the call working on the older `anthropic`
release pinned for Python 3.9 on the shared host.
"""

from __future__ import annotations

import logging

from django.conf import settings

logger = logging.getLogger(__name__)

TRANSLATION_MODEL = "claude-opus-5"

LANGUAGE_NAMES = {"es": "Spanish", "fr": "French"}


class TranslationError(Exception):
    """Translation could not be produced, with a host-readable reason."""


def translate(text: str, targets: tuple[str, ...] = ("es", "fr"), *, context: str = "") -> dict[str, str]:
    """Translate one short string. Returns {lang_code: translation}.

    `context` tells the model what kind of string this is, which matters for
    short fragments: "Saona" alone is ambiguous, "a service on a holiday
    rental's landing page" is not.
    """
    text = (text or "").strip()
    if not text:
        raise TranslationError("nothing to translate")

    unknown = [t for t in targets if t not in LANGUAGE_NAMES]
    if unknown:
        raise TranslationError(f"unsupported target language(s): {', '.join(unknown)}")
    if not settings.ANTHROPIC_API_KEY:
        raise TranslationError("ANTHROPIC_API_KEY is not set")

    import anthropic

    schema = {
        "type": "object",
        "properties": {
            code: {
                "type": "string",
                "description": f"The {LANGUAGE_NAMES[code]} translation.",
            }
            for code in targets
        },
        "required": list(targets),
    }

    system = (
        "You translate short user-facing labels for the landing page of a holiday "
        "rental in Bayahibe, Dominican Republic — the names of local services "
        "guests can request, such as excursions, taxis and food delivery.\n\n"
        "Translate the meaning, not the words. Keep it the length of a button or "
        "card title. Preserve proper nouns (place names, operator names) exactly "
        "as written. Do not add punctuation, quotes, or explanation."
    )
    user = f"{context}\n\nTranslate:\n{text}" if context else f"Translate:\n{text}"

    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    try:
        resp = client.messages.create(
            model=TRANSLATION_MODEL,
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": user}],
            tools=[
                {
                    "name": "record_translations",
                    "description": "Record the translated label.",
                    "input_schema": schema,
                }
            ],
            tool_choice={"type": "tool", "name": "record_translations"},
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to the host, never fatal
        raise TranslationError(f"{type(exc).__name__}: {exc}") from exc

    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "record_translations":
            out = {code: (block.input.get(code) or "").strip() for code in targets}
            missing = [code for code, value in out.items() if not value]
            if missing:
                raise TranslationError(f"no translation returned for: {', '.join(missing)}")
            return out

    raise TranslationError("the model returned no translation")


def fill_missing_names(service) -> list[str]:
    """Fill blank name_es / name_fr from name_en. Returns the codes filled.

    Never overwrites a name the host typed — a translation is a starting point,
    and their wording wins.
    """
    targets = tuple(
        code
        for code, value in (("es", service.name_es), ("fr", service.name_fr))
        if not (value or "").strip()
    )
    if not targets or not (service.name_en or "").strip():
        return []

    translations = translate(
        service.name_en,
        targets,
        context="This is the name of a service on a holiday rental's landing page.",
    )
    for code, value in translations.items():
        setattr(service, f"name_{code}", value)
    return list(targets)
