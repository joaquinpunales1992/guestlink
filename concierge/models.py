"""Core models for guestlink concierge relay."""

import datetime
import decimal
import re
import secrets

from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from django.utils import timezone

from .affiliate import CAMPAIGN_RE


def normalize_phone(phone: str) -> str:
    """Strip whitespace and any '+' prefix variations into canonical '+<digits>'.

    Lives here rather than in relay.py because Provider.save() needs it and
    relay.py already imports from this module.
    """
    phone = phone.strip()
    if not phone:
        return ""
    digits = re.sub(r"\D", "", phone)
    return f"+{digits}" if digits else ""


def _generate_short_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I to avoid confusion in chat
    return "".join(secrets.choice(alphabet) for _ in range(5))


class Service(models.Model):
    """A category of service the host offers via the QR (e.g. Saona excursion, airport taxi)."""

    class Channel(models.TextChoices):
        WHATSAPP = "whatsapp", "WhatsApp — guest messages us"
        AIRBNB = "airbnb", "Airbnb — guest books via the referral link"
        VIATOR = "viator", "Viator — guest books via the referral link"

    slug = models.SlugField(unique=True)
    name_en = models.CharField(max_length=120)
    # Optional: filled by translating name_en on save when an Anthropic key is
    # configured, and falling back to English on the page when it isn't.
    name_es = models.CharField(max_length=120, blank=True)
    name_fr = models.CharField(max_length=120, blank=True)
    description_en = models.TextField(blank=True)
    description_es = models.TextField(blank=True)
    description_fr = models.TextField(blank=True)
    image = models.ImageField(
        upload_to="services/",
        blank=True,
        null=True,
        help_text="Banner shown at the top of the service card on the landing page.",
    )
    keywords = models.TextField(
        blank=True,
        help_text="Comma-separated keywords used by the fallback classifier when no LLM is available.",
    )
    channel = models.CharField(
        max_length=20,
        choices=Channel.choices,
        default=Channel.WHATSAPP,
        help_text=(
            "Where this service's button sends guests. Airbnb and Viator both need "
            "a Referral URL below — without one the card falls back to WhatsApp so "
            "it is never a dead end."
        ),
    )
    referral_url = models.URLField(
        blank=True,
        # URLField defaults to 200, which renders maxlength="200" on the admin
        # input — a browser silently truncates a longer paste, with no error.
        # Airbnb share links run to ~350 characters and carry listing_id near
        # the end, so the part that identifies the experience is exactly what
        # got cut off.
        max_length=800,
        help_text=(
            "Affiliate or referral link, matching the channel above. Paste the whole "
            "link — Airbnb's are long, and the listing_id near the end is what points "
            "at the right experience."
        ),
    )
    default_provider = models.ForeignKey(
        "Provider", on_delete=models.SET_NULL, null=True, blank=True, related_name="default_for_services"
    )
    expected_commission_usd = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    sort_order = models.PositiveSmallIntegerField(default=100)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ("sort_order", "name_en")

    def __str__(self) -> str:
        return self.name_en

    @property
    def keyword_list(self) -> list[str]:
        return [k.strip().lower() for k in self.keywords.split(",") if k.strip()]

    @property
    def uses_referral_link(self) -> bool:
        """True when this card should send guests to the booking provider.

        A referral channel with no URL falls back to WhatsApp rather than
        rendering a card that goes nowhere — links get filled in gradually.
        """
        return self.channel in (self.Channel.AIRBNB, self.Channel.VIATOR) and bool(self.referral_url)

    @property
    def channel_label(self) -> str:
        """"Airbnb" / "Viator" for display; empty for WhatsApp."""
        return dict(self.Channel.choices).get(self.channel, "").split(" — ")[0] if self.uses_referral_link else ""

    # The landing page reads these rather than the raw columns so a missing
    # translation shows the English name instead of an empty card title — and,
    # in the pre-filled WhatsApp message, instead of "info sobre ." .
    @property
    def display_name_es(self) -> str:
        return self.name_es or self.name_en

    @property
    def display_name_fr(self) -> str:
        return self.name_fr or self.name_en


