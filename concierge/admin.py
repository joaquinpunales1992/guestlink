from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html

from .models import Guest, Message, Provider, Service, SiteSettings, Ticket


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = (
        "name_en", "slug", "has_referral", "default_provider",
        "expected_commission_usd", "active", "sort_order",
    )
    list_filter = ("active",)
    search_fields = ("name_en", "name_es", "name_fr", "slug", "keywords")

    @admin.display(description="referral link", boolean=True, ordering="referral_url")
    def has_referral(self, obj: Service) -> bool:
        return bool(obj.referral_url)


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
    list_display = ("short_code", "guest", "service", "provider", "status", "expected_commission_usd", "created_at", "thread_link")
    list_filter = ("status", "service", "provider")
    search_fields = ("short_code", "guest__phone", "guest__name", "notes")
    readonly_fields = ("short_code", "created_at", "raw_first_message", "extracted_fields", "thread_link")
    inlines = [MessageInline]
    actions = ["mark_completed", "mark_no_show", "mark_cancelled"]

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
    fieldsets = (
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
