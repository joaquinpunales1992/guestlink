import datetime
import decimal

from django.conf import settings
from django.contrib import admin, messages
from django.db.models import Count, Q, Sum
from django.utils import timezone
from django.urls import reverse
from django.utils.html import format_html

from .models import (
    Commission,
    Guest,
    Location,
    LocationEvent,
    Message,
    Provider,
    Service,
    SiteSettings,
    Ticket,
)
from .referral_preview import PreviewError, fetch_preview, points_at_a_listing
from .translate import TranslationError, fill_missing_names


def _is_fetched_image(service: Service) -> bool:
    """True if this image came from a referral link rather than the host.

    Fetched files are always saved as "<slug>-preview.jpg"; Django may append a
    random suffix on collision, so match the stem rather than the whole name.
    """
    if not service.image:
        return False
    filename = service.image.name.rsplit("/", 1)[-1]
    return filename.startswith(f"{service.slug}-preview")


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = (
        "name_en", "slug", "channel", "has_referral", "default_provider",
        "expected_commission_usd", "active", "sort_order",
    )
    list_filter = ("channel", "active")
    search_fields = ("name_en", "name_es", "name_fr", "slug", "keywords")

    actions = ["fetch_preview_images", "fetch_preview_images_replacing", "translate_names"]

    # Grouped by what the host is deciding, and narrowed by channel: the fields
    # a WhatsApp service needs are not the ones a Viator service needs, and
    # showing all of them invites filling in the ignored ones.
    fieldsets = (
        (
            "What guests see",
            {
                "fields": (
                    "name_en", "name_es", "name_fr",
                    "description_en", "description_es", "description_fr",
                    "image",
                ),
                "description": (
                    "Write the English name; Spanish and French are filled in on save "
                    "if left blank. The image is fetched from an Airbnb link automatically. "
                    "Descriptions are stored but not shown on the card yet."
                ),
            },
        ),
        (
            "Where the button sends them",
            {"fields": ("channel",)},
        ),
        (
            "Booking link",
            {
                "classes": ("referral-only",),
                "fields": ("referral_url",),
                "description": (
                    "Paste the whole product URL. Viator links get your affiliate id and "
                    "the venue's campaign added automatically — do not build them by hand."
                ),
            },
        ),
        (
            "WhatsApp routing",
            {
                "classes": ("whatsapp-only",),
                "fields": ("default_provider", "keywords"),
                "description": (
                    "Who the referral is forwarded to, and the words that match this "
                    "service when a guest writes their own message."
                ),
            },
        ),
        (
            "Money",
            {
                "fields": ("expected_commission_usd",),
                "description": "Used as the default amount when recording a commission.",
            },
        ),
        ("Listing", {"fields": ("slug", "sort_order", "active")}),
    )

    class Media:
        js = ("concierge/admin/service_form.js",)

    def save_model(self, request, obj, form, change):
        """Grab the card image as soon as a referral link is set.

        Only on admin saves, and only when the service has no image of its own:
        a photo uploaded by hand always wins. A failed fetch is a warning on the
        save, never an error — the service is already saved by then, and the
        card falls back to the plain gradient banner.
        """
        super().save_model(request, obj, form, change)
        self._fill_translations(request, obj)

        link_is_new = not change or "referral_url" in getattr(form, "changed_data", [])
        if not (obj.referral_url and link_is_new):
            return

        if not points_at_a_listing(obj.referral_url):
            self.message_user(
                request,
                "This looks like a shared Airbnb search, not a single experience — "
                "guests will land on a list of results. Open the experience itself "
                "and use its Share → Copy link.",
                level=messages.WARNING,
            )
            return

        # A changed link means the current image belongs to a different
        # listing, so keeping it is worse than replacing it — but only images
        # this code fetched are ours to overwrite. Anything uploaded by hand
        # has a different filename and always wins.
        if obj.image and not _is_fetched_image(obj):
            return

        try:
            preview = fetch_preview(obj.referral_url)
        except PreviewError as exc:
            self.message_user(
                request,
                f"Saved, but no card image could be fetched from the referral link: {exc}",
                level=messages.WARNING,
            )
            return

        obj.image.save(f"{obj.slug}-preview.jpg", preview.content, save=True)
        note = f" from “{preview.title}”" if preview.title else ""
        self.message_user(request, f"Card image fetched{note}.")

    def _fill_translations(self, request, service) -> None:
        """Translate the English name into any language left blank.

        A failure is a warning, never an error: the landing page falls back to
        the English name, so a missing translation is a cosmetic gap and not
        worth interrupting a save for.
        """
        if not getattr(settings, "TRANSLATE_SERVICE_NAMES", True):
            return
        try:
            filled = fill_missing_names(service)
        except TranslationError as exc:
            self.message_user(
                request,
                f"Saved, but the name could not be translated: {exc}. "
                "The page will show the English name until you fill it in.",
                level=messages.WARNING,
            )
            return
        if filled:
            Service.objects.filter(pk=service.pk).update(
                **{f"name_{code}": getattr(service, f"name_{code}") for code in filled}
            )
            names = ", ".join(f"{c.upper()}: “{getattr(service, f'name_{c}')}”" for c in filled)
            self.message_user(
                request,
                f"Translated the name — {names}. Machine translation, so check it reads well.",
            )

    @admin.display(description="link set", boolean=True, ordering="referral_url")
    def has_referral(self, obj: Service) -> bool:
        return bool(obj.referral_url)

    @admin.action(description="Translate missing Spanish / French names from English")
    def translate_names(self, request, queryset):
        done = 0
        for service in queryset:
            before = (service.name_es, service.name_fr)
            self._fill_translations(request, service)
            if (service.name_es, service.name_fr) != before:
                done += 1
        if not done:
            self.message_user(
                request, "Nothing to translate — every selected service already has both names."
            )

    @admin.action(description="Fetch card image from referral link (skip ones that have an image)")
    def fetch_preview_images(self, request, queryset):
        self._fetch_previews(request, queryset, replace=False)

    @admin.action(description="Fetch card image from referral link (REPLACE existing images)")
    def fetch_preview_images_replacing(self, request, queryset):
        self._fetch_previews(request, queryset, replace=True)

    def _fetch_previews(self, request, queryset, *, replace: bool) -> None:
        done = skipped = 0
        for service in queryset:
            if not service.referral_url:
                self.message_user(
                    request, f"{service.name_en}: no referral URL set.", level=messages.WARNING
                )
                continue
            if service.image and not replace:
                skipped += 1
                continue
            try:
                preview = fetch_preview(service.referral_url)
            except PreviewError as exc:
                self.message_user(request, f"{service.name_en}: {exc}", level=messages.ERROR)
                continue
            service.image.save(f"{service.slug}-preview.jpg", preview.content, save=True)
            done += 1
            note = f" (source title: {preview.title})" if preview.title else ""
            self.message_user(request, f"{service.name_en}: image updated{note}.")

        if skipped:
            self.message_user(
                request,
                f"{skipped} service(s) already had an image — use the REPLACE action to overwrite.",
                level=messages.WARNING,
            )
        if done:
            self.message_user(request, f"{done} image(s) fetched.")


