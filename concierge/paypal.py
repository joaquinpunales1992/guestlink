"""Thin wrapper around the PayPal Orders v2 / Payments v2 APIs.

Only the authorization lifecycle is covered, because that is the only thing a
damage deposit needs: put a hold on the guest's funds, then either let it go or
take part of it.

    create_order()      intent=AUTHORIZE, returns the id the JS SDK approves
    authorize_order()   after approval — this is what places the hold
    void()              checkout was clean; release the money
    capture()           something was broken; take up to the held amount
    reauthorize()       the stay outran the 3-day honor period

Deliberately no dry-run switch, unlike whatsapp.py. A fabricated hold would
read as "held" in the admin while the guest's bank knows nothing about it, and
the host would find out only once there was damage to charge for. Point
PAYPAL_ENV at `sandbox` instead: same code path, same API, test money.

PayPal's own timings, which the rest of the feature is shaped around:

* The hold is **guaranteed capturable for 3 days** (the honor period) and
  **expires entirely at 29 days**.
* Between those, capture may still work but is not guaranteed; `reauthorize`
  mints a fresh authorization id and restarts the honor period, within the
  original 29-day window.
* A capture may exceed the authorized amount by at most 15% or $75, whichever
  is less. We never rely on that — the amount held is the ceiling we quote the
  guest, and `capture()` refuses to go above it.
"""

from __future__ import annotations

import datetime
import decimal
import logging
import secrets
import threading
from dataclasses import dataclass, field

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

LIVE_BASE = "https://api-m.paypal.com"
SANDBOX_BASE = "https://api-m.sandbox.paypal.com"

# PayPal's published windows, in days from the moment of authorization.
HONOR_PERIOD_DAYS = 3
VALIDITY_PERIOD_DAYS = 29

TIMEOUT = 20


class PayPalError(Exception):
    """Any non-2xx from PayPal, or a missing configuration.

    `debug_id` is PayPal's own correlation id — it is the first thing their
    support asks for, so it is carried through to the admin rather than only
    into the log.
    """

    def __init__(self, message: str, *, debug_id: str = "", status: int = 0):
        super().__init__(message)
        self.debug_id = debug_id
        self.status = status


@dataclass
class Authorization:
    """The parts of a PayPal authorization this app stores or shows."""

    id: str
    status: str
    amount: decimal.Decimal
    currency: str
    expires_at: "datetime.datetime | None" = None
    raw: dict = field(default_factory=dict)


def is_configured() -> bool:
    return bool(settings.PAYPAL_CLIENT_ID and settings.PAYPAL_CLIENT_SECRET)


def is_live() -> bool:
    return settings.PAYPAL_ENV == "live"


def api_base() -> str:
    return LIVE_BASE if is_live() else SANDBOX_BASE


def money(amount, currency: str = "USD") -> dict:
    """PayPal's amount object. Values are strings with exactly two decimals."""
    quantized = decimal.Decimal(amount).quantize(
        decimal.Decimal("0.01"), rounding=decimal.ROUND_HALF_UP
    )
    return {"currency_code": currency.upper(), "value": f"{quantized:.2f}"}


# --- auth ----------------------------------------------------------------

# Access tokens last ~9 hours, so minting one per API call would be several
# round trips per page. Cached per process, with the lock because Passenger
# runs threaded workers and two of them racing would mint two tokens — not
# harmful, just wasteful, and the lock is uncontended in the normal case.
_token_lock = threading.Lock()
_token_cache: dict = {"value": "", "expires_at": None, "key": ""}


def _credentials_key() -> str:
    """Identifies the credentials a cached token was minted for.

    Without this, flipping PAYPAL_ENV from sandbox to live (or rotating the
    secret) would keep serving a token the new environment rejects until the
    process restarts.
    """
    return f"{settings.PAYPAL_ENV}:{settings.PAYPAL_CLIENT_ID}"


