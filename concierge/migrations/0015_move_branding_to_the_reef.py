"""Give the apartment the copy that used to be the whole site's.

Branding moved from the SiteSettings singleton onto each Location, because a
location is a business — a restaurant putting up a code is not "The Reef".

The singleton's values stay put as the fallback for venues with no copy of
their own, but they are also stamped onto the original apartment so its page is
byte-for-byte what it was before, rather than quietly inheriting whatever the
defaults later become.
"""

from django.conf import settings
from django.db import migrations

FIELDS = (
    "tab_title", "footer_text",
    "headline_en", "headline_es", "headline_fr",
    "tagline_en", "tagline_es", "tagline_fr",
)


def copy_to_apartment(apps, schema_editor):
    Location = apps.get_model("concierge", "Location")
    SiteSettings = apps.get_model("concierge", "SiteSettings")

    slug = (getattr(settings, "APARTMENT_SLUG", "") or "").strip("/")
    location = Location.objects.filter(slug=slug).first()
    site = SiteSettings.objects.first()
    if not location or not site:
        return

    for field in FIELDS:
        # Never clobber copy already written for this venue.
        if not getattr(location, field, ""):
            setattr(location, field, getattr(site, field, ""))
    location.save(update_fields=list(FIELDS))


def clear_apartment_copy(apps, schema_editor):
    slug = (getattr(settings, "APARTMENT_SLUG", "") or "").strip("/")
    apps.get_model("concierge", "Location").objects.filter(slug=slug).update(
        **{field: "" for field in FIELDS}
    )


class Migration(migrations.Migration):
    dependencies = [("concierge", "0014_location_footer_text_location_headline_en_and_more")]

    operations = [migrations.RunPython(copy_to_apartment, clear_apartment_copy)]