@admin.register(Location)
class LocationAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "qr_url", "qr_downloads", "menu", "scans_30d", "clicks_30d", "ctr_30d", "active")
    list_filter = ("kind", "active")
    search_fields = ("name", "slug", "contact_name", "notes")
    prepopulated_fields = {"slug": ("name",)}
    filter_horizontal = ("services",)
    readonly_fields = ("created_at", "qr_preview")
    fieldsets = (
        ("The business", {"fields": ("name", "slug", "kind", "active", "contact_name", "notes")}),
        (
            "Its page",
            {
                "fields": (
                    "tab_title",
                    "headline_en", "headline_es", "headline_fr",
                    "tagline_en", "tagline_es", "tagline_fr",
                    "footer_text",
                ),
                "description": (
                    "This venue's own wording. Leave a field blank to use the site-wide "
                    "default from Site settings."
                ),
            },
        ),
        ("Menu", {"fields": ("services",)}),
        ("Attribution", {"fields": ("campaign_code",)}),
        ("QR code", {"fields": ("qr_preview", "created_at")}),
    )

    def get_queryset(self, request):
        since = timezone.now() - datetime.timedelta(days=30)
        recent = Q(events__created_at__gte=since)
        return super().get_queryset(request).annotate(
            _scans=Count("events", filter=recent & Q(events__kind=LocationEvent.Kind.SCAN), distinct=False),
            _clicks=Count("events", filter=recent & Q(events__kind=LocationEvent.Kind.CLICK), distinct=False),
        )

    @admin.display(description="QR code")
    def qr_downloads(self, obj: Location) -> str:
        if not obj.pk:
            return "—"
        svg = reverse("location_qr", args=[obj.slug, "svg"])
        png = reverse("location_qr", args=[obj.slug, "png"])
        return format_html(
            '<a href="{}?download=1">SVG</a> · <a href="{}?download=1">PNG</a>', svg, png
        )

    @admin.display(description="QR code")
    def qr_preview(self, obj: Location) -> str:
        """Shown on the change form so the code can be checked before printing."""
        if not obj.pk:
            return "Save the location first, then its QR code appears here."
        svg = reverse("location_qr", args=[obj.slug, "svg"])
        png = reverse("location_qr", args=[obj.slug, "png"])
        return format_html(
            '<div><img src="{}" alt="QR code for {}" style="width:200px;height:200px">'
            '<p style="margin:6px 0 0">Encodes <code>/{}</code><br>'
            'Download: <a href="{}?download=1">SVG (for print)</a> · '
            '<a href="{}?download=1">PNG</a></p></div>',
            svg, obj.name, obj.slug, svg, png,
        )

    @admin.display(description="QR points at")
    def qr_url(self, obj: Location) -> str:
        return format_html('<a href="/{}" target="_blank">/{}</a>', obj.slug, obj.slug)

    @admin.display(description="menu")
    def menu(self, obj: Location) -> str:
        chosen = obj.services.count()
        return f"{chosen} chosen" if chosen else "all services"

    @admin.display(description="scans (30d)", ordering="_scans")
    def scans_30d(self, obj: Location) -> int:
        return obj._scans

    @admin.display(description="clicks (30d)", ordering="_clicks")
    def clicks_30d(self, obj: Location) -> int:
        return obj._clicks

    @admin.display(description="click rate (30d)")
    def ctr_30d(self, obj: Location) -> str:
        # Scans are page opens, so a rate above 100% just means guests tapped
        # more than one service — worth seeing rather than clamping.
        return f"{obj._clicks / obj._scans:.0%}" if obj._scans else "—"


