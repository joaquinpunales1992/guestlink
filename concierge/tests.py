"""Smoke tests for the relay routing logic.

Runs with WHATSAPP_DRY_RUN=1 (default) so no network calls are made — outbound
messages are still persisted as Message rows, which is what we assert against.
"""

from __future__ import annotations

import html as html_lib
from io import BytesIO
from urllib.parse import parse_qsl, urlparse
from unittest.mock import Mock, patch

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.test import RequestFactory, TestCase, override_settings

from concierge.affiliate import is_viator, viator_url
from concierge.classifier import Classification
from concierge.models import Guest, Message, Provider, Service, SiteSettings, Ticket
from concierge.referral_preview import (
    PreviewError,
    canonical_page_url,
    fetch_preview,
    og_value,
    points_at_a_listing,
)
from concierge.relay import CODE_RE, _strip_code, handle_inbound, normalize_phone
from concierge.translate import TranslationError, fill_missing_names, translate
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
        Service.objects.create(
            slug="saona", name_en="Saona", name_es="Saona",
            channel=Service.Channel.AIRBNB, referral_url=self.REF,
        )
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


class ReferralPreviewTests(TestCase):
    """Resolving a card image from a referral link. No network in these tests."""

    RP = (
        "https://es-l.airbnb.com/rp/jpunales1?direct_open=true&p=recommendations"
        "&product=experience&listing_id=3015830&s=67&unique_share_id=f3e34064"
    )

    def test_canonical_url_resolves_the_experience_from_a_referral_link(self) -> None:
        # The /rp/ shim carries no og:image, so we must hop to the real page.
        self.assertEqual(
            canonical_page_url(self.RP), "https://www.airbnb.com/experiences/3015830"
        )

    def test_canonical_url_handles_a_direct_experience_url(self) -> None:
        self.assertEqual(
            canonical_page_url("https://www.airbnb.com.co/experiences/3015830?x=1"),
            "https://www.airbnb.com/experiences/3015830",
        )

    def test_canonical_url_passes_other_sites_through_untouched(self) -> None:
        other = "https://www.getyourguide.com/bayahibe-l1234/saona-t5678"
        self.assertEqual(canonical_page_url(other), other)

    def test_og_value_matches_exactly_and_ignores_width_variants(self) -> None:
        html = (
            '<meta property="og:image:width" content="4000"/>'
            '<meta property="og:image" content="https://cdn/x.jpg"/>'
            '<meta name="og:title" content="Saona trip"/>'
        )
        self.assertEqual(og_value(html, "og:image"), "https://cdn/x.jpg")
        self.assertEqual(og_value(html, "og:title"), "Saona trip")
        self.assertIsNone(og_value(html, "og:description"))

    def test_missing_og_image_is_a_clear_error(self) -> None:
        with patch("concierge.referral_preview.requests.get") as get:
            get.return_value = Mock(status_code=200, text="<html><head></head></html>")
            with self.assertRaises(PreviewError) as ctx:
                fetch_preview(self.RP)
        self.assertIn("no og:image", str(ctx.exception))

    def test_fetch_downscales_and_stores_a_jpeg(self) -> None:
        from PIL import Image

        big = BytesIO()
        Image.new("RGB", (4000, 2250), (10, 120, 130)).save(big, format="JPEG")
        page = Mock(status_code=200, text='<meta property="og:image" content="https://cdn/x.jpg">')
        image = Mock(status_code=200)
        image.iter_content = lambda n: iter([big.getvalue()])

        with patch("concierge.referral_preview.requests.get", side_effect=[page, image]):
            preview = fetch_preview(self.RP)

        out = Image.open(BytesIO(preview.content.read()))
        self.assertEqual(out.width, 1200)  # downscaled from 4000
        self.assertEqual(out.format, "JPEG")

    def test_oversized_image_is_refused(self) -> None:
        page = Mock(status_code=200, text='<meta property="og:image" content="https://cdn/x.jpg">')
        image = Mock(status_code=200)
        image.iter_content = lambda n: iter([b"x" * (9 * 1024 * 1024)])
        with patch("concierge.referral_preview.requests.get", side_effect=[page, image]):
            with self.assertRaises(PreviewError) as ctx:
                fetch_preview(self.RP)
        self.assertIn("larger than 8 MB", str(ctx.exception))