class Location(models.Model):
    """A place a QR code is displayed — a restaurant, dive shop, apartment, lobby.

    Each has its own landing URL, its own menu of services, and its own Viator
    campaign code, so bookings and traffic can be attributed to the venue that
    produced them.
    """

    class Kind(models.TextChoices):
        APARTMENT = "apartment", "Holiday apartment"
        HOTEL = "hotel", "Hotel"
        RESIDENCE = "residence", "Residential building"
        RESTAURANT = "restaurant", "Restaurant / bar"
        DIVE_SHOP = "dive_shop", "Dive shop"
        SUPERMARKET = "supermarket", "Supermarket / shop"
        OTHER = "other", "Other"

    name = models.CharField(max_length=120, help_text="Shown to guests, e.g. “Restaurante La Bahía”.")
    slug = models.SlugField(
        unique=True,
        help_text="The QR address: bookyourtickets.online/<slug>. Changing it orphans printed codes.",
    )
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.OTHER)
    services = models.ManyToManyField(
        Service,
        blank=True,
        related_name="locations",
        help_text=(
            "Which services this venue shows. Leave empty to show all active ones. "
            "Use it to avoid advertising a venue's own competitors."
        ),
    )
    campaign_code = models.SlugField(
        max_length=60,
        blank=True,
        help_text=(
            "Viator campaign for this venue. Leave blank unless you have checked "
            "that a link carrying it still opens the tour rather than a listing — "
            "extra parameters have broken Viator links on this account before."
        ),
    )
    # --- how this business presents itself -------------------------------
    # Each location is its own business with its own page. Every field here is
    # optional and falls back to the site-wide default, so a venue you have not
    # written copy for still renders — and the original single-apartment setup
    # keeps working unchanged.
    tab_title = models.CharField(
        max_length=120, blank=True, help_text="Browser tab title. Blank uses the site default."
    )
    headline_en = models.CharField(max_length=160, blank=True, help_text="The big line at the top.")
    headline_es = models.CharField(max_length=160, blank=True)
    headline_fr = models.CharField(max_length=160, blank=True)
    tagline_en = models.CharField(max_length=240, blank=True, help_text="The sentence under the headline.")
    tagline_es = models.CharField(max_length=240, blank=True)
    tagline_fr = models.CharField(max_length=240, blank=True)
    footer_text = models.CharField(max_length=200, blank=True)

    commission_share_pct = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text=(
            "This venue's cut, as a percentage of the commission you receive — not of "
            "the booking price. 20 means you keep 80%. Changing it only affects "
            "commissions recorded from now on."
        ),
    )
    contact_name = models.CharField(max_length=120, blank=True)
    notes = models.TextField(blank=True, help_text="Internal — revenue share agreed, who to invoice, etc.")
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ("name",)

    def __str__(self) -> str:
        return self.name

    # Copy fields this venue can override; anything blank falls back site-wide.
    BRANDING_FIELDS = (
        "tab_title", "footer_text",
        "headline_en", "headline_es", "headline_fr",
        "tagline_en", "tagline_es", "tagline_fr",
    )

    @property
    def viator_campaign(self) -> str:
        """Campaign code sent to Viator — only when explicitly set.

        It used to default to the slug, but every extra parameter on a Viator
        link has to be verified against a live product page: an unchecked one
        sends guests to a listing. Set it per venue once you have confirmed the
        link still opens the tour.
        """
        return self.campaign_code

    def branding(self, site) -> dict:
        """This venue's page copy, filling any gap from the site defaults."""
        return {
            field: (getattr(self, field, "") or getattr(site, field, ""))
            for field in self.BRANDING_FIELDS
        }

    def visible_services(self):
        """Active services for this venue — all of them when none are chosen."""
        chosen = self.services.filter(active=True)
        return chosen if chosen.exists() else Service.objects.filter(active=True)


