"""Pre-flight check to run ON the server after deploying.

The single biggest unknown on cheap shared hosting is whether outbound HTTPS is
allowed. If it isn't, the relay silently degrades: Meta webhooks still arrive,
tickets still get created, but nothing is ever delivered to the provider or the
guest. Better to find out from a command than from a confused guest.

Usage (from the app root, with the cPanel virtualenv active):
    python manage.py check_deploy
"""

from __future__ import annotations

import os
import platform
import socket
import sys

import django
from django.conf import settings
from django.core.management.base import BaseCommand

PROBES = [
    ("WhatsApp Cloud API", "https://graph.facebook.com/v21.0/", "graph.facebook.com"),
    ("Anthropic API", "https://api.anthropic.com/v1/models", "api.anthropic.com"),
]


class Command(BaseCommand):
    help = "Verify the server can actually run guestlink (config + outbound network)."

    def handle(self, *args, **options):
        failures = 0
        failures += self._section_runtime()
        failures += self._section_config()
        failures += self._section_paths()
        failures += self._section_network()

        self.stdout.write("")
        if failures:
            self.stdout.write(self.style.ERROR(f"{failures} problem(s) found — see above."))
            sys.exit(1)
        self.stdout.write(self.style.SUCCESS("All checks passed."))

    # -- helpers ---------------------------------------------------------

    def _ok(self, msg: str) -> int:
        self.stdout.write(self.style.SUCCESS(f"  ok    {msg}"))
        return 0

    def _warn(self, msg: str) -> int:
        self.stdout.write(self.style.WARNING(f"  warn  {msg}"))
        return 0

    def _bad(self, msg: str) -> int:
        self.stdout.write(self.style.ERROR(f"  FAIL  {msg}"))
        return 1

    def _header(self, title: str) -> None:
        self.stdout.write(self.style.MIGRATE_HEADING(f"\n{title}"))

    # -- sections --------------------------------------------------------

    def _section_runtime(self) -> int:
        self._header("Runtime")
        f = 0
        self.stdout.write(f"  python {platform.python_version()} at {sys.executable}")
        self.stdout.write(f"  django {django.get_version()}")
        if sys.version_info < (3, 10):
            f += self._bad("Django 5.2 needs Python >= 3.10. Pick a newer interpreter in cPanel.")
        elif sys.version_info < (3, 12):
            f += self._warn("Python < 3.12 means Django 5.2 LTS here vs 6.0 in local dev")
        else:
            f += self._ok("Python matches the Django major used in local dev")
        return f

    def _section_config(self) -> int:
        self._header("Configuration")
        f = 0
        if settings.DEBUG:
            f += self._bad("DJANGO_DEBUG is on — set DJANGO_DEBUG=0 in .env before going live")
        else:
            f += self._ok("DEBUG is off")

        if not settings.ALLOWED_HOSTS or settings.ALLOWED_HOSTS == ["localhost", "127.0.0.1"]:
            f += self._bad("DJANGO_ALLOWED_HOSTS still points at localhost")
        else:
            f += self._ok(f"ALLOWED_HOSTS = {settings.ALLOWED_HOSTS}")

        if not settings.CSRF_TRUSTED_ORIGINS:
            f += self._warn("DJANGO_CSRF_TRUSTED_ORIGINS is empty — admin logins over HTTPS may fail")
        else:
            f += self._ok(f"CSRF_TRUSTED_ORIGINS = {settings.CSRF_TRUSTED_ORIGINS}")

        if settings.WHATSAPP_DRY_RUN:
            f += self._warn("WHATSAPP_DRY_RUN=1 — outbound messages are logged, not sent")
        elif not (settings.WHATSAPP_ACCESS_TOKEN and settings.WHATSAPP_PHONE_NUMBER_ID):
            f += self._bad("Dry-run is off but WhatsApp credentials are incomplete")
        else:
            f += self._ok("WhatsApp credentials present and dry-run is off")

        if settings.WHATSAPP_VERIFY_TOKEN in ("", "local-verify-token"):
            f += self._bad("WHATSAPP_VERIFY_TOKEN is still the default — Meta's webhook needs a real secret")
        else:
            f += self._ok("WHATSAPP_VERIFY_TOKEN is customised")

        if not settings.ANTHROPIC_API_KEY:
            f += self._warn("ANTHROPIC_API_KEY unset — classifier falls back to keyword matching")
        else:
            f += self._ok("ANTHROPIC_API_KEY present")
        return f

    def _section_paths(self) -> int:
        self._header("Filesystem")
        f = 0
        db_path = str(settings.DATABASES["default"]["NAME"])
        if "public_html" in db_path:
            f += self._bad(f"SQLite file sits inside the web docroot and is downloadable: {db_path}")
        elif not os.path.exists(db_path):
            f += self._bad(f"SQLite file missing: {db_path} — run `python manage.py migrate`")
        elif not os.access(db_path, os.W_OK):
            f += self._bad(f"SQLite file is not writable: {db_path}")
        else:
            f += self._ok(f"database writable at {db_path}")

        for label, path in (("STATIC_ROOT", settings.STATIC_ROOT), ("MEDIA_ROOT", settings.MEDIA_ROOT)):
            if not os.path.isdir(path):
                msg = f"{label} does not exist: {path}"
                f += self._bad(msg + " — run `python manage.py collectstatic`" if label == "STATIC_ROOT" else msg)
            elif not os.access(path, os.W_OK):
                f += self._bad(f"{label} is not writable: {path}")
            else:
                f += self._ok(f"{label} = {path}")
        return f

    def _section_network(self) -> int:
        self._header("Outbound network (the shared-hosting risk)")
        f = 0
        try:
            import requests
        except ImportError:  # pragma: no cover - dependency is declared
            return self._bad("requests is not installed")

        for label, url, host in PROBES:
            try:
                socket.getaddrinfo(host, 443)
            except socket.gaierror as exc:
                f += self._bad(f"{label}: DNS lookup for {host} failed ({exc})")
                continue
            try:
                # Any HTTP response proves the egress path works. 400/401 from
                # an unauthenticated probe is a pass, not a failure.
                resp = requests.get(url, timeout=15)
            except requests.exceptions.SSLError as exc:
                f += self._bad(f"{label}: TLS failed — host may be intercepting HTTPS ({exc})")
            except requests.exceptions.RequestException as exc:
                f += self._bad(f"{label}: cannot reach {host}:443 — outbound likely blocked ({exc})")
            else:
                f += self._ok(f"{label}: reachable (HTTP {resp.status_code})")
        return f
