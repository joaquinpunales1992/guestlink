"""Core models for guestlink concierge relay."""

import secrets

from django.db import models
from django.utils import timezone


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
    keywords = models.TextField(
        blank=True,
        help_text="Comma-separated keywords used by the fallback classifier when no LLM is available.",
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


class Message(models.Model):
    """Every inbound and outbound WhatsApp message we observe or send."""

    class Direction(models.TextChoices):
        GUEST_IN = "guest_in", "From guest"
        GUEST_OUT = "guest_out", "To guest"
        PROVIDER_IN = "provider_in", "From provider"
        PROVIDER_OUT = "provider_out", "To provider"
        SYSTEM_OUT = "system_out", "System → user"

    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, null=True, blank=True, related_name="messages")
    direction = models.CharField(max_length=20, choices=Direction.choices)
    from_phone = models.CharField(max_length=20, blank=True)
    to_phone = models.CharField(max_length=20, blank=True)
    body = models.TextField()
    wa_message_id = models.CharField(max_length=120, blank=True, db_index=True)
    raw_payload = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=("ticket", "direction"))]

    def __str__(self) -> str:
        return f"{self.direction} @ {self.created_at:%Y-%m-%d %H:%M}: {self.body[:40]}"
