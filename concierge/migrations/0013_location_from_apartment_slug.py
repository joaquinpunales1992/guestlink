"""Turn the single hard-coded apartment into the first Location.

Until now one apartment slug lived in settings (APARTMENT_SLUG) and was routed
by hand in urls.py. Locations replace that. The printed QR cards in the
apartment encode /the-reef-401, so the row has to exist with exactly that slug
or every card already on a wall stops working.

Its Viator campaign is left blank, which falls back to the slug — so bookings
from those cards are attributed without any further setup.
"""

from django.conf import settings
from django.db import migrations


def create_location(apps, schema_editor):
    Location = apps.get_model("concierge", "Location")
    slug = (getattr(settings, "APARTMENT_SLUG", "") or "").strip("/")
    if not slug or Location.objects.filter(slug=slug).exists():
        return
    label = getattr(settings, "HOST_APARTMENT_LABEL", "") or "Apartment"
    Location.objects.create(
        name=f"Apto {label}",
        slug=slug,
        kind="apartment",
        notes="Created automatically from the old APARTMENT_SLUG setting.",
    )


def delete_location(apps, schema_editor):
    slug = (getattr(settings, "APARTMENT_SLUG", "") or "").strip("/")
    if slug:
        apps.get_model("concierge", "Location").objects.filter(slug=slug).delete()


class Migration(migrations.Migration):
    dependencies = [("concierge", "0012_location_locationevent")]

    operations = [migrations.RunPython(create_location, delete_location)]
