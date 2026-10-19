"""Group the admin index by the job being done, not by the alphabet.

Django lists every model of an app in one alphabetical block, which put
"Location events" — a debugging table — between the two screens used most.
The sections below are ordered the way the work runs: set up what a guest
sees, get cards onto walls, settle up, and only then the records and the
configuration.

Implemented by overriding `get_app_list` on a custom AdminSite, installed via
`GuestlinkAdminConfig` so that `django.contrib.admin.site` *is* this class.
Every existing `@admin.register(...)` keeps working untouched, and auth's
User/Group register themselves as usual.
"""

from __future__ import annotations

from django.contrib.admin.apps import AdminConfig
from django.contrib.admin.sites import AdminSite
from django.urls import reverse

# Model class names, in the order they should appear inside their section.
# Anything not listed still shows up, under "Other" — a new model must never
# vanish from the index just because nobody added it here.
SECTIONS = (
    ("Guest-facing", ("Location", "Service")),
    ("QR cards", ("QrBatch", "QrTag")),
    ("Bookings & money", ("Ticket", "Commission", "DamageDeposit", "Provider")),
    ("Records", ("Guest", "Message", "LocationEvent")),
    ("Settings", ("SiteSettings", "User", "Group")),
)


class GuestlinkAdminSite(AdminSite):
    site_header = "Guestlink"
    site_title = "Guestlink admin"
    index_title = ""

    def get_app_list(self, request, app_label=None):
        # The per-app page (/admin/concierge/) genuinely is one app's models;
        # regrouping there would be wrong, and Django builds that page with the
        # same method.
        if app_label:
            return super().get_app_list(request, app_label)

        models_by_name = {}
        for app in super().get_app_list(request):
            for model in app["models"]:
                models_by_name[model["object_name"]] = model

        app_list = []
        for title, object_names in SECTIONS:
            models = [
                models_by_name.pop(name) for name in object_names if name in models_by_name
            ]
            if models:
                app_list.append(self._section(title, models))

        # Whatever the map did not account for, so nothing is ever hidden by
        # an omission here.
        if models_by_name:
            leftovers = sorted(models_by_name.values(), key=lambda m: m["name"])
            app_list.append(self._section("Other", leftovers))

        return app_list

    def _section(self, title: str, models: list) -> dict:
        """One index block.

        `app_label` is a slug the index template uses for the section's id and
        CSS class; it is not a real app. The template links the heading to
        `app_url`, and a section has no page of its own — so it points back at
        the index rather than being left empty, which browsers would resolve to
        the current URL anyway but renders as a bare `href=""`.
        """
        return {
            "name": title,
            "app_label": title.lower().replace(" ", "-").replace("&", "and"),
            "app_url": reverse("admin:index", current_app=self.name),
            "has_module_perms": True,
            "models": models,
        }


class GuestlinkAdminConfig(AdminConfig):
    default_site = "concierge.admin_site.GuestlinkAdminSite"
