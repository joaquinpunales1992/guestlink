"""Core models for guestlink concierge relay."""

import re
import secrets

from django.db import models
from django.utils import timezone


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

    slug = models.SlugField(unique=True)
    name_en = models.CharField(max_length=120)
    name_es = models.CharField(max_length=120)
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
    referral_url = models.URLField(
        blank=True,
        help_text=(
            "Affiliate or referral link for this service (e.g. an Airbnb experience). "
            "Shown instead of the WhatsApp button when the landing page CTA mode uses "
            "referral links. Services without one fall back to WhatsApp."
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
        WHATSAPP = "whatsapp", "WhatsApp only"
        REFERRAL = "referral", "Referral link only (WhatsApp used where no link is set)"
        BOTH = "both", "Referral link, with WhatsApp underneath"

    cta_mode = models.CharField(
        max_length=20,
        choices=CtaMode.choices,
        default=CtaMode.WHATSAPP,
        help_text=(
            "What each service card links to. Referral modes need a Referral URL on "
            "the service; any service without one keeps its WhatsApp button so the "
            "card is never a dead end."
        ),
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
