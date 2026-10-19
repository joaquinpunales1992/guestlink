"""Fill the deposit wording on a site that already has a settings row.

SiteSettings._defaults() only runs when the singleton is first created, so an
existing install would come out of the previous migration with three blank
terms fields and a deposit page that explains nothing. This writes them once,
and only where the host has not written their own.
"""

from django.db import migrations

ENGLISH = (
    "This is a refundable security deposit held against damage. Your card is "
    "not charged — PayPal only reserves the amount, and we release it within "
    "48 hours of check-out once the apartment has been checked."
)
SPANISH = (
    "Es un depósito de garantía reembolsable por daños. No se cobra nada a tu "
    "tarjeta — PayPal solo reserva el importe, y lo liberamos en las 48 horas "
    "siguientes a tu salida, una vez revisado el apartamento."
)
FRENCH = (
    "Il s'agit d'une caution remboursable en cas de dégâts. Votre carte n'est "
    "pas débitée — PayPal ne fait que réserver le montant, et nous le libérons "
    "dans les 48 heures suivant votre départ, après vérification."
)


def fill_terms(apps, schema_editor):
    SiteSettings = apps.get_model("concierge", "SiteSettings")
    for site in SiteSettings.objects.all():
        fields = []
        for name, text in (
            ("deposit_terms_en", ENGLISH),
            ("deposit_terms_es", SPANISH),
            ("deposit_terms_fr", FRENCH),
        ):
            if not getattr(site, name, ""):
                setattr(site, name, text)
                fields.append(name)
        if fields:
            site.save(update_fields=fields)


def unfill(apps, schema_editor):
    """Clear only text this migration could have written, never the host's own."""
    SiteSettings = apps.get_model("concierge", "SiteSettings")
    for site in SiteSettings.objects.all():
        fields = []
        for name, text in (
            ("deposit_terms_en", ENGLISH),
            ("deposit_terms_es", SPANISH),
            ("deposit_terms_fr", FRENCH),
        ):
            if getattr(site, name, "") == text:
                setattr(site, name, "")
                fields.append(name)
        if fields:
            site.save(update_fields=fields)


class Migration(migrations.Migration):

    dependencies = [("concierge", "0019_sitesettings_deposit_amount_and_more")]

    operations = [migrations.RunPython(fill_terms, unfill)]
