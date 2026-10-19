"""Reconcile damage deposits against PayPal, and flag the ones about to lapse.

PayPal expires a hold 29 days after it was placed and never tells this app, so
a deposit sitting at "held" in the admin may be holding nothing at all. This
command is what keeps that honest. It also surfaces the two failures that are
invisible until it is too late: a hold that dies before the guest checks out,
and one past its 3-day honor period that PayPal no longer guarantees.

Run it daily:

    uv run python manage.py sync_deposits

On cPanel, a cron entry doing the same:

    cd ~/guestlink && .venv/bin/python manage.py sync_deposits >> ~/logs/deposits.log 2>&1

With --reauthorize it also renews holds that have left the honor period but are
still inside the 29-day window. That is off by default: it is a real API call
against a guest's card, and whether it is wanted depends on how long the stay
is, so the default is to report and let the host decide.
"""

from __future__ import annotations

import datetime

from django.core.management.base import BaseCommand
from django.utils import timezone

from concierge import paypal
from concierge.admin import sync_authorization
from concierge.models import DamageDeposit


class Command(BaseCommand):
    help = "Refresh damage deposit holds from PayPal and report ones needing attention."

    def add_arguments(self, parser):
        parser.add_argument(
            "--reauthorize",
            action="store_true",
            help="Also renew holds past their honor period but still inside the 29-day window.",
        )
        parser.add_argument(
            "--warn-days",
            type=int,
            default=5,
            help="Flag holds expiring within this many days (default: 5).",
        )

    def handle(self, *args, **options):
        if not paypal.is_configured():
            self.stderr.write(
                self.style.ERROR(
                    "PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET are unset — nothing to sync."
                )
            )
            return

        warn_days = options["warn_days"]
        deposits = DamageDeposit.objects.filter(
            status=DamageDeposit.Status.HELD
        ).exclude(paypal_authorization_id="")

        if not deposits.exists():
            self.stdout.write("No live holds to check.")
            return

        checked = changed = renewed = 0
        warnings = []

        for record in deposits:
            try:
                live = paypal.get_authorization(record.paypal_authorization_id)
            except paypal.PayPalError as exc:
                self.stderr.write(self.style.ERROR(f"{record.guest_name}: {exc}"))
                continue

            checked += 1
            if sync_authorization(record, live):
                changed += 1
                self.stdout.write(
                    f"  {record.guest_name}: now {record.get_status_display().split(' — ')[0]}"
                )

            # Re-read the status: sync may just have moved it off "held", in
            # which case there is nothing left to renew or warn about.
            if record.status != DamageDeposit.Status.HELD:
                continue

            if options["reauthorize"] and record.needs_reauthorization:
                renewed += self._reauthorize(record)
                continue

            warning = self._warning_for(record, warn_days)
            if warning:
                warnings.append(warning)

        self.stdout.write(
            self.style.SUCCESS(
                f"Checked {checked} hold(s); {changed} changed"
                + (f"; {renewed} reauthorized" if options["reauthorize"] else "")
                + "."
            )
        )
        for warning in warnings:
            self.stdout.write(self.style.WARNING(f"  ! {warning}"))

    def _reauthorize(self, record: DamageDeposit) -> int:
        try:
            fresh = paypal.reauthorize(
                record.paypal_authorization_id,
                amount=record.amount,
                currency=record.currency,
            )
        except paypal.PayPalError as exc:
            self.stderr.write(self.style.ERROR(f"{record.guest_name}: reauthorize failed — {exc}"))
            return 0

        record.paypal_authorization_id = fresh.id
        record.expires_at = fresh.expires_at
        record.authorized_at = timezone.now()
        record.save(update_fields=("paypal_authorization_id", "expires_at", "authorized_at"))
        self.stdout.write(f"  {record.guest_name}: reauthorized, new id {fresh.id}")
        return 1

    def _warning_for(self, record: DamageDeposit, warn_days: int) -> str:
        """The one line worth printing about a hold that still stands."""
        if not record.outlasts_the_stay:
            return (
                f"{record.guest_name}: hold expires {record.expiry:%d %b} but check-out is "
                f"{record.check_out:%d %b} — it will be gone before the apartment is checked."
            )
        days = record.days_left
        if days is not None and days <= warn_days:
            return f"{record.guest_name}: hold expires in {days} day(s) ({record.expiry:%d %b})."
        if record.needs_reauthorization:
            return (
                f"{record.guest_name}: past the {paypal.HONOR_PERIOD_DAYS}-day honor period — "
                "a capture is no longer guaranteed. Reauthorize to restart it."
            )
        if record.check_out and record.check_out < timezone.now().date() - datetime.timedelta(days=2):
            return (
                f"{record.guest_name}: checked out {record.check_out:%d %b} and the hold is "
                "still standing. Release it."
            )
        return ""