def _access_token(*, force_refresh: bool = False) -> str:
    if not is_configured():
        raise PayPalError(
            "PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET are unset — add them to .env. "
            "Until then, damage deposits cannot be created."
        )

    now = datetime.datetime.now(datetime.timezone.utc)
    key = _credentials_key()
    with _token_lock:
        fresh_enough = (
            not force_refresh
            and _token_cache["value"]
            and _token_cache["key"] == key
            and _token_cache["expires_at"]
            and _token_cache["expires_at"] > now
        )
        if fresh_enough:
            return _token_cache["value"]

        response = requests.post(
            f"{api_base()}/v1/oauth2/token",
            auth=(settings.PAYPAL_CLIENT_ID, settings.PAYPAL_CLIENT_SECRET),
            data={"grant_type": "client_credentials"},
            headers={"Accept": "application/json"},
            timeout=TIMEOUT,
        )
        if response.status_code >= 300:
            raise PayPalError(
                f"PayPal rejected the credentials ({response.status_code}). "
                f"Check PAYPAL_CLIENT_ID/SECRET match PAYPAL_ENV={settings.PAYPAL_ENV}.",
                status=response.status_code,
            )
        payload = response.json()
        token = payload.get("access_token", "")
        if not token:
            raise PayPalError("PayPal returned no access_token.")

        # 60s of slack so a token cannot expire mid-request.
        lifetime = int(payload.get("expires_in", 3600))
        _token_cache.update(
            value=token,
            key=key,
            expires_at=now + datetime.timedelta(seconds=max(60, lifetime - 60)),
        )
        return token


def _request(method: str, path: str, *, body: "dict | None" = None, idempotent: bool = False) -> dict:
    """One PayPal call, retrying once on a 401 with a freshly minted token.

    The retry matters because a token can be revoked from PayPal's dashboard at
    any time; without it, every call fails until the process is restarted.
    """
    url = f"{api_base()}{path}"
    # PayPal replays the original response for a repeated request id rather
    # than acting twice, which is what makes a double-tapped button safe.
    request_id = secrets.token_hex(16) if idempotent else ""

    def send(token: str):
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if request_id:
            headers["PayPal-Request-Id"] = request_id
        return requests.request(method, url, headers=headers, json=body, timeout=TIMEOUT)

    response = send(_access_token())
    if response.status_code == 401:
        logger.info("PayPal 401 on %s %s; refreshing the access token", method, path)
        response = send(_access_token(force_refresh=True))

    if response.status_code >= 300:
        debug_id = response.headers.get("PayPal-Debug-Id", "")
        logger.error(
            "PayPal %s %s -> %s (debug_id=%s): %s",
            method, path, response.status_code, debug_id, response.text[:1000],
        )
        raise PayPalError(
            _readable_error(response),
            debug_id=debug_id,
            status=response.status_code,
        )

    if response.status_code == 204 or not response.content:
        return {}
    return response.json()


def _readable_error(response) -> str:
    """Turn PayPal's error envelope into one line fit for an admin message."""
    try:
        payload = response.json()
    except ValueError:
        return f"PayPal returned {response.status_code}: {response.text[:200]}"

    name = payload.get("name") or payload.get("error") or "error"
    detail = payload.get("message") or payload.get("error_description") or ""
    issues = payload.get("details") or []
    if issues:
        first = issues[0]
        issue = first.get("issue", "")
        description = first.get("description", "")
        detail = f"{detail} [{issue}: {description}]".strip()
    return f"PayPal {name} ({response.status_code}): {detail}".strip()


# --- the authorization lifecycle -----------------------------------------


def create_order(*, amount, currency: str, reference: str, description: str, brand_name: str = "") -> dict:
    """Create an AUTHORIZE-intent order. Returns PayPal's order object.

    Nothing is held yet — this only describes what the guest is about to
    approve. The hold happens in `authorize_order`.
    """
    unit = {
        "amount": money(amount, currency),
        # Both are shown to the guest on PayPal's own approval screen, which is
        # the one place we cannot restyle. They should read as a deposit.
        "description": description[:127],
        "custom_id": reference[:127],
    }
    body = {
        "intent": "AUTHORIZE",
        "purchase_units": [unit],
        "payment_source": {
            "paypal": {
                "experience_context": {
                    # The guest never leaves the page, so there is nowhere to
                    # return to; the JS SDK resolves the popup itself.
                    "user_action": "CONTINUE",
                    "shipping_preference": "NO_SHIPPING",
                    **({"brand_name": brand_name[:127]} if brand_name else {}),
                }
            }
        },
    }
    return _request("POST", "/v2/checkout/orders", body=body, idempotent=True)


