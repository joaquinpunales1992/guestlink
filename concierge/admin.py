from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html

from .models import Guest, Message, Provider, Service, Ticket


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = ("name_en", "slug", "default_provider", "expected_commission_usd", "active", "sort_order")
    list_filter = ("active",)
    search_fields = ("name_en", "name_es", "slug", "keywords")


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


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ("created_at", "direction", "from_phone", "to_phone", "ticket", "body_short")
    list_filter = ("direction",)
    search_fields = ("body", "from_phone", "to_phone", "wa_message_id")
    readonly_fields = ("created_at", "raw_payload", "wa_message_id")

    @admin.display(description="body")
    def body_short(self, obj: Message) -> str:
        return (obj.body[:60] + "…") if len(obj.body) > 60 else obj.body