class ServiceAdminAutoPreviewTests(TestCase):
    """Saving a service with a referral link should pull its card image."""

    RP = "https://es-l.airbnb.com/rp/jpunales1?product=experience&listing_id=3015830"

    def setUp(self) -> None:
        from django.contrib.admin.sites import site as admin_site

        self.admin = admin_site._registry[Service]
        self.request = RequestFactory().post("/admin/concierge/service/add/")

    def _save(self, service, *, change=False, changed_data=()):
        form = Mock(changed_data=list(changed_data))
        with patch.object(type(self.admin), "message_user"):
            self.admin.save_model(self.request, service, form, change)

    def _fake_preview(self):
        from PIL import Image

        buf = BytesIO()
        Image.new("RGB", (1200, 675), (10, 120, 130)).save(buf, format="JPEG")
        return Mock(content=ContentFile(buf.getvalue()), title="Saona trip", source_url="https://cdn/x.jpg")

    def test_new_service_with_referral_link_gets_an_image(self) -> None:
        s = Service(slug="saona", name_en="Saona", name_es="Saona", referral_url=self.RP)
        with patch("concierge.admin.fetch_preview", return_value=self._fake_preview()) as fp:
            self._save(s)
        fp.assert_called_once_with(self.RP)
        s.refresh_from_db()
        self.assertTrue(s.image)

    def test_a_hand_uploaded_image_is_never_overwritten(self) -> None:
        s = Service(slug="saona", name_en="Saona", name_es="Saona", referral_url=self.RP)
        s.image.save("mine.jpg", ContentFile(b"not-a-real-jpeg"), save=False)
        with patch("concierge.admin.fetch_preview") as fp:
            self._save(s)
        fp.assert_not_called()

    def test_unrelated_edit_does_not_refetch(self) -> None:
        s = Service.objects.create(slug="saona", name_en="Saona", name_es="Saona", referral_url=self.RP)
        with patch("concierge.admin.fetch_preview") as fp:
            self._save(s, change=True, changed_data=["name_en"])
        fp.assert_not_called()

    def test_failed_fetch_still_saves_the_service(self) -> None:
        s = Service(slug="saona", name_en="Saona", name_es="Saona", referral_url=self.RP)
        with patch("concierge.admin.fetch_preview", side_effect=PreviewError("no og:image")):
            self._save(s)
        self.assertTrue(Service.objects.filter(slug="saona").exists())
        self.assertFalse(Service.objects.get(slug="saona").image)


class ReferralLinkShapeTests(TestCase):
    """A shared Airbnb *search* is a valid referral link that lands guests wrong."""

    SEARCH_SHARE = (
        "https://es-l.airbnb.com/rp/jpunales1?location=Bayah%C3%ADbe%2C+Rep%C3%BAblica+Dominicana"
        "&currentTab=experience_tab&federatedSearchId=8670d415&searchId=065b630a"
    )
    EXPERIENCE_SHARE = (
        "https://es-l.airbnb.com/rp/jpunales1?product=experience&listing_id=3015830"
    )

    def test_search_share_link_is_flagged(self) -> None:
        self.assertFalse(points_at_a_listing(self.SEARCH_SHARE))

    def test_experience_share_link_passes(self) -> None:
        self.assertTrue(points_at_a_listing(self.EXPERIENCE_SHARE))

    def test_plain_experience_url_passes(self) -> None:
        self.assertTrue(points_at_a_listing("https://www.airbnb.com.co/experiences/3015830"))

    def test_non_airbnb_links_are_left_alone(self) -> None:
        self.assertTrue(points_at_a_listing("https://www.getyourguide.com/x-t5678"))

    def test_admin_warns_and_skips_the_fetch_for_a_search_link(self) -> None:
        from django.contrib.admin.sites import site as admin_site

        model_admin = admin_site._registry[Service]
        request = RequestFactory().post("/admin/concierge/service/add/")
        s = Service(slug="saona", name_en="Saona", name_es="Saona", referral_url=self.SEARCH_SHARE)
        with patch("concierge.admin.fetch_preview") as fp, \
             patch.object(type(model_admin), "message_user") as msg:
            model_admin.save_model(request, s, Mock(changed_data=["referral_url"]), False)
        fp.assert_not_called()
        self.assertIn("shared Airbnb search", msg.call_args[0][1])