def authorize_order(order_id: str) -> Authorization:
    """Place the hold on an order the guest has approved."""
    payload = _request("POST", f"/v2/checkout/orders/{order_id}/authorize", body={}, idempotent=True)
    return _authorization_from_order(payload)


def get_order(order_id: str) -> dict:
    return _request("GET", f"/v2/checkout/orders/{order_id}")


def get_authorization(authorization_id: str) -> Authorization:
    """Current state of a hold, straight from PayPal.

    Used to reconcile: PayPal expires authorizations on its own schedule and
    never tells this app, so the stored status is only ever a cache.
    """
    return _parse_authorization(_request("GET", f"/v2/payments/authorizations/{authorization_id}"))


def void(authorization_id: str) -> None:
    """Release the hold. The guest's funds are freed; nothing is charged."""
    _request("POST", f"/v2/payments/authorizations/{authorization_id}/void", idempotent=True)


def capture(authorization_id: str, *, amount, currency: str, note: str = "", final: bool = True) -> dict:
    """Take money off a hold, for damage actually found.

    `final_capture` closes the authorization and releases any remainder, which
    is what a damage claim wants: one charge, then the guest is free.
    """
    body = {
        "amount": money(amount, currency),
        "final_capture": final,
    }
    if note:
        # Appears on the guest's PayPal receipt, so they can see what it was for.
        body["note_to_payer"] = note[:255]
    return _request(
        "POST", f"/v2/payments/authorizations/{authorization_id}/capture",
        body=body, idempotent=True,
    )


def reauthorize(authorization_id: str, *, amount, currency: str) -> Authorization:
    """Restart the 3-day honor period, within the original 29-day window.

    Returns a *new* authorization with a new id; the old one is superseded, so
    the caller must store the new id or it will later void the wrong thing.
    """
    payload = _request(
        "POST", f"/v2/payments/authorizations/{authorization_id}/reauthorize",
        body={"amount": money(amount, currency)}, idempotent=True,
    )
    return _parse_authorization(payload)


# --- response parsing ----------------------------------------------------


def _authorization_from_order(order: dict) -> Authorization:
    """Dig the authorization out of an order response.

    It is nested three deep and PayPal returns a list, so this is written to
    fail loudly rather than silently hand back an empty id that would later be
    stored as a hold that does not exist.
    """
    units = order.get("purchase_units") or []
    for unit in units:
        authorizations = (unit.get("payments") or {}).get("authorizations") or []
        if authorizations:
            return _parse_authorization(authorizations[0])
    raise PayPalError(
        f"PayPal accepted the order but returned no authorization "
        f"(order status {order.get('status', 'unknown')!r}). Nothing is held."
    )


def _parse_authorization(node: dict) -> Authorization:
    amount_node = node.get("amount") or {}
    try:
        amount = decimal.Decimal(str(amount_node.get("value", "0")))
    except decimal.InvalidOperation:
        amount = decimal.Decimal("0")
    return Authorization(
        id=node.get("id", ""),
        status=node.get("status", ""),
        amount=amount,
        currency=amount_node.get("currency_code", "USD"),
        expires_at=_parse_time(node.get("expiration_time", "")),
        raw=node,
    )


def _parse_time(value: str):
    """PayPal timestamps are RFC 3339, e.g. '2026-11-17T10:04:51Z'.

    Python 3.9's fromisoformat does not accept the trailing 'Z', and this is
    pinned to 3.9 by the host — so swap it for the offset it stands for.
    """
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("Could not parse PayPal timestamp %r", value)
        return None
