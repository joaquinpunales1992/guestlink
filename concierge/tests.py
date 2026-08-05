"""Smoke tests for the relay routing logic.

Runs with WHATSAPP_DRY_RUN=1 (default) so no network calls are made — outbound
messages are still persisted as Message rows, which is what we assert against.
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase, override_settings

from concierge.classifier import Classification
from concierge.models import Guest, Message, Provider, Service, SiteSettings, Ticket
from concierge.relay import CODE_RE, _strip_code, handle_inbound, normalize_phone
from concierge.whatsapp import WhatsAppError


@override_settings(WHATSAPP_DRY_RUN=True, ANTHROPIC_API_KEY="")
class RelayTests(TestCase):
    def setUp(self) -> None:
        self.saona_provider = Provider.objects.create(name="María (Saona)", phone="+18091111111")
        self.taxi_provider = Provider.objects.create(name="Pedro Taxi", phone="+18092222222")

        self.saona = Service.objects.create(
            slug="saona",
            name_en="Saona Island excursion",
            name_es="Excursión Isla Saona",
            keywords="saona, island, isla, excursion, excursión",
            default_provider=self.saona_provider,
            expected_commission_usd=15,
        )
        self.taxi = Service.objects.create(
            slug="airport-taxi",
            name_en="Airport taxi",
            name_es="Taxi al aeropuerto",
            keywords="airport, aeropuerto, taxi, transfer",
            default_provider=self.taxi_provider,
            expected_commission_usd=5,
        )

    # ---- helpers ----------------------------------------------------------

    def _msgs(self, direction: str | None = None):
        qs = Message.objects.all()
        if direction:
            qs = qs.filter(direction=direction)
        return list(qs.order_by("created_at"))

    # ---- code parsing -----------------------------------------------------

    def test_code_regex_matches_brackets(self) -> None:
        m = CODE_RE.search("[A47B3] sí podemos llevarlo")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1).upper(), "A47B3")

    def test_strip_code_removes_first_bracket_group(self) -> None:
        self.assertEqual(_strip_code("[A47B3] Sí, claro"), "Sí, claro")
        self.assertEqual(_strip_code("Sí, claro"), "Sí, claro")

    def test_normalize_phone_strips_punctuation(self) -> None:
        self.assertEqual(normalize_phone("+1 (809) 222-3333"), "+18092223333")
        self.assertEqual(normalize_phone("  "), "")

    # ---- guest → first message → new ticket -------------------------------

    def test_guest_first_message_creates_ticket_and_intros_provider(self) -> None:
        outcome = handle_inbound(
            from_phone="+4915112345678",
            body="Hi, I'd like info about the Saona excursion for tomorrow",
        )
        self.assertTrue(outcome.handled)
        self.assertIsNotNone(outcome.ticket)
        self.assertEqual(outcome.ticket.service, self.saona)
        self.assertEqual(outcome.ticket.provider, self.saona_provider)
        self.assertEqual(outcome.ticket.status, Ticket.Status.OPEN)

        guest_in = self._msgs(Message.Direction.GUEST_IN)
        provider_out = self._msgs(Message.Direction.PROVIDER_OUT)
        guest_out = self._msgs(Message.Direction.GUEST_OUT)

        self.assertEqual(len(guest_in), 1)
        self.assertEqual(len(provider_out), 1)
        self.assertEqual(len(guest_out), 1)
        self.assertIn(f"[{outcome.ticket.short_code}]", provider_out[0].body)
        self.assertEqual(provider_out[0].to_phone, self.saona_provider.phone)
        self.assertEqual(guest_out[0].to_phone, "+4915112345678")

    def test_guest_second_message_routes_to_same_provider_with_prefix(self) -> None:
        first = handle_inbound(from_phone="+4915112345678", body="Hi, I want Saona excursion")
        Message.objects.all().delete()  # focus on the second exchange

        outcome = handle_inbound(from_phone="+4915112345678", body="we are 4 people, possible?")
        self.assertEqual(outcome.ticket, first.ticket)
        provider_out = self._msgs(Message.Direction.PROVIDER_OUT)
        self.assertEqual(len(provider_out), 1)
        self.assertTrue(provider_out[0].body.startswith(f"[{first.ticket.short_code}]"))
        self.assertEqual(provider_out[0].to_phone, self.saona_provider.phone)

    # ---- provider → guest with code --------------------------------------

    def test_provider_with_code_routes_back_to_guest_and_strips_code(self) -> None:
        first = handle_inbound(from_phone="+4915112345678", body="Hi, Saona excursion please")
        Message.objects.all().delete()
        code = first.ticket.short_code

        outcome = handle_inbound(
            from_phone=self.saona_provider.phone,
            body=f"[{code}] sí, podemos llevarlos. USD 60 por persona.",
        )
        self.assertEqual(outcome.ticket, first.ticket)
        guest_out = self._msgs(Message.Direction.GUEST_OUT)
        self.assertEqual(len(guest_out), 1)
        self.assertEqual(guest_out[0].to_phone, "+4915112345678")
        self.assertNotIn("[", guest_out[0].body)
        self.assertIn("USD 60", guest_out[0].body)
        first.ticket.refresh_from_db()
        self.assertEqual(first.ticket.status, Ticket.Status.IN_PROGRESS)

    def test_provider_without_code_but_single_active_ticket_routes_anyway(self) -> None:
        first = handle_inbound(from_phone="+4915112345678", body="Hi, Saona please")
        Message.objects.all().delete()

        outcome = handle_inbound(from_phone=self.saona_provider.phone, body="sí, llegamos a las 7am")
        self.assertEqual(outcome.ticket, first.ticket)
        guest_out = self._msgs(Message.Direction.GUEST_OUT)
        self.assertEqual(len(guest_out), 1)

    def test_provider_without_code_and_multiple_active_tickets_asks_for_code(self) -> None:
        # Two guests, both with active tickets pointing at the same provider.
        handle_inbound(from_phone="+4915100000001", body="Saona please")
        handle_inbound(from_phone="+4915100000002", body="Saona excursion thanks")
        Message.objects.all().delete()

        outcome = handle_inbound(from_phone=self.saona_provider.phone, body="ok confirmado")
        self.assertTrue(outcome.handled)
        self.assertIsNone(outcome.ticket)
        system_out = self._msgs(Message.Direction.SYSTEM_OUT)
        self.assertEqual(len(system_out), 1)
        self.assertIn("código", system_out[0].body.lower())

    # ---- unmatched service -----------------------------------------------

    def test_guest_message_with_no_service_match_pings_host_fallback(self) -> None:
        outcome = handle_inbound(from_phone="+4915199999999", body="hola necesito ayuda con algo raro")
        self.assertTrue(outcome.handled)
        self.assertIsNone(outcome.ticket)
        system_out = self._msgs(Message.Direction.SYSTEM_OUT)
        self.assertEqual(len(system_out), 1)
        # No ticket → no PROVIDER_OUT message
        self.assertEqual(len(self._msgs(Message.Direction.PROVIDER_OUT)), 0)

    # ---- the proxy must not leak either side's number ---------------------

    def test_provider_intro_never_contains_the_guest_phone(self) -> None:
        guest_phone = "+4915112345678"
        outcome = handle_inbound(from_phone=guest_phone, body="Hi, Saona excursion please")
        intro = self._msgs(Message.Direction.PROVIDER_OUT)[0].body
        self.assertNotIn(guest_phone, intro)
        self.assertNotIn(guest_phone.lstrip("+"), intro)
        # Falls back to the ticket code so the provider still has a handle.
        self.assertIn(f"Huésped: {outcome.ticket.short_code}", intro)

    def test_provider_intro_uses_the_guest_name_when_known(self) -> None:
        Guest.objects.create(phone="+4915112345678", name="Lena")
        handle_inbound(from_phone="+4915112345678", body="Hi, Saona excursion please")
        intro = self._msgs(Message.Direction.PROVIDER_OUT)[0].body
        self.assertIn("Huésped: Lena", intro)
        self.assertNotIn("4915112345678", intro)

    # ---- a failed send must not destroy the conversation -------------------

    def test_failed_send_still_keeps_ticket_and_messages(self) -> None:
        """An expired token used to erase the guest's request entirely.

        handle_inbound is atomic and the webhook view swallows exceptions, so a
        raising send_text rolled back the inbound row and the ticket, returned
        200 to Meta, and left no trace of what the guest asked for.
        """
        with patch("concierge.relay.send_text", side_effect=WhatsAppError("(#190) token expired")):
            outcome = handle_inbound(from_phone="+4915112345678", body="Hi, Saona excursion please")

        self.assertTrue(outcome.handled)
        self.assertTrue(outcome.delivery_failed)
        self.assertIsNotNone(outcome.ticket)
        # The evidence survives.
        self.assertEqual(Ticket.objects.count(), 1)
        self.assertEqual(Guest.objects.count(), 1)
        self.assertEqual(len(self._msgs(Message.Direction.GUEST_IN)), 1)

        failed = Message.objects.filter(delivery_status=Message.Delivery.FAILED)
        self.assertEqual(failed.count(), 2)  # provider intro + guest ack
        self.assertIn("token expired", failed.first().delivery_error)

    def test_successful_send_is_marked_dry_run_under_dry_run(self) -> None:
        handle_inbound(from_phone="+4915112345678", body="Hi, Saona excursion please")
        statuses = set(
            Message.objects.exclude(delivery_status="").values_list("delivery_status", flat=True)
        )
        self.assertEqual(statuses, {Message.Delivery.DRY_RUN})
        # Inbound rows carry no delivery status at all.
        self.assertEqual(
            Message.objects.get(direction=Message.Direction.GUEST_IN).delivery_status, ""
        )

    def test_delivery_failed_is_false_on_a_clean_run(self) -> None:
        outcome = handle_inbound(from_phone="+4915112345678", body="Hi, Saona excursion please")
        self.assertFalse(outcome.delivery_failed)

    # ---- provider phone normalization -------------------------------------

    def test_provider_phone_is_normalized_on_save(self) -> None:
        p = Provider.objects.create(name="Messy", phone="+1 (809) 444-5555")
        p.refresh_from_db()
        self.assertEqual(p.phone, "+18094445555")

    def test_provider_typed_with_punctuation_still_routes_replies(self) -> None:
        """A number entered as '+1 809-333 4444' must still match inbound messages.

        Without normalization on save the lookup in handle_inbound misses, and
        the provider's reply is misread as a brand-new guest enquiry.
        """
        provider = Provider.objects.create(name="Sloppy Entry", phone="+1 809-333 4444")
        self.saona.default_provider = provider
        self.saona.save()
        first = handle_inbound(from_phone="+4915112345678", body="Hi, Saona excursion please")
        Message.objects.all().delete()

        outcome = handle_inbound(from_phone="+18093334444", body=f"[{first.ticket.short_code}] sí, disponible")

        self.assertEqual(outcome.ticket, first.ticket)
        self.assertEqual(outcome.note, "forwarded provider→guest")
        # Routed as a provider, so no second guest/ticket was invented.
        self.assertEqual(Guest.objects.count(), 1)
        self.assertEqual(Ticket.objects.count(), 1)


@override_settings(WHATSAPP_DRY_RUN=True, ANTHROPIC_API_KEY="sk-test")
class ClassifierIntegrationTests(TestCase):
    """When ANTHROPIC_API_KEY is set, the relay should call the Claude classifier.

    We patch the classifier itself so no network call happens.
    """

    def setUp(self) -> None:
        self.provider = Provider.objects.create(name="María", phone="+18091111111")
        self.service = Service.objects.create(
            slug="saona",
            name_en="Saona Island excursion",
            name_es="Excursión Isla Saona",
            keywords="",  # force LLM path
            default_provider=self.provider,
        )

    def test_relay_uses_classifier_result(self) -> None:
        fake = Classification(service_slug="saona", confidence=0.92, extracted_fields={"party_size": 4}, backend="claude")
        with patch("concierge.relay.classify", return_value=fake):
            outcome = handle_inbound(from_phone="+4912345", body="something cryptic the keywords would miss")
        self.assertIsNotNone(outcome.ticket)
        self.assertEqual(outcome.ticket.service, self.service)
        self.assertEqual(outcome.ticket.extracted_fields, {"party_size": 4})


@override_settings(
    ALLOWED_HOSTS=["testserver"],
    WHATSAPP_BUSINESS_NUMBER="573222448409",
    HOST_APARTMENT_LABEL="Reef",
)
class LandingCtaModeTests(TestCase):
    """The landing page can send guests to a referral link, to WhatsApp, or both."""

    WA = "https://wa.me/573222448409?text="
    REF = "https://www.airbnb.com/experiences/12345?ref=abc"

    def setUp(self) -> None:
        # One service with a referral link, one without — the mixed catalogue
        # is the case that matters, since links get filled in gradually.
        Service.objects.create(slug="saona", name_en="Saona", name_es="Saona", referral_url=self.REF)
        Service.objects.create(slug="taxi", name_en="Airport taxi", name_es="Taxi")

    def _render(self, mode: str) -> str:
        site = SiteSettings.load()
        site.cta_mode = mode
        site.save()
        return self.client.get("/").content.decode()

    def test_whatsapp_mode_ignores_referral_urls(self) -> None:
        html = self._render(SiteSettings.CtaMode.WHATSAPP)
        self.assertNotIn(self.REF, html)
        self.assertIn(self.WA, html)
        self.assertNotIn("commission", html)

    def test_referral_mode_uses_the_link_and_falls_back_per_service(self) -> None:
        html = self._render(SiteSettings.CtaMode.REFERRAL)
        self.assertIn(self.REF, html)
        # The taxi has no referral link, so its card must still reach WhatsApp
        # rather than rendering a dead end.
        self.assertIn(self.WA, html)
        self.assertIn("Book online", html)

    def test_both_mode_shows_whatsapp_under_the_referral_card(self) -> None:
        html = self._render(SiteSettings.CtaMode.BOTH)
        self.assertIn(self.REF, html)
        self.assertIn("Or ask us on WhatsApp", html)

    def test_referral_links_are_not_followed_and_open_safely(self) -> None:
        html = self._render(SiteSettings.CtaMode.REFERRAL)
        self.assertIn('rel="noopener nofollow sponsored"', html)
        self.assertIn('target="_blank"', html)

    def test_disclosure_appears_only_when_a_referral_link_is_shown(self) -> None:
        self.assertNotIn("commission", self._render(SiteSettings.CtaMode.WHATSAPP))
        self.assertIn("commission", self._render(SiteSettings.CtaMode.REFERRAL))

    def test_disclosure_hidden_when_no_service_has_a_link(self) -> None:
        Service.objects.filter(slug="saona").update(referral_url="")
        self.assertNotIn("commission", self._render(SiteSettings.CtaMode.REFERRAL))


class SiteSettingsAdminFormTests(TestCase):
    """Guard against the fieldsets whitelist hiding a field from the host.

    SiteSettingsAdmin lists fields explicitly, so a new model field is invisible
    in the admin until it is added there — silently, with no error anywhere.
    """

    def test_every_editable_field_is_reachable_in_the_admin(self) -> None:
        from django.contrib.admin.sites import site as admin_site

        from concierge.models import SiteSettings as S

        model_admin = admin_site._registry[S]
        in_fieldsets = {
            f for _, opts in model_admin.fieldsets for f in opts["fields"]
        }
        editable = {
            f.name for f in S._meta.get_fields()
            if getattr(f, "editable", False) and not f.auto_created and f.name != "id"
        }
        self.assertEqual(
            editable - in_fieldsets,
            set(),
            "SiteSettings fields missing from SiteSettingsAdmin.fieldsets",
        )