class ReferralUrlLengthTests(TestCase):
    """Airbnb share links are far longer than URLField's 200-character default.

    The default renders maxlength="200" on the admin input, so a browser
    truncates the paste silently — and listing_id sits near the end of an
    Airbnb link, so exactly the identifying part disappears.
    """

    FULL = (
        "https://es-l.airbnb.com/rp/jpunales1?location=Bayah%C3%ADbe%2C+Rep%C3%BAblica+Dominicana"
        "&currentTab=experience_tab&federatedSearchId=661ba9d2-6904-44a8-9a76-b401289116ce"
        "&searchId=6180823b-a5db-413e-b661-ced41fb17b15"
        "&sectionId=f5b6f4c3-70c3-402d-8540-0ade51f50643&p=recommendations"
        "&product=experience&listing_id=591291&s=67"
        "&unique_share_id=0fcf77ef-fda5-437d-8e96-454fe2618fe0"
    )

    def test_a_real_airbnb_share_link_exceeds_the_old_default(self) -> None:
        self.assertGreater(len(self.FULL), 200)

    def test_the_field_accepts_it_whole(self) -> None:
        self.assertGreaterEqual(Service._meta.get_field("referral_url").max_length, len(self.FULL))
        s = Service.objects.create(
            slug="taxi", name_en="Ride", name_es="Traslado", referral_url=self.FULL
        )
        s.refresh_from_db()
        self.assertEqual(s.referral_url, self.FULL)
        self.assertTrue(points_at_a_listing(s.referral_url))

    def test_truncating_it_loses_the_listing_id(self) -> None:
        # Precisely the failure seen in production: 200 chars, no listing_id.
        self.assertFalse(points_at_a_listing(self.FULL[:200]))


class StaleCardImageTests(TestCase):
    """Changing the referral link must not leave the previous listing's photo.

    Observed in production: two cards kept images fetched while the links were
    truncated, because save_model skipped the fetch whenever any image existed.
    """

    OLD = "https://es-l.airbnb.com/rp/jpunales1?product=experience&listing_id=111111"
    NEW = "https://es-l.airbnb.com/rp/jpunales1?product=experience&listing_id=222222"

    def setUp(self) -> None:
        from django.contrib.admin.sites import site as admin_site

        self.admin = admin_site._registry[Service]
        self.request = RequestFactory().post("/admin/concierge/service/add/")

    def _preview(self):
        from PIL import Image

        buf = BytesIO()
        Image.new("RGB", (1200, 675), (200, 90, 60)).save(buf, format="JPEG")
        return Mock(content=ContentFile(buf.getvalue()), title="New listing", source_url="https://cdn/n.jpg")

    def _save(self, service, changed_data):
        form = Mock(changed_data=list(changed_data))
        with patch.object(type(self.admin), "message_user"):
            with patch("concierge.admin.fetch_preview", return_value=self._preview()) as fp:
                self.admin.save_model(self.request, service, form, True)
        return fp

    def test_changing_the_link_refetches_over_a_previously_fetched_image(self) -> None:
        s = Service.objects.create(slug="saona", name_en="Saona", name_es="Saona", referral_url=self.OLD)
        s.image.save("saona-preview.jpg", ContentFile(b"old"), save=True)
        s.referral_url = self.NEW
        fp = self._save(s, ["referral_url"])
        fp.assert_called_once_with(self.NEW)

    def test_a_hand_uploaded_photo_survives_a_link_change(self) -> None:
        s = Service.objects.create(slug="saona", name_en="Saona", name_es="Saona", referral_url=self.OLD)
        s.image.save("my-own-photo.jpg", ContentFile(b"mine"), save=True)
        s.referral_url = self.NEW
        fp = self._save(s, ["referral_url"])
        fp.assert_not_called()
        s.refresh_from_db()
        self.assertIn("my-own-photo", s.image.name)


