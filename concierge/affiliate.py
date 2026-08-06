"""Turn a plain Viator product URL into an affiliate link.

Viator attributes a booking from query parameters appended to any viator.com
URL, so the host can paste a normal product page and let this add the tracking:

    https://www.viator.com/tours/…/d5021-123P4
    → …/d5021-123P4?pid=P00012345&mcid=42383&medium=link

Existing parameters are never touched. Viator's own guidance is that a booking
cannot be paid out if `pid` or `mcid` is modified or removed, so a link the
host pasted with its own tracking already on it is left exactly as-is — only
missing parameters get filled in.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# Viator's tracking id for the "link" medium, as used in their documented
# examples. Configurable because it is theirs to change, not ours.
DEFAULT_MCID = "42383"
DEFAULT_MEDIUM = "link"

# Viator: "only include alphanumeric characters and dashes — special characters
# may break tracking and attribution entirely."
CAMPAIGN_RE = re.compile(r"^[A-Za-z0-9-]*$")


def is_viator(url: str) -> bool:
    """Match viator.com and its subdomains — and nothing that merely contains it.

    A substring check would treat `viator.com.evil.example` as Viator and stamp
    the host's affiliate id onto someone else's domain.
    """
    host = urlparse(url or "").netloc.lower().split("@")[-1].split(":")[0]
    return host == "viator.com" or host.endswith(".viator.com")


def viator_url(url: str, *, pid: str, mcid: str = "", campaign: str = "") -> str:
    """Return `url` with Viator affiliate parameters added where missing.

    Returns the URL unchanged when there is no `pid` configured, when it isn't
    a viator.com link, or when tracking is already present.
    """
    pid = (pid or "").strip()
    if not pid or not url or not is_viator(url):
        return url

    parsed = urlparse(url)
    params = parse_qsl(parsed.query, keep_blank_values=True)
    present = {key.lower() for key, _ in params}

    additions = {
        "pid": pid,
        "mcid": (mcid or "").strip() or DEFAULT_MCID,
        "medium": DEFAULT_MEDIUM,
    }
    if campaign.strip():
        additions["campaign"] = campaign.strip()

    for key, value in additions.items():
        if key not in present:
            params.append((key, value))

    return urlunparse(parsed._replace(query=urlencode(params)))