@admin.register(LocationEvent)
class LocationEventAdmin(admin.ModelAdmin):
    list_display = ("created_at", "kind", "location", "service", "channel")
    list_filter = ("kind", "channel", "location")
    date_hierarchy = "created_at"
    readonly_fields = ("created_at", "kind", "location", "service", "channel")

    def has_add_permission(self, request) -> bool:
        return False  # written by the site, never by hand


@admin.register(Commission)
class CommissionAdmin(admin.ModelAdmin):
    list_display = (
        "occurred_on", "channel", "location", "service", "owed_by",
        "gross_usd", "status", "venue_share", "payout_status",
    )
    list_filter = ("status", "payout_status", "channel", "location")
    search_fields = ("reference", "notes", "location__name", "provider__name")
    date_hierarchy = "occurred_on"
    autocomplete_fields = ()
    readonly_fields = ("venue_share_usd", "created_at")
    actions = ["mark_claimed", "mark_received", "mark_venue_paid"]

    @admin.display(description="owed by")
    def owed_by(self, obj: Commission) -> str:
        """Who you collect from — the whole point of the WhatsApp column."""
        if obj.channel == Service.Channel.WHATSAPP:
            return obj.provider.name if obj.provider else "— (no provider set)"
        return obj.get_channel_display().split(" — ")[0]

    @admin.display(description="venue share")
    def venue_share(self, obj: Commission) -> str:
        if obj.venue_share_usd <= 0:
            return "—"
        return f"${obj.venue_share_usd} ({obj.venue_share_pct:g}%)"

    def changelist_view(self, request, extra_context=None):
        """Put the three numbers that matter at the top of the list.

        The filters apply, so "what do I owe La Bahía this month" is a filter
        away rather than a spreadsheet export.
        """
        response = super().changelist_view(request, extra_context)
        try:
            qs = response.context_data["cl"].queryset
        except (AttributeError, KeyError):
            return response

        def total(queryset, field):
            # SQLite returns SUM(decimal) with float artefacts (21.6000000000000);
            # quantize so the figures are exact money, not just rounded on screen.
            value = queryset.aggregate(t=Sum(field))["t"] or decimal.Decimal("0")
            return decimal.Decimal(value).quantize(decimal.Decimal("0.01"))

        outstanding = qs.exclude(status__in=[Commission.Status.RECEIVED, Commission.Status.WRITTEN_OFF])
        received = qs.filter(status=Commission.Status.RECEIVED)
        response.context_data["summary"] = {
            "to_collect": total(outstanding, "gross_usd"),
            "to_collect_count": outstanding.count(),
            "received": total(received, "gross_usd"),
            "owed_to_venues": total(qs.filter(payout_status=Commission.Payout.OWED), "venue_share_usd"),
            "paid_to_venues": total(qs.filter(payout_status=Commission.Payout.PAID), "venue_share_usd"),
            "net_kept": total(received, "gross_usd") - total(received, "venue_share_usd"),
        }
        return response

    @admin.action(description="Mark as claimed / invoiced")
    def mark_claimed(self, request, queryset):
        n = queryset.exclude(status=Commission.Status.RECEIVED).update(status=Commission.Status.CLAIMED)
        self.message_user(request, f"{n} commission(s) marked as claimed.")

    @admin.action(description="Mark as received")
    def mark_received(self, request, queryset):
        n = queryset.update(status=Commission.Status.RECEIVED)
        self.message_user(request, f"{n} commission(s) marked as received.")

    @admin.action(description="Mark the venue's share as paid")
    def mark_venue_paid(self, request, queryset):
        n = queryset.filter(payout_status=Commission.Payout.OWED).update(
            payout_status=Commission.Payout.PAID
        )
        self.message_user(request, f"{n} venue payout(s) marked as paid.")


