"""Live experiment: can one PayPal hold pay a booking and keep a deposit?

The plan for a "booking + deposit" link without Vault is a single
authorization for both amounts, of which the booking is captured at once and
the rest stays held as the deposit. PayPal documents multiple partial
captures, but not whether the remainder can still be *reauthorized* after one
— and reauthorizing is what keeps a deposit capturable past day 3. Sandbox
cannot answer that, because it will not reauthorize before day 4 either, so
this runs against the real account with one dollar.

The amounts are fixed here on purpose: this is a probe, not a payment tool.

    start               hold 1.00, capture 0.60 ("booking"), 0.40 stays held
    reauth AUTH_ID      on day 4 or later: try to renew the 0.40
    status AUTH_ID      what PayPal says about the hold now
    finish AUTH_ID      release the remainder (or --damage: capture 0.20 of it)

The payer must be a different PayPal account or card from the business
account — PayPal refuses a payment to yourself.
"""

from __future__ import annotations

import json
import time

from django.core.management.base import BaseCommand, CommandError

from concierge import paypal

TOTAL = "1.00"
BOOKING = "0.60"
DEPOSIT = "0.40"
DAMAGE = "0.20"
CURRENCY = "USD"
APPROVAL_WAIT_SECONDS = 15 * 60


class Command(BaseCommand):
    help = "One-dollar live probe of booking + deposit on a single PayPal hold."

    def add_arguments(self, parser):
        parser.add_argument("step", choices=["start", "reauth", "status", "finish"])
        parser.add_argument("authorization_id", nargs="?", default="")
        parser.add_argument("--damage", action="store_true",
                            help=f"finish by capturing {DAMAGE} instead of releasing")

    def handle(self, *args, **options):
        if not paypal.is_configured():
            raise CommandError("PayPal is not configured.")
        step, auth_id = options["step"], options["authorization_id"]
        if step != "start" and not auth_id:
            raise CommandError(f"'{step}' needs the authorization id printed by 'start'.")
        self.stdout.write(f"PAYPAL_ENV={'live' if paypal.is_live() else 'sandbox'}")
        getattr(self, f"step_{step}")(auth_id, options)

    # --- steps -------------------------------------------------------------

    def step_start(self, _auth_id, _options):
        order = paypal._request("POST", "/v2/checkout/orders", idempotent=True, body={
            "intent": "AUTHORIZE",
            "purchase_units": [{
                "amount": paypal.money(TOTAL, CURRENCY),
                "description": "Test: booking 0.60 + refundable deposit 0.40",
                "custom_id": "split-probe",
            }],
            "payment_source": {"paypal": {"experience_context": {
                "user_action": "CONTINUE",
                "shipping_preference": "NO_SHIPPING",
                # Only somewhere to land after approving; nothing reads it.
                "return_url": "https://bookyourtickets.online/",
                "cancel_url": "https://bookyourtickets.online/",
            }}},
        })
        link = next(l["href"] for l in order.get("links", []) if l["rel"] in ("payer-action", "approve"))
        self.stdout.write(
            f"\nOpen this and approve with ANOTHER PayPal account or card (US$ {TOTAL}):\n\n"
            f"  {link}\n\nWaiting up to {APPROVAL_WAIT_SECONDS // 60} min for the approval..."
        )

        deadline = time.monotonic() + APPROVAL_WAIT_SECONDS
        while paypal.get_order(order["id"]).get("status") != "APPROVED":
            if time.monotonic() > deadline:
                raise CommandError("Not approved in time. Nothing was held; run 'start' again.")
            time.sleep(5)

        auth = paypal.authorize_order(order["id"])
        self.stdout.write(self.style.SUCCESS(
            f"Held {auth.amount} {auth.currency}. Authorization id: {auth.id}  <-- keep this"
        ))
        self._try(f"Capture {BOOKING} as the booking (final_capture=false)", paypal.capture,
                  auth.id, amount=BOOKING, currency=CURRENCY, note="Test booking", final=False)
        self._show(auth.id)
        self.stdout.write(
            f"\nNext, on day 4 or later:\n"
            f"  .venv/bin/python manage.py paypal_split_test reauth {auth.id}"
        )

    def step_reauth(self, auth_id, _options):
        # Unknown which amount PayPal expects after a partial capture, so try
        # the remainder first and the original total second.
        for amount in (DEPOSIT, TOTAL):
            new = self._try(f"Reauthorize {amount}", paypal.reauthorize,
                            auth_id, amount=amount, currency=CURRENCY)
            if new:
                self.stdout.write(self.style.SUCCESS(
                    f"New authorization id: {new.id}, expires {new.expires_at}. Use THIS id from now on."
                ))
                self._show(new.id)
                return
        self._show(auth_id)

    def step_status(self, auth_id, _options):
        self._show(auth_id)

    def step_finish(self, auth_id, options):
        if options["damage"]:
            self._try(f"Capture {DAMAGE} as damage (final_capture=true)", paypal.capture,
                      auth_id, amount=DAMAGE, currency=CURRENCY, note="Test damage", final=True)
        else:
            self._try("Release the remainder (void)", paypal.void, auth_id)
        self._show(auth_id)

    # --- output ------------------------------------------------------------

    def _try(self, label, fn, *args, **kwargs):
        try:
            result = fn(*args, **kwargs)
        except paypal.PayPalError as exc:
            self.stdout.write(self.style.ERROR(f"FAIL  {label}\n      {exc}\n      debug_id={exc.debug_id}"))
            return None
        self.stdout.write(self.style.SUCCESS(f"OK    {label}"))
        return result if result is not None else True

    def _show(self, auth_id):
        try:
            auth = paypal.get_authorization(auth_id)
        except paypal.PayPalError as exc:
            self.stdout.write(self.style.ERROR(f"Could not read {auth_id}: {exc}"))
            return
        raw = auth.raw
        self.stdout.write("Authorization now: " + json.dumps({
            "status": raw.get("status"),
            "status_details": raw.get("status_details"),
            "amount": raw.get("amount"),
            "expiration_time": raw.get("expiration_time"),
        }))
