"""Seed a handful of demo services + providers so the admin and landing page
have something to render locally. Idempotent.

Usage:
    uv run python manage.py seed_demo
"""

from django.core.management.base import BaseCommand

from concierge.models import Provider, Service


class Command(BaseCommand):
    help = "Seed demo services and providers for local development."

    def handle(self, *args, **options):
        providers = {
            "saona": ("María (Saona)", "+18091111111"),
            "taxi": ("Pedro Taxi", "+18092222222"),
            "rental": ("AutoRD Bayahibe", "+18093333333"),
            "restaurant": ("La Bahía Delivery", "+18094444444"),
        }
        provider_objs = {}
        for key, (name, phone) in providers.items():
            obj, _ = Provider.objects.get_or_create(phone=phone, defaults={"name": name})
            provider_objs[key] = obj
            self.stdout.write(f"  provider: {obj.name} ({obj.phone})")

        services = [
            {
                "slug": "saona",
                "name_en": "Saona Island excursion",
                "name_es": "Excursión Isla Saona",
                "description_en": "Full-day boat trip to Isla Saona with lunch.",
                "description_es": "Excursión de día completo a Isla Saona con almuerzo.",
                "keywords": "saona, island, isla, excursion, excursión, boat, lancha",
                "default_provider": provider_objs["saona"],
                "expected_commission_usd": 15,
                "sort_order": 10,
            },
            {
                "slug": "airport-taxi",
                "name_en": "Airport taxi",
                "name_es": "Taxi al aeropuerto",
                "description_en": "Private transfer to SDQ / PUJ airports.",
                "description_es": "Traslado privado a aeropuertos SDQ / PUJ.",
                "keywords": "airport, aeropuerto, taxi, transfer, sdq, puj",
                "default_provider": provider_objs["taxi"],
                "expected_commission_usd": 5,
                "sort_order": 20,
            },
            {
                "slug": "car-rental",
                "name_en": "Car rental",
                "name_es": "Alquiler de auto",
                "description_en": "Daily and weekly car rentals, delivery to the apartment.",
                "description_es": "Alquiler de autos por día o semana, entrega en el apartamento.",
                "keywords": "rental, alquiler, car, auto, carro",
                "default_provider": provider_objs["rental"],
                "expected_commission_usd": 10,
                "sort_order": 30,
            },
            {
                "slug": "food-delivery",
                "name_en": "Food delivery",
                "name_es": "Delivery de comida",
                "description_en": "Local restaurants with delivery to the apartment.",
                "description_es": "Restaurantes locales con entrega en el apartamento.",
                "keywords": "food, comida, delivery, restaurant, restaurante",
                "default_provider": provider_objs["restaurant"],
                "expected_commission_usd": 3,
                "sort_order": 40,
            },
        ]
        for spec in services:
            slug = spec.pop("slug")
            obj, created = Service.objects.update_or_create(slug=slug, defaults=spec)
            self.stdout.write(f"  service: {obj.slug} ({'created' if created else 'updated'})")

        self.stdout.write(self.style.SUCCESS("Done."))