@admin.register(Provider)
class ProviderAdmin(admin.ModelAdmin):
    list_display = ("name", "phone", "active", "created_at")
    list_filter = ("active", "services")
    search_fields = ("name", "phone", "notes")
    filter_horizontal = ("services",)


@admin.register(Guest)
class GuestAdmin(admin.ModelAdmin):
    list_display = ("phone", "name", "first_seen", "last_seen")
    search_fields = ("phone", "name")
    readonly_fields = ("first_seen",)


class MessageInline(admin.TabularInline):
    model = Message
    extra = 0
    fields = ("created_at", "direction", "from_phone", "to_phone", "body")
    readonly_fields = ("created_at", "direction", "from_phone", "to_phone", "body")
    can_delete = False
    show_change_link = True


@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    list_display = (
        "short_code", "guest", "service", "provider", "location", "status",
        "expected_commission_usd", "commission_recorded", "created_at", "thread_link",
    )
    list_filter = ("status", "service", "provider", "location")
    search_fields = ("short_code", "guest__phone", "guest__name", "notes")
    readonly_fields = ("short_code", "created_at", "raw_first_message", "extracted_fields", "thread_link")
    inlines = [MessageInline]
    actions = ["record_commission", "mark_completed", "mark_no_show", "mark_cancelled"]

    @admin.display(description="commission", boolean=True)
    def commission_recorded(self, obj: Ticket) -> bool:
        return obj.commissions.exists()

    @admin.action(description="Record the commission to claim from the provider")
    def record_commission(self, request, queryset):
        """Open a claim for each ticket, so WhatsApp referrals are chased too.

        Airbnb and Viator report their own commissions; a lanchero does not,
        which is exactly why these need recording by hand.
        """
        created = skipped = 0
        for ticket in queryset.select_related("service", "provider", "location"):
            if ticket.commissions.exists():
                skipped += 1
                continue
            Commission.objects.create(
                occurred_on=ticket.created_at.date(),
                location=ticket.location,
                service=ticket.service,
                channel=Service.Channel.WHATSAPP,
                provider=ticket.provider,
                ticket=ticket,
                gross_usd=ticket.expected_commission_usd,
                reference=ticket.short_code,
            )
            created += 1
        if created:
            self.message_user(
                request,
                f"{created} commission(s) recorded as expected. "
                "Amounts come from the service's expected commission — edit any that differed.",
            )
        if skipped:
            self.message_user(
                request, f"{skipped} ticket(s) already had a commission.", level=messages.WARNING
            )

    @admin.display(description="Chat view")
    def thread_link(self, obj: Ticket) -> str:
        url = reverse("ticket_thread", args=[obj.short_code])
        return format_html('<a href="{}" target="_blank">Open thread →</a>', url)

    @admin.action(description="Mark selected tickets as completed")
    def mark_completed(self, request, queryset):
        for t in queryset:
            t.close(Ticket.Status.COMPLETED)

    @admin.action(description="Mark selected tickets as no-show")
    def mark_no_show(self, request, queryset):
        for t in queryset:
            t.close(Ticket.Status.NO_SHOW)

    @admin.action(description="Mark selected tickets as cancelled")
    def mark_cancelled(self, request, queryset):
        for t in queryset:
            t.close(Ticket.Status.CANCELLED)


