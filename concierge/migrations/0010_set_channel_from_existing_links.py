"""Give existing services a channel matching what they already do.

`channel` defaults to WhatsApp, but services that already have a referral URL
are being rendered as referral cards today. Without this, deploying the new
field would silently flip them all back to WhatsApp.

Every referral link in production at the time of writing is an Airbnb one, so
the host name decides the channel and anything unrecognised stays on WhatsApp
rather than being guessed at.
"""

from django.db import migrations


def set_channel(apps, schema_editor):
    Service = apps.get_model("concierge", "Service")
    for service in Service.objects.exclude(referral_url=""):
        url = service.referral_url.lower()
        if "airbnb." in url:
            service.channel = "airbnb"
        elif "viator." in url:
            service.channel = "viator"
        else:
            continue
        service.save(update_fields=["channel"])


def unset_channel(apps, schema_editor):
    apps.get_model("concierge", "Service").objects.update(channel="whatsapp")


class Migration(migrations.Migration):
    dependencies = [("concierge", "0009_service_channel_alter_service_referral_url_and_more")]

    operations = [migrations.RunPython(set_channel, unset_channel)]