@override_settings(ANTHROPIC_API_KEY="sk-test")
class TranslationTests(TestCase):
    """One English name in, Spanish and French filled in automatically."""

    def _client_returning(self, payload):
        # `name` is a reserved Mock constructor kwarg — it names the mock
        # rather than setting the attribute, so assign it afterwards.
        block = Mock(type="tool_use", input=payload)
        block.name = "record_translations"
        client = Mock()
        client.messages.create.return_value = Mock(content=[block])
        return client

    def _patch(self, client):
        module = Mock()
        module.Anthropic.return_value = client
        return patch.dict("sys.modules", {"anthropic": module})

    def test_translate_returns_both_languages(self) -> None:
        client = self._client_returning({"es": "Excursión a Isla Saona", "fr": "Excursion à Saona"})
        with self._patch(client):
            out = translate("Saona Island excursion")
        self.assertEqual(out, {"es": "Excursión a Isla Saona", "fr": "Excursion à Saona"})
        kwargs = client.messages.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "claude-opus-5")
        # Forced tool use, so the model cannot answer in prose.
        self.assertEqual(kwargs["tool_choice"]["name"], "record_translations")

    def test_only_blank_languages_are_requested_and_filled(self) -> None:
        s = Service.objects.create(
            slug="saona", name_en="Saona Island excursion", name_es="Mi propia traducción"
        )
        client = self._client_returning({"fr": "Excursion à Saona"})
        with self._patch(client):
            filled = fill_missing_names(s)
        self.assertEqual(filled, ["fr"])
        # The host's Spanish wording is untouched.
        self.assertEqual(s.name_es, "Mi propia traducción")
        self.assertEqual(s.name_fr, "Excursion à Saona")
        self.assertEqual(set(client.messages.create.call_args.kwargs["tools"][0]["input_schema"]["properties"]), {"fr"})

    def test_nothing_to_do_makes_no_api_call(self) -> None:
        s = Service.objects.create(slug="s", name_en="Taxi", name_es="Taxi", name_fr="Taxi")
        client = self._client_returning({})
        with self._patch(client):
            self.assertEqual(fill_missing_names(s), [])
        client.messages.create.assert_not_called()

    def test_empty_translation_is_an_error_not_a_blank_name(self) -> None:
        client = self._client_returning({"es": "  ", "fr": "Excursion"})
        with self._patch(client):
            with self.assertRaises(TranslationError) as ctx:
                translate("Saona Island excursion")
        self.assertIn("es", str(ctx.exception))

    @override_settings(ANTHROPIC_API_KEY="")
    def test_without_an_api_key_it_raises_rather_than_guessing(self) -> None:
        with self.assertRaises(TranslationError):
            translate("Saona Island excursion")

    def test_api_failure_is_wrapped(self) -> None:
        client = Mock()
        client.messages.create.side_effect = RuntimeError("connection reset")
        with self._patch(client):
            with self.assertRaises(TranslationError) as ctx:
                translate("Saona")
        self.assertIn("connection reset", str(ctx.exception))