@admin.register(SiteSettings)
class SiteSettingsAdmin(admin.ModelAdmin):
    # NB: fieldsets are a whitelist — a new model field is invisible in the
    # admin until it is listed here.
    fieldsets = (
        (
            "Service buttons",
            {
                "fields": (
                    "cta_mode",
                    "referral_cta_en",
                    "referral_cta_es",
                    "referral_cta_fr",
                    "referral_disclosure",
                    "viator_partner_id",
                    "viator_mcid",
                    "viator_campaign",
                ),
                "description": (
                    "Set a Referral URL on each service, then choose a mode here. "
                    "Services without a referral link keep their WhatsApp button "
                    "whichever mode is selected."
                ),
            },
        ),
        ("Tab & footer", {"fields": ("tab_title", "footer_text")}),
        ("Headline (big h1)", {"fields": ("headline_en", "headline_es", "headline_fr")}),
        ("Tagline (subtitle under h1)", {"fields": ("tagline_en", "tagline_es", "tagline_fr")}),
    )

    def has_add_permission(self, request) -> bool:
        return not SiteSettings.objects.exists()

    def has_delete_permission(self, request, obj=None) -> bool:
        return False


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = (
        "created_at", "direction", "delivered", "from_phone", "to_phone", "ticket", "body_short",
    )
    # delivery_status first: "did anything fail to reach someone?" is the
    # question worth answering at a glance.
    list_filter = ("delivery_status", "direction")
    search_fields = ("body", "from_phone", "to_phone", "wa_message_id", "delivery_error")
    readonly_fields = ("created_at", "raw_payload", "wa_message_id", "delivery_error")

    @admin.display(description="delivery", ordering="delivery_status")
    def delivered(self, obj: Message) -> str:
        if obj.delivery_status == Message.Delivery.FAILED:
            return format_html(
                '<span style="color:#b3261e;font-weight:600" title="{}">✕ failed</span>',
                obj.delivery_error or "unknown error",
            )
        if obj.delivery_status == Message.Delivery.DRY_RUN:
            return format_html('<span style="color:#8a6d00">dry run</span>')
        if obj.delivery_status == Message.Delivery.SENT:
            return format_html('<span style="color:#1e7b34">✓ sent</span>')
        return ""  # inbound

    @admin.display(description="body")
    def body_short(self, obj: Message) -> str:
        return (obj.body[:60] + "…") if len(obj.body) > 60 else obj.body