class LocationEvent(models.Model):
    """A QR scan or an outbound click, counted per location.

    Deliberately holds no IP address, user agent, or anything else identifying
    a visitor — these are counters for attribution and revenue share, not
    analytics about people.
    """

    class Kind(models.TextChoices):
        SCAN = "scan", "Scan (landing page opened)"
        CLICK = "click", "Click (guest followed a service)"

    location = models.ForeignKey(
        Location, on_delete=models.CASCADE, null=True, blank=True, related_name="events"
    )
    kind = models.CharField(max_length=10, choices=Kind.choices)
    service = models.ForeignKey(
        Service, on_delete=models.SET_NULL, null=True, blank=True, related_name="events"
    )
    # Copied rather than derived: the service's channel may change later, and
    # historic counts should reflect where the guest was actually sent.
    channel = models.CharField(max_length=20, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=("location", "kind", "created_at"))]

    def __str__(self) -> str:
        where = self.location.name if self.location else "no location"
        return f"{self.kind} @ {where} ({self.created_at:%Y-%m-%d %H:%M})"


class Commission(models.Model):
    """One commission: what you earn on a booking, and what you owe the venue.

    Both sides live on one row because they are two halves of the same event —
    a Saona booking from the restaurant's QR earns you $15 and owes the
    restaurant its share of that $15. Tracking them separately would let the
    two drift apart.

    Amounts are the commission itself, never the booking price, and always in
    USD.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Expected — not claimed yet"
        CLAIMED = "claimed", "Claimed / invoiced"
        RECEIVED = "received", "Received"
        WRITTEN_OFF = "written_off", "Written off"

    class Payout(models.TextChoices):
        NONE = "none", "No share due"
        OWED = "owed", "Owed to the venue"
        PAID = "paid", "Paid to the venue"

    occurred_on = models.DateField(
        default=datetime.date.today, help_text="Date of the booking this commission is for."
    )
    location = models.ForeignKey(
        Location, on_delete=models.PROTECT, null=True, blank=True, related_name="commissions",
        help_text="Which venue's QR produced it. Blank means it came from no particular venue.",
    )
    service = models.ForeignKey(
        Service, on_delete=models.PROTECT, null=True, blank=True, related_name="commissions"
    )
    channel = models.CharField(
        max_length=20,
        choices=Service.Channel.choices,
        help_text="Who owes you: the provider (WhatsApp), Airbnb, or Viator.",
    )
    provider = models.ForeignKey(
        "Provider", on_delete=models.PROTECT, null=True, blank=True,
        related_name="commissions",
        help_text="For WhatsApp referrals: the provider you claim this from.",
    )
    ticket = models.ForeignKey(
        "Ticket", on_delete=models.SET_NULL, null=True, blank=True, related_name="commissions"
    )

    gross_usd = models.DecimalField(
        max_digits=10, decimal_places=2,
        validators=[MinValueValidator(0)],
        help_text="The commission you earn, in USD. Not the price the guest paid.",
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)

    # Snapshot, not a live lookup: renegotiating a venue's share must not
    # silently rewrite what you already owe on past bookings.
    venue_share_pct = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    venue_share_usd = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    payout_status = models.CharField(max_length=20, choices=Payout.choices, default=Payout.NONE)

    reference = models.CharField(
        max_length=120, blank=True, help_text="Booking or payout reference, for reconciling."
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ("-occurred_on", "-created_at")
        indexes = [
            models.Index(fields=("status", "occurred_on")),
            models.Index(fields=("payout_status", "occurred_on")),
            models.Index(fields=("location", "occurred_on")),
        ]

    def __str__(self) -> str:
        where = self.location.name if self.location else "no venue"
        return f"{self.get_channel_display()} · ${self.gross_usd} · {where} · {self.occurred_on}"

    @property
    def net_usd(self):
        """What you keep once the venue is paid."""
        return self.gross_usd - self.venue_share_usd

    def save(self, *args, **kwargs):
        # Default the share from the venue, then keep amount and status in step
        # with it so a hand-edited percentage can't leave a stale figure behind.
        if self.venue_share_pct == 0 and self.location_id and not self.pk:
            self.venue_share_pct = self.location.commission_share_pct
        self.venue_share_usd = (self.gross_usd * self.venue_share_pct / 100).quantize(
            decimal.Decimal("0.01"), rounding=decimal.ROUND_HALF_UP
        )
        if self.venue_share_usd <= 0:
            self.payout_status = self.Payout.NONE
        elif self.payout_status == self.Payout.NONE:
            self.payout_status = self.Payout.OWED
        super().save(*args, **kwargs)


class Provider(models.Model):
    """A local provider we refer guests to (lanchero, taxista, restaurant, etc.)."""

    name = models.CharField(max_length=120)
    phone = models.CharField(max_length=20, unique=True, help_text="E.164 format, e.g. +18091234567")
    services = models.ManyToManyField(Service, blank=True, related_name="providers")
    notes = models.TextField(blank=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ("name",)

    def __str__(self) -> str:
        return f"{self.name} ({self.phone})"

    def save(self, *args, **kwargs):
        # Inbound routing matches this column against a normalized '+<digits>'
        # string (relay.handle_inbound). A number typed into the admin as
        # "+1 809-222-3333" would never match, and the provider's replies would
        # be treated as a brand-new guest instead of being forwarded.
        self.phone = normalize_phone(self.phone)
        super().save(*args, **kwargs)


class Guest(models.Model):
    """A guest who has contacted us via the QR / WhatsApp."""

    phone = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=120, blank=True)
    first_seen = models.DateTimeField(default=timezone.now)
    last_seen = models.DateTimeField(default=timezone.now)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ("-last_seen",)

    def __str__(self) -> str:
        return self.name or self.phone


class Ticket(models.Model):
    """A referral instance: guest asked for a service, we routed to a provider."""

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        IN_PROGRESS = "in_progress", "In progress"
        COMPLETED = "completed", "Completed"
        NO_SHOW = "no_show", "No show"
        CANCELLED = "cancelled", "Cancelled"

    guest = models.ForeignKey(Guest, on_delete=models.CASCADE, related_name="tickets")
    provider = models.ForeignKey(Provider, on_delete=models.PROTECT, related_name="tickets")
    # Detected from the guest's first message, which the QR pre-fills with the
    # venue's name. Blank when they typed their own opener instead.
    location = models.ForeignKey(
        Location, on_delete=models.SET_NULL, null=True, blank=True, related_name="tickets"
    )
    service = models.ForeignKey(Service, on_delete=models.PROTECT, related_name="tickets")
    short_code = models.CharField(max_length=10, unique=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    created_at = models.DateTimeField(default=timezone.now)
    closed_at = models.DateTimeField(null=True, blank=True)
    expected_commission_usd = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    raw_first_message = models.TextField(blank=True)
    extracted_fields = models.JSONField(default=dict, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("guest", "status")),
            models.Index(fields=("provider", "status")),
        ]

    def __str__(self) -> str:
        return f"[{self.short_code}] {self.service} → {self.provider} ({self.status})"

    @classmethod
    def new_short_code(cls) -> str:
        for _ in range(20):
            code = _generate_short_code()
            if not cls.objects.filter(short_code=code).exists():
                return code
        raise RuntimeError("Could not allocate unique short_code after 20 tries")

    def close(self, status: str = Status.COMPLETED) -> None:
        self.status = status
        self.closed_at = timezone.now()
        self.save(update_fields=("status", "closed_at"))

    @property
    def is_active(self) -> bool:
        return self.status in {self.Status.OPEN, self.Status.IN_PROGRESS}


class SiteSettings(models.Model):
    """Singleton holding admin-editable copy for the guest-facing landing page."""

    class CtaMode(models.TextChoices):
        WHATSAPP = "whatsapp", "WhatsApp for everything (ignore each service's channel)"
        REFERRAL = "referral", "Use each service's own channel"
        BOTH = "both", "Use each service's channel, with WhatsApp underneath"

    cta_mode = models.CharField(
        max_length=20,
        choices=CtaMode.choices,
        default=CtaMode.WHATSAPP,
        help_text=(
            "Site-wide switch over the per-service Channel. The first option is a "
            "kill switch that routes every card to WhatsApp — useful if a referral "
            "programme is paused."
        ),
    )
    viator_partner_id = models.CharField(
        max_length=32,
        blank=True,
        help_text=(
            "Your Viator affiliate PID, e.g. P00012345. Set this once and any plain "
            "viator.com product URL becomes a referral link — no need to build one "
            "per service."
        ),
    )
    viator_mcid = models.CharField(
        max_length=32,
        blank=True,
        help_text=(
            "Leave blank unless Viator gave you a campaign id for this account. "
            "A value that is not yours makes their links fall back to a listing "
            "page instead of the tour."
        ),
    )
    viator_campaign = models.CharField(
        max_length=60,
        blank=True,
        validators=[
            RegexValidator(
                CAMPAIGN_RE,
                "Letters, numbers and dashes only — other characters break Viator's tracking.",
            )
        ],
        help_text="Optional label for your own reporting, e.g. apto-reef-qr.",
    )

    referral_cta_en = models.CharField(max_length=60, default="Book online")
    referral_cta_es = models.CharField(max_length=60, default="Reservar online")
    referral_cta_fr = models.CharField(max_length=60, default="Réserver en ligne")
    referral_disclosure = models.CharField(
        max_length=200,
        blank=True,
        default="Some links are referral links — booking through them may earn us a commission at no extra cost to you.",
        help_text="Shown in the footer whenever referral links are on the page. Leave blank to hide.",
    )

    tab_title = models.CharField(max_length=120, help_text="Browser tab title (single string, language-neutral).")
    footer_text = models.CharField(max_length=200, help_text="Line shown in the page footer.")
    headline_en = models.CharField(max_length=160)
    headline_es = models.CharField(max_length=160)
    headline_fr = models.CharField(max_length=160, blank=True)
    tagline_en = models.CharField(max_length=240, blank=True)
    tagline_es = models.CharField(max_length=240, blank=True)
    tagline_fr = models.CharField(max_length=240, blank=True)

    class Meta:
        verbose_name = "Site settings"
        verbose_name_plural = "Site settings"

    def __str__(self) -> str:
        return self.tab_title or "Site settings"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        return  # singleton, deletion disabled

    @classmethod
    def load(cls) -> "SiteSettings":
        obj, _ = cls.objects.get_or_create(pk=1, defaults=cls._defaults())
        return obj

    @classmethod
    def _defaults(cls) -> dict:
        from django.conf import settings as dj_settings

        host = getattr(dj_settings, "HOST_NAME", "Host")
        apt = getattr(dj_settings, "HOST_APARTMENT_LABEL", "1")
        return {
            "tab_title": f"{host} · Apto {apt} · Bayahibe",
            "footer_text": f"{host} · Bayahibe, Dominican Republic",
            "headline_en": f"Welcome to Apto {apt}",
            "headline_es": f"Bienvenido al Apto {apt}",
            "headline_fr": f"Bienvenue à l'Apto {apt}",
            "tagline_en": "Whatever you need during your stay, we'll arrange it for you over WhatsApp.",
            "tagline_es": "Lo que necesites durante tu estadía, lo coordinamos por WhatsApp.",
            "tagline_fr": "Tout ce dont vous avez besoin pendant votre séjour, nous l'organisons sur WhatsApp.",
        }


class Message(models.Model):
    """Every inbound and outbound WhatsApp message we observe or send."""

    class Direction(models.TextChoices):
        GUEST_IN = "guest_in", "From guest"
        GUEST_OUT = "guest_out", "To guest"
        PROVIDER_IN = "provider_in", "From provider"
        PROVIDER_OUT = "provider_out", "To provider"
        SYSTEM_OUT = "system_out", "System → user"

    class Delivery(models.TextChoices):
        """Outbound only — inbound rows leave this blank."""

        SENT = "sent", "Sent"
        DRY_RUN = "dry_run", "Dry run"
        FAILED = "failed", "Failed"

    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, null=True, blank=True, related_name="messages")
    direction = models.CharField(max_length=20, choices=Direction.choices)
    from_phone = models.CharField(max_length=20, blank=True)
    to_phone = models.CharField(max_length=20, blank=True)
    body = models.TextField()
    wa_message_id = models.CharField(max_length=120, blank=True, db_index=True)
    raw_payload = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    delivery_status = models.CharField(
        max_length=20,
        blank=True,
        choices=Delivery.choices,
        help_text="Outbound only. 'failed' means the guest or provider never received this.",
    )
    delivery_error = models.TextField(
        blank=True,
        help_text="Why the send failed — usually an expired token or a closed 24-hour window.",
    )

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("ticket", "direction")),
            # The admin's "undelivered" view scans this; keep it cheap.
            models.Index(fields=("delivery_status", "created_at")),
        ]

    def __str__(self) -> str:
        return f"{self.direction} @ {self.created_at:%Y-%m-%d %H:%M}: {self.body[:40]}"