class MissingTranslationFallbackTests(TestCase):
    """A blank translation must never render as an empty title or a broken message."""

    def test_display_names_fall_back_to_english(self) -> None:
        s = Service(slug="saona", name_en="Saona Island excursion")
        self.assertEqual(s.display_name_es, "Saona Island excursion")
        self.assertEqual(s.display_name_fr, "Saona Island excursion")

    @override_settings(
        ALLOWED_HOSTS=["testserver"], WHATSAPP_BUSINESS_NUMBER="573222448409", ANTHROPIC_API_KEY=""
    )
    def test_landing_page_uses_english_when_a_translation_is_missing(self) -> None:
        Service.objects.create(slug="saona", name_en="Saona Island excursion")
        html = self.client.get("/").content.decode()
        # Every language span shows the English name — none render empty.
        self.assertGreaterEqual(html.count("Saona Island excursion"), 3)
        self.assertNotIn('<span class="es"></span>', html)
        self.assertNotIn('<span class="fr"></span>', html)
        # And the pre-filled Spanish message is not "info sobre ."
        self.assertNotIn("sobre%20.", html)


@override_settings(
    ALLOWED_HOSTS=["testserver"],
    WHATSAPP_BUSINESS_NUMBER="573222448409",
    HOST_APARTMENT_LABEL="Reef",
    ANTHROPIC_API_KEY="",
)
class ServiceChannelTests(TestCase):
    """Each service picks its own destination: WhatsApp, Airbnb, or Viator."""

    WA = "https://wa.me/573222448409?text="
    AIRBNB = "https://es-l.airbnb.com/rp/jpunales1?product=experience&listing_id=591291"
    VIATOR = "https://www.viator.com/tours/Bayahibe/Saona/d123-abc?pid=P00012345"

    def setUp(self) -> None:
        self.saona = Service.objects.create(
            slug="saona", name_en="Saona", channel=Service.Channel.AIRBNB, referral_url=self.AIRBNB
        )
        self.catalina = Service.objects.create(
            slug="catalina", name_en="Catalina", channel=Service.Channel.VIATOR, referral_url=self.VIATOR
        )
        self.taxi = Service.objects.create(
            slug="taxi", name_en="Airport taxi", channel=Service.Channel.WHATSAPP
        )

    def _render(self, mode=SiteSettings.CtaMode.REFERRAL) -> str:
        site = SiteSettings.load()
        site.cta_mode = mode
        site.save()
        # Unescape: Django renders & as &amp; inside href attributes.
        return html_lib.unescape(self.client.get("/").content.decode())

    def test_each_channel_routes_to_its_own_destination(self) -> None:
        html = self._render()
        self.assertIn(self.AIRBNB, html)
        self.assertIn(self.VIATOR, html)
        self.assertIn(self.WA, html)  # the taxi

    def test_whatsapp_channel_ignores_a_referral_url_that_is_set(self) -> None:
        # Switching a service back to WhatsApp must take effect even though the
        # link is still on record — the host may be pausing a programme.
        parked = "https://www.viator.com/tours/parked-d999"
        self.taxi.referral_url = parked
        self.taxi.channel = Service.Channel.WHATSAPP
        self.taxi.save()
        self.assertFalse(self.taxi.uses_referral_link)
        self.assertNotIn(parked, self._render())

    def test_referral_channel_without_a_link_falls_back_to_whatsapp(self) -> None:
        self.saona.referral_url = ""
        self.saona.save()
        self.assertFalse(self.saona.uses_referral_link)
        html = self._render()
        self.assertNotIn("airbnb.com", html)
        self.assertIn(self.WA, html)

    def test_site_wide_kill_switch_overrides_every_channel(self) -> None:
        html = self._render(SiteSettings.CtaMode.WHATSAPP)
        self.assertNotIn("airbnb.com", html)
        self.assertNotIn("viator.com", html)
        self.assertNotIn("commission", html)  # nothing to disclose

    def test_channel_label_names_the_provider(self) -> None:
        self.assertEqual(self.saona.channel_label, "Airbnb")
        self.assertEqual(self.catalina.channel_label, "Viator")
        self.assertEqual(self.taxi.channel_label, "")

    def test_disclosure_shows_when_any_channel_is_a_referral(self) -> None:
        self.assertIn("commission", self._render())


