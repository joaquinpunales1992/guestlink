"""Report which PayPal features this install's credentials are allowed to use.

Read-only: it mints an access token and reads the scopes PayPal granted with
it. Nothing is charged, held or stored.

The question it exists to answer is whether the account can vault — save a
guest's payment method on PayPal's side so a damage-deposit hold can be placed
later without the guest entering their card again. Vault is not on by default
and depends on the account's country and approval, and the only reliable way
to know is to ask PayPal with the real credentials, which live on the server:

    .venv/bin/python manage.py paypal_features
"""

from __future__ import annotations

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from concierge import paypal

VAULT_SCOPE = "https://uri.paypal.com/services/vault/payment-tokens/readwrite"


class Command(BaseCommand):
    help = "Show whether the configured PayPal account can vault payment methods."

    def add_arguments(self, parser):
        parser.add_argument(
            "--all", action="store_true", help="Also print every scope PayPal granted."
        )

    def handle(self, *args, **options):
        if not paypal.is_configured():
            raise CommandError("PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET are unset.")

        response = requests.post(
            f"{paypal.api_base()}/v1/oauth2/token",
            auth=(settings.PAYPAL_CLIENT_ID, settings.PAYPAL_CLIENT_SECRET),
            data={"grant_type": "client_credentials"},
            headers={"Accept": "application/json"},
            timeout=paypal.TIMEOUT,
        )
        if response.status_code >= 300:
            raise CommandError(
                f"PayPal rejected the credentials ({response.status_code}) "
                f"for PAYPAL_ENV={settings.PAYPAL_ENV}."
            )
        scopes = set((response.json().get("scope") or "").split())

        self.stdout.write(f"PAYPAL_ENV={settings.PAYPAL_ENV}")
        if VAULT_SCOPE in scopes:
            self.stdout.write(self.style.SUCCESS("Vault: enabled — a saved card can be reused for the deposit."))
        else:
            self.stdout.write(self.style.WARNING(
                "Vault: NOT enabled — enable 'Save payment methods' for this app in the "
                "PayPal developer dashboard (Apps & Credentials -> app -> Features)."
            ))
        if options["all"]:
            for scope in sorted(scopes):
                self.stdout.write(f"  {scope}")
