"""Pull a card image from a service's referral link.

Airbnb's `/rp/<user>` referral URLs are a JS shim with no Open Graph tags, but
they carry `listing_id`, so the canonical `/experiences/<id>` page — which does
serve og:image server-side — can be resolved from them.

Nothing here is Airbnb-specific beyond that one hop: any destination exposing
og:image works, so a Viator or GetYourGuide link would behave the same.

The image is downloaded, downscaled and stored in MEDIA_ROOT rather than
hotlinked. CDN URLs rotate, and a card whose photo silently 404s months later
is worse than one with no photo.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from io import BytesIO
from urllib.parse import parse_qs, urlparse

import requests
from django.core.files.base import ContentFile

USER_AGENT = "Mozilla/5.0 (compatible; guestlink/1.0; +https://bookyourtickets.online)"
TIMEOUT = 20
MAX_IMAGE_BYTES = 8 * 1024 * 1024
# The card banner is small and guests are on phone data; the source is often
# 4000px wide.
TARGET_WIDTH = 1200
JPEG_QUALITY = 82

_META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r'([\w:-]+)\s*=\s*"([^"]*)"', re.IGNORECASE)
_EXPERIENCE_PATH_RE = re.compile(r"/experiences/(\d+)")


class PreviewError(Exception):
    """Anything that stops us producing an image, with a host-readable reason."""


@dataclass
class Preview:
    content: ContentFile
    title: str
    source_url: str


def og_value(html: str, prop: str) -> str | None:
    """Read one Open Graph property.

    Matches the property exactly so `og:image` does not pick up
    `og:image:width`, and accepts either `property=` or `name=` since sites
    disagree about which is correct.
    """
    for tag in _META_TAG_RE.findall(html):
        attrs = {k.lower(): v for k, v in _ATTR_RE.findall(tag)}
        if prop in (attrs.get("property", "").lower(), attrs.get("name", "").lower()):
            content = attrs.get("content", "").strip()
            if content:
                return content
    return None


def canonical_page_url(referral_url: str) -> str:
    """The page whose preview we want, given a referral link.

    Airbnb referral links point at a redirect shim, so prefer the experience id
    they carry. Anything else is fetched as given.
    """
    parsed = urlparse(referral_url)
    listing_id = parse_qs(parsed.query).get("listing_id", [None])[0]
    if listing_id and listing_id.isdigit():
        return f"https://www.airbnb.com/experiences/{listing_id}"
    match = _EXPERIENCE_PATH_RE.search(parsed.path)
    if match:
        return f"https://www.airbnb.com/experiences/{match.group(1)}"
    return referral_url


def points_at_a_listing(referral_url: str) -> bool:
    """False for an Airbnb link that resolves to a search page, not one listing.

    Airbnb's Share button appears on search results too, and the link it copies
    there carries `federatedSearchId` / `searchId` instead of `listing_id`. It
    is a valid referral link — it just dumps the guest on a list of everything
    in Bayahibe rather than the trip whose card they tapped, which is easy to
    do and impossible to spot by eye.
    """
    parsed = urlparse(referral_url)
    if "airbnb." not in parsed.netloc.lower():
        return True  # not ours to judge
    if _EXPERIENCE_PATH_RE.search(parsed.path):
        return True
    return "listing_id" in parse_qs(parsed.query)


def fetch_preview(referral_url: str) -> Preview:
    """Download and downscale the destination's preview image."""
    page_url = canonical_page_url(referral_url)
    headers = {"User-Agent": USER_AGENT}

    try:
        page = requests.get(page_url, headers=headers, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise PreviewError(f"could not load {page_url} ({exc})") from exc
    if page.status_code in (401, 403, 429):
        # Viator sits behind bot protection and answers any server-side fetch
        # with a challenge page. Nothing to fix in the URL — say so, rather than
        # leaving the host to decode an HTTP code.
        raise PreviewError(
            f"{urlparse(page_url).netloc} blocks automated fetches (HTTP {page.status_code}), "
            "so the image and title cannot be read from the page. Add them by hand."
        )
    if page.status_code >= 300:
        raise PreviewError(f"{page_url} returned HTTP {page.status_code}")

    image_url = og_value(page.text, "og:image")
    if not image_url:
        raise PreviewError(f"no og:image on {page_url}")
    title = og_value(page.text, "og:title") or ""

    try:
        resp = requests.get(image_url, headers=headers, timeout=TIMEOUT, stream=True)
    except requests.RequestException as exc:
        raise PreviewError(f"could not download the image ({exc})") from exc
    if resp.status_code >= 300:
        raise PreviewError(f"image download returned HTTP {resp.status_code}")

    raw = b""
    for chunk in resp.iter_content(64 * 1024):
        raw += chunk
        if len(raw) > MAX_IMAGE_BYTES:
            raise PreviewError("image is larger than 8 MB — refusing to store it")
    if not raw:
        raise PreviewError("image download was empty")

    return Preview(content=_to_jpeg(raw), title=title, source_url=image_url)


def _to_jpeg(raw: bytes) -> ContentFile:
    """Downscale to TARGET_WIDTH and normalise to JPEG."""
    from PIL import Image  # imported late: only this path needs Pillow

    try:
        img = Image.open(BytesIO(raw))
        img.load()
    except Exception as exc:  # noqa: BLE001 - Pillow raises a wide variety
        raise PreviewError(f"downloaded file is not a readable image ({exc})") from exc

    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    if img.width > TARGET_WIDTH:
        height = round(img.height * TARGET_WIDTH / img.width)
        img = img.resize((TARGET_WIDTH, height), Image.LANCZOS)

    buf = BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return ContentFile(buf.getvalue())