class ViatorAffiliateUrlTests(TestCase):
    """Any viator.com URL becomes a referral link from one configured PID."""

    PID = "P00012345"
    PLAIN = "https://www.viator.com/tours/Bayahibe/Saona/d5021-123456P7"

    def _params(self, url):
        return dict(parse_qsl(urlparse(url).query))

    def test_parameters_are_appended_to_a_plain_product_url(self) -> None:
        out = viator_url(self.PLAIN, pid=self.PID)
        self.assertEqual(
            self._params(out), {"pid": self.PID, "mcid": "42383", "medium": "link"}
        )
        self.assertTrue(out.startswith(self.PLAIN + "?"))

    def test_existing_query_parameters_are_preserved(self) -> None:
        out = viator_url(self.PLAIN + "?m=1&sortType=rating", pid=self.PID)
        params = self._params(out)
        self.assertEqual(params["m"], "1")
        self.assertEqual(params["sortType"], "rating")
        self.assertEqual(params["pid"], self.PID)

    def test_existing_tracking_is_never_overwritten(self) -> None:
        # Viator will not pay out if pid or mcid is modified — a link the host
        # pasted with its own tracking must survive untouched.
        already = self.PLAIN + "?pid=P00099999&mcid=11111&medium=banner"
        self.assertEqual(self._params(viator_url(already, pid=self.PID)), self._params(already))

    def test_campaign_is_added_only_when_configured(self) -> None:
        self.assertNotIn("campaign", self._params(viator_url(self.PLAIN, pid=self.PID)))
        out = viator_url(self.PLAIN, pid=self.PID, campaign="apto-reef-qr")
        self.assertEqual(self._params(out)["campaign"], "apto-reef-qr")

    def test_custom_mcid_overrides_the_default(self) -> None:
        self.assertEqual(self._params(viator_url(self.PLAIN, pid=self.PID, mcid="99999"))["mcid"], "99999")

    def test_non_viator_and_unconfigured_urls_pass_through(self) -> None:
        airbnb = "https://es-l.airbnb.com/rp/x?listing_id=1"
        self.assertEqual(viator_url(airbnb, pid=self.PID), airbnb)      # not viator
        self.assertEqual(viator_url(self.PLAIN, pid=""), self.PLAIN)    # no PID set
        self.assertEqual(viator_url("", pid=self.PID), "")

    def test_lookalike_domains_are_not_treated_as_viator(self) -> None:
        self.assertFalse(is_viator("https://viator.com.evil.example/tours/x"))


@override_settings(
    ALLOWED_HOSTS=["testserver"], WHATSAPP_BUSINESS_NUMBER="573222448409", ANTHROPIC_API_KEY=""
)
class ViatorAffiliateRenderingTests(TestCase):
    PLAIN = "https://www.viator.com/tours/Bayahibe/Saona/d5021-123456P7"

    def setUp(self) -> None:
        Service.objects.create(
            slug="saona", name_en="Saona", channel=Service.Channel.VIATOR, referral_url=self.PLAIN
        )
        self.site = SiteSettings.load()
        self.site.cta_mode = SiteSettings.CtaMode.REFERRAL

    def _html(self) -> str:
        self.site.save()
        return html_lib.unescape(self.client.get("/").content.decode())

    def test_the_rendered_link_carries_the_affiliate_id(self) -> None:
        self.site.viator_partner_id = "P00012345"
        self.site.viator_campaign = "apto-reef-qr"
        html = self._html()
        self.assertIn("pid=P00012345", html)
        self.assertIn("mcid=42383", html)
        self.assertIn("campaign=apto-reef-qr", html)

    def test_without_a_pid_the_plain_link_still_renders(self) -> None:
        html = self._html()
        self.assertIn(self.PLAIN, html)
        self.assertNotIn("pid=", html)

    def test_campaign_rejects_characters_that_break_tracking(self) -> None:
        self.site.viator_campaign = "apto reef!"
        with self.assertRaises(ValidationError):
            self.site.full_clean()
