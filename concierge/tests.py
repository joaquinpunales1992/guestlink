"""Smoke tests for the relay routing logic.

Runs with WHATSAPP_DRY_RUN=1 (default) so no network calls are made — outbound
messages are still persisted as Message rows, which is what we assert against.
"""

from __future__ import annotations

import html as html_lib
import importlib
from io import BytesIO
from urllib.parse import parse_qsl, quote, urlparse
from unittest.mock import Mock, patch

import qrcode
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.test import RequestFactory, TestCase, override_settings

from concierge import qr
from concierge.affiliate import is_viator, viator_url
from concierge.classifier import Classification
from concierge.models import (
    Commission,
    Guest,
    Location,
    LocationEvent,
    Message,
    Provider,
    Service,
    SiteSettings,
    Ticket,
)
from concierge.referral_preview import (
    PreviewError,
    canonical_page_url,
    fetch_preview,
    og_value,
    points_at_a_listing,
)
from concierge.relay import CODE_RE, _strip_code, detect_location, handle_inbound, normalize_phone
from concierge.translate import TranslationError, fill_missing_names, translate
from concierge.whatsapp import WhatsAppError


def destination_of(client, service_slug, **params):
    """Follow one card's counting redirect and return where it points."""
    resp = client.get(f"/go/{service_slug}/", params)
    assert resp.status_code == 302, f"expected a redirect, got {resp.status_code}"
    return resp["Location"]


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
        self.assertNotIn("commission", html)
        self.assertTrue(destination_of(self.client, "saona").startswith(self.WA))

    def test_referral_mode_uses_the_link_and_falls_back_per_service(self) -> None:
        html = self._render(SiteSettings.CtaMode.REFERRAL)
        self.assertIn("Book online", html)
        self.assertEqual(destination_of(self.client, "saona"), self.REF)
        # The taxi has no referral link, so its card must still reach WhatsApp
        # rather than rendering a dead end.
        self.assertTrue(destination_of(self.client, "taxi").startswith(self.WA))

    def test_both_mode_shows_whatsapp_under_the_referral_card(self) -> None:
        html = self._render(SiteSettings.CtaMode.BOTH)
        self.assertIn("Or ask us on WhatsApp", html)
        self.assertEqual(destination_of(self.client, "saona"), self.REF)
        # The secondary link forces WhatsApp even though the card is a referral.
        self.assertTrue(destination_of(self.client, "saona", ch="wa").startswith(self.WA))

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


@override_settings(TRANSLATE_SERVICE_NAMES=False)
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


@override_settings(TRANSLATE_SERVICE_NAMES=False)
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


@override_settings(TRANSLATE_SERVICE_NAMES=False)
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


class TranslationTests(TestCase):
    """Machine translation of service names, over MyMemory's free API."""

    def _reply(self, text, status=200):
        return Mock(status_code=status, json=lambda: {"responseData": {"translatedText": text}})

    def test_each_target_language_is_requested(self) -> None:
        with patch("concierge.translate.requests.get") as get:
            get.side_effect = [self._reply("Excursión a la isla Saona"), self._reply("Excursion sur l'île")]
            out = translate("Saona Island excursion")
        self.assertEqual(out["es"], "Excursión a la isla Saona")
        self.assertEqual(out["fr"], "Excursion sur l'île")
        pairs = [call.kwargs["params"]["langpair"] for call in get.call_args_list]
        self.assertEqual(pairs, ["en|es", "en|fr"])

    def test_only_blank_languages_are_requested_and_filled(self) -> None:
        s = Service.objects.create(
            slug="saona", name_en="Saona Island excursion", name_es="Mi propia traducción"
        )
        with patch("concierge.translate.requests.get") as get:
            get.return_value = self._reply("Excursion sur l'île")
            filled = fill_missing_names(s)
        self.assertEqual(filled, ["fr"])
        self.assertEqual(s.name_es, "Mi propia traducción")  # the host's wording wins
        self.assertEqual(get.call_count, 1)

    def test_nothing_to_do_makes_no_request(self) -> None:
        s = Service.objects.create(slug="s", name_en="Taxi", name_es="Taxi", name_fr="Taxi")
        with patch("concierge.translate.requests.get") as get:
            self.assertEqual(fill_missing_names(s), [])
        get.assert_not_called()

    def test_a_lower_cased_result_keeps_the_title_capitalised(self) -> None:
        with patch("concierge.translate.requests.get") as get:
            get.return_value = self._reply("excursión a la isla saona")
            self.assertEqual(translate("Saona Island excursion", ("es",))["es"][0], "E")

    def test_html_entities_are_decoded(self) -> None:
        with patch("concierge.translate.requests.get") as get:
            get.return_value = self._reply("Alquiler de coches &amp; motos")
            self.assertEqual(translate("Car rental", ("es",))["es"], "Alquiler de coches & motos")

    def test_an_exhausted_quota_is_reported_as_such(self) -> None:
        # MyMemory returns this inside the text with HTTP 200, not as an error.
        with patch("concierge.translate.requests.get") as get:
            get.return_value = self._reply("MYMEMORY WARNING: YOU USED ALL AVAILABLE FREE TRANSLATIONS")
            with self.assertRaises(TranslationError) as ctx:
                translate("Saona", ("es",))
        self.assertIn("quota", str(ctx.exception))

    def test_an_empty_translation_is_an_error_not_a_blank_name(self) -> None:
        with patch("concierge.translate.requests.get") as get:
            get.return_value = self._reply("")
            with self.assertRaises(TranslationError):
                translate("Saona", ("es",))

    def test_network_failure_is_wrapped(self) -> None:
        import requests as _requests

        with patch("concierge.translate.requests.get", side_effect=_requests.ConnectionError("down")):
            with self.assertRaises(TranslationError) as ctx:
                translate("Saona", ("es",))
        self.assertIn("could not reach", str(ctx.exception))

    def test_http_error_is_wrapped(self) -> None:
        with patch("concierge.translate.requests.get") as get:
            get.return_value = self._reply("x", status=503)
            with self.assertRaises(TranslationError) as ctx:
                translate("Saona", ("es",))
        self.assertIn("503", str(ctx.exception))

    @override_settings(MYMEMORY_EMAIL="host@example.com")
    def test_the_email_is_sent_when_configured_to_raise_the_quota(self) -> None:
        with patch("concierge.translate.requests.get") as get:
            get.return_value = self._reply("Taxi")
            translate("Taxi", ("es",))
        self.assertEqual(get.call_args.kwargs["params"]["de"], "host@example.com")

    def test_no_api_key_is_required(self) -> None:
        # The whole point of the swap: translation must not depend on an LLM key.
        with override_settings(ANTHROPIC_API_KEY=""):
            with patch("concierge.translate.requests.get") as get:
                get.return_value = self._reply("Taxi")
                self.assertEqual(translate("Taxi", ("es",)), {"es": "Taxi"})


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
        self._render()
        self.assertEqual(destination_of(self.client, "saona"), self.AIRBNB)
        self.assertTrue(destination_of(self.client, "catalina").startswith(self.VIATOR))
        self.assertTrue(destination_of(self.client, "taxi").startswith(self.WA))

    def test_whatsapp_channel_ignores_a_referral_url_that_is_set(self) -> None:
        # Switching a service back to WhatsApp must take effect even though the
        # link is still on record — the host may be pausing a programme.
        self.taxi.referral_url = "https://www.viator.com/tours/parked-d999"
        self.taxi.channel = Service.Channel.WHATSAPP
        self.taxi.save()
        self.assertFalse(self.taxi.uses_referral_link)
        self._render()
        self.assertTrue(destination_of(self.client, "taxi").startswith(self.WA))

    def test_referral_channel_without_a_link_falls_back_to_whatsapp(self) -> None:
        self.saona.referral_url = ""
        self.saona.save()
        self.assertFalse(self.saona.uses_referral_link)
        self._render()
        self.assertTrue(destination_of(self.client, "saona").startswith(self.WA))

    def test_site_wide_kill_switch_overrides_every_channel(self) -> None:
        html = self._render(SiteSettings.CtaMode.WHATSAPP)
        self.assertNotIn("commission", html)  # nothing to disclose
        for slug in ("saona", "catalina", "taxi"):
            self.assertTrue(destination_of(self.client, slug).startswith(self.WA), slug)

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

    def test_output_matches_viator_own_link_builder(self) -> None:
        # Pasting a product URL into Viator's affiliate link tool returns
        # exactly this shape; reproducing it is the whole job.
        out = viator_url(self.PLAIN, pid=self.PID)
        self.assertEqual(self._params(out), {"pid": self.PID, "mcid": "42383", "medium": "link"})
        self.assertTrue(out.startswith(self.PLAIN + "?"))

    def test_a_campaign_is_added_only_when_configured(self) -> None:
        self.assertNotIn("campaign=", viator_url(self.PLAIN, pid=self.PID))
        self.assertIn("campaign=la-bahia", viator_url(self.PLAIN, pid=self.PID, campaign="la-bahia"))

    def test_a_configured_mcid_overrides_the_default(self) -> None:
        self.assertEqual(self._params(viator_url(self.PLAIN, pid=self.PID, mcid="99999"))["mcid"], "99999")

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
        self._html()
        dest = destination_of(self.client, "saona")
        self.assertIn("pid=P00012345", dest)
        self.assertIn("campaign=apto-reef-qr", dest)

    def test_without_a_pid_the_plain_link_still_renders(self) -> None:
        self._html()
        self.assertEqual(destination_of(self.client, "saona"), self.PLAIN)

    def test_campaign_rejects_characters_that_break_tracking(self) -> None:
        self.site.viator_campaign = "apto reef!"
        with self.assertRaises(ValidationError):
            self.site.full_clean()


@override_settings(
    ALLOWED_HOSTS=["testserver"],
    WHATSAPP_BUSINESS_NUMBER="573222448409",
    HOST_APARTMENT_LABEL="Reef",
    ANTHROPIC_API_KEY="",
)
class LocationTests(TestCase):
    """One QR per venue: its own page, its own menu, its own attribution."""

    VIATOR = "https://www.viator.com/tours/Bayahibe/Saona/d5021-123P4"

    def setUp(self) -> None:
        self.saona = Service.objects.create(
            slug="saona", name_en="Saona trip", channel=Service.Channel.VIATOR, referral_url=self.VIATOR
        )
        self.diving = Service.objects.create(slug="diving", name_en="Diving")
        self.taxi = Service.objects.create(slug="taxi", name_en="Airport taxi")

        # This slug already exists: migration 0013 creates it from the old
        # APARTMENT_SLUG setting so the printed cards keep working.
        self.apto, _ = Location.objects.update_or_create(
            slug="the-reef-401",
            defaults={"name": "Apto Reef", "kind": Location.Kind.APARTMENT, "active": True},
        )
        self.dive_shop = Location.objects.create(
            name="Scuba Caribe", slug="scuba-caribe", kind=Location.Kind.DIVE_SHOP
        )
        # A dive shop should not advertise a rival dive trip.
        self.dive_shop.services.set([self.saona, self.taxi])

        site = SiteSettings.load()
        site.cta_mode = SiteSettings.CtaMode.REFERRAL
        site.viator_partner_id = "P00012345"
        site.save()

    def test_the_printed_apartment_slug_survived_the_migration(self) -> None:
        # Migration 0013 must create this row; without it every QR card already
        # hanging in the apartment 404s.
        self.assertTrue(Location.objects.filter(slug="the-reef-401").exists())

    def test_each_location_has_its_own_page(self) -> None:
        self.assertEqual(self.client.get("/the-reef-401/").status_code, 200)
        self.assertEqual(self.client.get("/scuba-caribe/").status_code, 200)

    def test_printed_qr_payloads_without_a_trailing_slash_are_served_directly(self) -> None:
        # A 301 here would cost the guest an extra round trip on mobile data.
        self.assertEqual(self.client.get("/the-reef-401").status_code, 200)

    def test_an_unknown_or_inactive_slug_is_a_404(self) -> None:
        self.assertEqual(self.client.get("/no-such-venue/").status_code, 404)
        Location.objects.filter(slug="scuba-caribe").update(active=False)
        self.assertEqual(self.client.get("/scuba-caribe/").status_code, 404)

    def test_a_location_shows_only_its_chosen_services(self) -> None:
        html = self.client.get("/scuba-caribe/").content.decode()
        self.assertIn("Saona trip", html)
        self.assertIn("Airport taxi", html)
        self.assertNotIn("Diving", html)

    def test_no_selection_means_every_active_service(self) -> None:
        html = self.client.get("/the-reef-401/").content.decode()
        for name in ("Saona trip", "Diving", "Airport taxi"):
            self.assertIn(name, html)

    def test_fixed_routes_still_win_over_location_slugs(self) -> None:
        Location.objects.create(name="Impostor", slug="privacy")
        self.assertIn("Privacy Policy", self.client.get("/privacy/").content.decode())

    # ---- attribution -------------------------------------------------------

    def test_every_venue_gets_a_campaign_so_bookings_are_attributable(self) -> None:
        # Revenue share is paid on bookings traced back to a venue, so a venue
        # without its own campaign would be invisible in Viator's reporting.
        dest = destination_of(self.client, "saona", at="scuba-caribe")
        self.assertIn("pid=P00012345", dest)
        self.assertIn("campaign=scuba-caribe", dest)

    def test_an_explicit_campaign_code_overrides_the_slug(self) -> None:
        Location.objects.filter(slug="scuba-caribe").update(campaign_code="dive-partner-a")
        self.assertIn("campaign=dive-partner-a", destination_of(self.client, "saona", at="scuba-caribe"))

    def test_two_venues_never_share_a_campaign(self) -> None:
        # Two venues on one code would mean paying the wrong partner.
        a = destination_of(self.client, "saona", at="scuba-caribe")
        b = destination_of(self.client, "saona", at="the-reef-401")
        self.assertIn("campaign=scuba-caribe", a)
        self.assertIn("campaign=the-reef-401", b)
        self.assertNotEqual(a, b)

    def test_an_explicit_code_overrides_the_slug(self) -> None:
        Location.objects.filter(slug="scuba-caribe").update(campaign_code="dive-shop")
        self.assertIn("campaign=dive-shop", destination_of(self.client, "saona", at="scuba-caribe"))

    def test_whatsapp_message_names_the_venue(self) -> None:
        at_shop = destination_of(self.client, "taxi", at="scuba-caribe")
        self.assertIn(quote("I'm at Scuba Caribe"), at_shop)
        # "Staying at" reads right for an apartment and wrong for a shop.
        at_apto = destination_of(self.client, "taxi", at="the-reef-401")
        self.assertIn(quote("I'm staying at Apto Reef"), at_apto)

    def test_whatsapp_message_follows_the_language(self) -> None:
        self.assertIn(quote("Quisiera info sobre"), destination_of(self.client, "taxi", at="scuba-caribe", lang="es"))
        self.assertIn(quote("Je voudrais"), destination_of(self.client, "taxi", at="scuba-caribe", lang="fr"))

    # ---- counting ----------------------------------------------------------

    def test_a_page_view_counts_as_a_scan_for_that_location(self) -> None:
        self.client.get("/scuba-caribe/")
        event = LocationEvent.objects.get()
        self.assertEqual(event.kind, LocationEvent.Kind.SCAN)
        self.assertEqual(event.location, self.dive_shop)

    def test_a_click_is_counted_against_the_location_and_service(self) -> None:
        destination_of(self.client, "saona", at="scuba-caribe")
        event = LocationEvent.objects.get(kind=LocationEvent.Kind.CLICK)
        self.assertEqual(event.location, self.dive_shop)
        self.assertEqual(event.service, self.saona)
        self.assertEqual(event.channel, "viator")

    def test_a_whatsapp_click_records_the_whatsapp_channel(self) -> None:
        destination_of(self.client, "saona", at="scuba-caribe", ch="wa")
        self.assertEqual(LocationEvent.objects.get(kind=LocationEvent.Kind.CLICK).channel, "whatsapp")

    def test_clicks_without_a_location_are_still_counted(self) -> None:
        destination_of(self.client, "taxi")
        self.assertIsNone(LocationEvent.objects.get(kind=LocationEvent.Kind.CLICK).location)

    def test_events_hold_nothing_that_identifies_a_visitor(self) -> None:
        self.client.get("/scuba-caribe/", HTTP_USER_AGENT="Mozilla/5.0", REMOTE_ADDR="203.0.113.9")
        fields = {f.name for f in LocationEvent._meta.get_fields()}
        self.assertEqual(fields & {"ip", "ip_address", "user_agent", "session_key"}, set())

    def test_the_page_links_through_the_counter_carrying_the_location(self) -> None:
        html = self.client.get("/scuba-caribe/").content.decode()
        self.assertIn("/go/saona/?at=scuba-caribe", html.replace("&amp;", "&"))


class PrivacyPolicyAccuracyTests(TestCase):
    """The policy has to describe what the site actually does."""

    @override_settings(ALLOWED_HOSTS=["testserver"])
    def test_it_discloses_the_scan_and_click_counting(self) -> None:
        html = self.client.get("/privacy/").content.decode()
        self.assertIn("count", html.lower())
        self.assertIn("QR code", html)
        # The old copy claimed no automatic collection at all — now untrue.
        self.assertNotIn("Nothing is collected from you automatically", html)
        # And it must still be honest about what is NOT stored.
        self.assertIn("no IP address", html)


class LocationQrTests(TestCase):
    """A printable QR per venue, generated from its landing URL."""

    def setUp(self) -> None:
        self.loc = Location.objects.create(name="Restaurante La Bahía", slug="la-bahia")
        User = get_user_model()
        self.staff = User.objects.create_user("staffer", password="pw", is_staff=True)

    def test_payload_is_the_absolute_landing_url_without_a_trailing_slash(self) -> None:
        # A trailing slash would encode a longer string (denser code) anddiffer from
        # the payload already printed on the apartment's cards.
        request = RequestFactory().get("/admin/")
        self.assertEqual(qr.payload(request, self.loc), "http://testserver/la-bahia")

    def test_svg_renders_and_is_scalable(self) -> None:
        body = qr.svg_bytes("https://bookyourtickets.online/la-bahia")
        self.assertIn(b"<svg", body)
        self.assertIn(b"path", body)  # vector, not an embedded bitmap

    def test_png_renders_with_a_valid_header(self) -> None:
        self.assertTrue(qr.png_bytes("https://bookyourtickets.online/la-bahia").startswith(b"\x89PNG\r\n\x1a\n"))

    def test_quiet_zone_and_error_correction_are_print_safe(self) -> None:
        # Scanners need the 4-module quiet zone; Q survives a scuffed card.
        self.assertGreaterEqual(qr.BORDER, 4)
        self.assertEqual(qr.ERROR_CORRECTION, qrcode.constants.ERROR_CORRECT_Q)

    @override_settings(ALLOWED_HOSTS=["testserver"])
    def test_the_view_serves_both_formats_to_staff(self) -> None:
        self.client.force_login(self.staff)
        svg = self.client.get("/qr/la-bahia.svg")
        png = self.client.get("/qr/la-bahia.png")
        self.assertEqual(svg["Content-Type"], "image/svg+xml")
        self.assertEqual(png["Content-Type"], "image/png")
        self.assertIn("inline", svg["Content-Disposition"])

    @override_settings(ALLOWED_HOSTS=["testserver"])
    def test_download_flag_sets_a_filename(self) -> None:
        self.client.force_login(self.staff)
        resp = self.client.get("/qr/la-bahia.svg?download=1")
        self.assertIn('attachment; filename="qr-la-bahia.svg"', resp["Content-Disposition"])

    @override_settings(ALLOWED_HOSTS=["testserver"])
    def test_it_is_staff_only(self) -> None:
        self.assertEqual(self.client.get("/qr/la-bahia.svg").status_code, 302)  # to admin login

    @override_settings(ALLOWED_HOSTS=["testserver"])
    def test_unknown_venue_is_a_404(self) -> None:
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get("/qr/nope.svg").status_code, 404)

    @override_settings(ALLOWED_HOSTS=["testserver"])
    def test_the_qr_route_is_not_shadowed_by_a_location_slug(self) -> None:
        Location.objects.create(name="Impostor", slug="qr")
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get("/qr/la-bahia.svg")["Content-Type"], "image/svg+xml")


@override_settings(ALLOWED_HOSTS=["testserver"], WHATSAPP_BUSINESS_NUMBER="573222448409")
class LocationBrandingTests(TestCase):
    """A location is a business, so it carries its own page copy."""

    def setUp(self) -> None:
        Service.objects.create(slug="taxi", name_en="Airport taxi")
        self.site = SiteSettings.load()
        self.site.tab_title = "Default tab"
        self.site.headline_en = "Default headline"
        self.site.tagline_en = "Default tagline"
        self.site.footer_text = "Default footer"
        self.site.save()

    def test_a_venue_overrides_the_site_copy(self) -> None:
        Location.objects.create(
            name="Restaurante La Bahía", slug="la-bahia",
            tab_title="La Bahía · Bayahibe",
            headline_en="Welcome to La Bahía",
            tagline_en="Ask us about anything nearby.",
            footer_text="Restaurante La Bahía",
        )
        html = self.client.get("/la-bahia/").content.decode()
        self.assertIn("<title>La Bahía · Bayahibe</title>", html)
        self.assertIn("Welcome to La Bahía", html)
        self.assertIn("Ask us about anything nearby.", html)
        self.assertNotIn("Default headline", html)

    def test_blank_fields_fall_back_to_the_site_default(self) -> None:
        # A venue added in a hurry still renders a complete page.
        Location.objects.create(name="Scuba Caribe", slug="scuba-caribe", headline_en="Dive with us")
        html = self.client.get("/scuba-caribe/").content.decode()
        self.assertIn("Dive with us", html)          # its own
        self.assertIn("<title>Default tab</title>", html)  # inherited
        self.assertIn("Default footer", html)

    def test_the_bare_domain_uses_the_site_defaults(self) -> None:
        html = self.client.get("/").content.decode()
        self.assertIn("<title>Default tab</title>", html)
        self.assertIn("Default headline", html)

    def test_two_venues_render_different_identities(self) -> None:
        Location.objects.create(name="A", slug="venue-a", headline_en="I am A")
        Location.objects.create(name="B", slug="venue-b", headline_en="I am B")
        self.assertIn("I am A", self.client.get("/venue-a/").content.decode())
        self.assertIn("I am B", self.client.get("/venue-b/").content.decode())

    def test_the_apartment_kept_its_original_copy_through_the_move(self) -> None:
        # Migration 0015 stamps the old site-wide copy onto the apartment so its
        # page does not silently change when the defaults later do.
        apto = Location.objects.filter(slug="the-reef-401").first()
        self.assertIsNotNone(apto)
        self.assertTrue(apto.headline_en, "the apartment should carry its own headline")


class CommissionTests(TestCase):
    """Two sides of one event: what you collect, and what you owe the venue."""

    def setUp(self) -> None:
        self.venue = Location.objects.create(
            name="Restaurante La Bahía", slug="la-bahia", commission_share_pct=Decimal("20")
        )
        self.service = Service.objects.create(slug="saona", name_en="Saona")

    def _commission(self, **kw):
        return Commission.objects.create(
            **{
                "location": self.venue,
                "service": self.service,
                "channel": Service.Channel.VIATOR,
                "gross_usd": Decimal("50.00"),
                **kw,
            }
        )

    def test_the_venue_share_is_taken_from_the_location(self) -> None:
        c = self._commission()
        self.assertEqual(c.venue_share_pct, Decimal("20"))
        self.assertEqual(c.venue_share_usd, Decimal("10.00"))
        self.assertEqual(c.net_usd, Decimal("40.00"))
        self.assertEqual(c.payout_status, Commission.Payout.OWED)

    def test_the_share_is_a_snapshot_not_a_live_lookup(self) -> None:
        # Renegotiating a venue's cut must not rewrite what is already owed.
        c = self._commission()
        Location.objects.filter(pk=self.venue.pk).update(commission_share_pct=Decimal("50"))
        c.refresh_from_db()
        self.assertEqual(c.venue_share_usd, Decimal("10.00"))

    def test_rounding_is_to_the_cent(self) -> None:
        c = self._commission(gross_usd=Decimal("33.33"), venue_share_pct=Decimal("33.33"))
        self.assertEqual(c.venue_share_usd, Decimal("11.11"))

    def test_no_share_means_nothing_to_pay(self) -> None:
        free = Location.objects.create(name="Own apartment", slug="own", commission_share_pct=0)
        c = self._commission(location=free)
        self.assertEqual(c.venue_share_usd, Decimal("0.00"))
        self.assertEqual(c.payout_status, Commission.Payout.NONE)
        self.assertEqual(c.net_usd, c.gross_usd)

    def test_editing_the_percentage_recalculates_the_amount(self) -> None:
        c = self._commission()
        c.venue_share_pct = Decimal("10")
        c.save()
        self.assertEqual(c.venue_share_usd, Decimal("5.00"))

    def test_a_commission_can_have_no_venue(self) -> None:
        c = self._commission(location=None)
        self.assertEqual(c.venue_share_usd, Decimal("0.00"))
        self.assertEqual(c.payout_status, Commission.Payout.NONE)


@override_settings(WHATSAPP_DRY_RUN=True, ANTHROPIC_API_KEY="")
class WhatsAppCommissionTests(TestCase):
    """WhatsApp is the channel nobody reports for you — it must be claimable."""

    def setUp(self) -> None:
        self.provider = Provider.objects.create(name="María (Saona)", phone="+18091111111")
        self.service = Service.objects.create(
            slug="saona", name_en="Saona Island excursion", name_es="Excursión Isla Saona",
            keywords="saona", default_provider=self.provider, expected_commission_usd=Decimal("15.00"),
        )
        self.venue = Location.objects.create(
            name="Restaurante La Bahía", slug="la-bahia", commission_share_pct=Decimal("25")
        )

    def test_the_ticket_records_which_venue_sent_the_guest(self) -> None:
        # The QR pre-fills "I'm at <venue>", so the name arrives in the message.
        outcome = handle_inbound(
            from_phone="+4915112345678",
            body="Hi! I'm at Restaurante La Bahía. I'd like info about Saona.",
        )
        self.assertEqual(outcome.ticket.location, self.venue)

    def test_a_guest_typing_their_own_message_leaves_the_venue_unknown(self) -> None:
        outcome = handle_inbound(from_phone="+4915112345678", body="hola quiero ir a saona")
        self.assertIsNone(outcome.ticket.location)

    def test_the_longest_matching_venue_name_wins(self) -> None:
        Location.objects.create(name="Bahía", slug="bahia")
        self.assertEqual(
            detect_location("I'm at Restaurante La Bahía today"), self.venue
        )

    def test_recording_a_claim_from_a_ticket_carries_venue_and_provider(self) -> None:
        from django.contrib.admin.sites import site as admin_site

        outcome = handle_inbound(
            from_phone="+4915112345678",
            body="Hi! I'm at Restaurante La Bahía. I'd like info about Saona.",
        )
        model_admin = admin_site._registry[Ticket]
        request = RequestFactory().post("/admin/")
        with patch.object(type(model_admin), "message_user"):
            model_admin.record_commission(request, Ticket.objects.filter(pk=outcome.ticket.pk))

        c = Commission.objects.get()
        self.assertEqual(c.channel, Service.Channel.WHATSAPP)
        self.assertEqual(c.provider, self.provider)       # who to claim from
        self.assertEqual(c.location, self.venue)          # who to pay
        self.assertEqual(c.gross_usd, Decimal("15.00"))
        self.assertEqual(c.venue_share_usd, Decimal("3.75"))
        self.assertEqual(c.reference, outcome.ticket.short_code)
        self.assertEqual(c.status, Commission.Status.PENDING)

    def test_recording_twice_does_not_double_count(self) -> None:
        from django.contrib.admin.sites import site as admin_site

        outcome = handle_inbound(from_phone="+4915112345678", body="Saona please")
        model_admin = admin_site._registry[Ticket]
        request = RequestFactory().post("/admin/")
        qs = Ticket.objects.filter(pk=outcome.ticket.pk)
        with patch.object(type(model_admin), "message_user"):
            model_admin.record_commission(request, qs)
            model_admin.record_commission(request, qs)
        self.assertEqual(Commission.objects.count(), 1)


PLAIN_STATIC = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


@override_settings(STORAGES=PLAIN_STATIC)
class CommissionSummaryTests(TestCase):
    """The admin has to answer: what do I collect, and what do I owe?

    Rendering the real admin page rather than calling the aggregate directly:
    the totals are only useful if they survive the template.
    """

    def setUp(self) -> None:
        self.venue = Location.objects.create(
            name="La Bahía", slug="la-bahia", commission_share_pct=Decimal("20")
        )
        User = get_user_model()
        self.staff = User.objects.create_superuser("boss", "b@example.com", "pw")

    def _c(self, gross, status=Commission.Status.PENDING, payout=None):
        c = Commission.objects.create(
            location=self.venue, channel=Service.Channel.VIATOR,
            gross_usd=Decimal(gross), status=status,
        )
        if payout:
            Commission.objects.filter(pk=c.pk).update(payout_status=payout)
        return c

    @override_settings(ALLOWED_HOSTS=["testserver"])
    def test_the_totals_answer_both_questions(self) -> None:
        self._c("100")                                                  # to collect
        self._c("50", status=Commission.Status.CLAIMED)                 # to collect
        self._c("40", status=Commission.Status.RECEIVED)                # received
        self._c("10", status=Commission.Status.WRITTEN_OFF)             # neither

        self.client.force_login(self.staff)
        summary = self.client.get("/admin/concierge/commission/").context["summary"]
        self.assertEqual(summary["to_collect"], Decimal("150.00"))
        self.assertEqual(summary["to_collect_count"], 2)
        self.assertEqual(summary["received"], Decimal("40.00"))
        self.assertEqual(summary["owed_to_venues"], Decimal("40.00"))   # 20% of 200
        self.assertEqual(summary["net_kept"], Decimal("32.00"))         # 40 - 20%

    @override_settings(ALLOWED_HOSTS=["testserver"])
    def test_totals_follow_the_filters(self) -> None:
        other = Location.objects.create(name="Other", slug="other", commission_share_pct=Decimal("50"))
        self._c("100")
        Commission.objects.create(location=other, channel=Service.Channel.VIATOR, gross_usd=Decimal("80"))

        self.client.force_login(self.staff)
        url = f"/admin/concierge/commission/?location__id__exact={self.venue.pk}"
        summary = self.client.get(url).context["summary"]
        self.assertEqual(summary["to_collect"], Decimal("100.00"))
        self.assertEqual(summary["owed_to_venues"], Decimal("20.00"))


class QrDependencyTests(TestCase):
    """A missing optional dependency must not take the whole site down."""

    def test_the_module_imports_without_the_library(self) -> None:
        # views.py imports this module, so an import-time dependency would stop
        # Django booting at all — the guest-facing site down over an admin tool.
        with patch.dict("sys.modules", {"qrcode": None, "qrcode.image.svg": None}):
            importlib.reload(qr)
            self.assertEqual(qr.BORDER, 4)

    def test_the_hardcoded_level_still_matches_the_library(self) -> None:
        self.assertEqual(qr.ERROR_CORRECTION, qrcode.constants.ERROR_CORRECT_Q)

    def test_generating_without_the_library_raises_an_actionable_error(self) -> None:
        with patch("concierge.qr._load", side_effect=qr.QrUnavailable("not installed: pip install")):
            with self.assertRaises(qr.QrUnavailable) as ctx:
                qr.png_bytes("https://example.com/x")
        self.assertIn("pip install", str(ctx.exception))

    @override_settings(ALLOWED_HOSTS=["testserver"])
    def test_the_view_explains_itself_instead_of_500ing(self) -> None:
        Location.objects.create(name="La Bahía", slug="la-bahia")
        User = get_user_model()
        staff = User.objects.create_user("s2", password="pw", is_staff=True)
        self.client.force_login(staff)
        with patch("concierge.qr._load", side_effect=qr.QrUnavailable("Run: pip install -r requirements.txt")):
            resp = self.client.get("/qr/la-bahia.png")
        self.assertEqual(resp.status_code, 503)
        self.assertIn(b"pip install", resp.content)

    @override_settings(ALLOWED_HOSTS=["testserver"])
    def test_the_landing_page_is_unaffected(self) -> None:
        Service.objects.create(slug="taxi", name_en="Taxi")
        with patch("concierge.qr._load", side_effect=qr.QrUnavailable("nope")):
            self.assertEqual(self.client.get("/").status_code, 200)


@override_settings(ALLOWED_HOSTS=["testserver"], HOST_NAME="Joaquin")
class PrivacyPolicyAnonymityTests(TestCase):
    """The policy speaks for the service, not for a named individual."""

    def test_no_personal_name_appears(self) -> None:
        html = self.client.get("/privacy/").content.decode()
        self.assertNotIn("Joaquin", html)
        self.assertNotIn(settings.HOST_NAME, html)

    def test_it_still_says_who_to_contact(self) -> None:
        html = self.client.get("/privacy/").content.decode()
        self.assertIn("For anything about this policy", html)
        # A policy with no contact route is not a usable policy.
        self.assertTrue("wa.me" in html or "mailto:" in html or "us directly" in html)

    def test_the_relay_wording_still_reads_naturally(self) -> None:
        html = self.client.get("/privacy/").content.decode()
        self.assertIn("the provider replies to us, not to you directly", html)


class ViatorPathIsPreservedTests(TestCase):
    """The path, locale segment included, must reach Viator untouched.

    Stripping a leading /es-PE/ shipped briefly and broke every link: Viator
    re-applied the visitor's locale and the English slug no longer resolved
    under it, so product pages fell back to a destination listing.
    """

    PID = "P00313645"
    LOCALISED = "https://www.viator.com/es-PE/tours/La-Romana/Saona-Island-Excursion/d4176-17571P10"

    def test_the_locale_segment_survives(self) -> None:
        out = viator_url(self.LOCALISED, pid=self.PID)
        self.assertIn("/es-PE/tours/La-Romana/Saona-Island-Excursion/d4176-17571P10", out)

    def test_the_product_slug_and_code_survive(self) -> None:
        out = viator_url(self.LOCALISED, pid=self.PID)
        self.assertIn("Saona-Island-Excursion", out)
        self.assertIn("d4176-17571P10", out)

    def test_only_the_query_string_is_added_to(self) -> None:
        out = viator_url(self.LOCALISED, pid=self.PID, campaign="la-bahia")
        self.assertEqual(out.split("?")[0], self.LOCALISED)
        self.assertIn(f"pid={self.PID}", out)
        self.assertIn("campaign=la-bahia", out)

    def test_a_url_with_no_locale_is_equally_untouched(self) -> None:
        plain = "https://www.viator.com/tours/La-Romana/Saona/d4176-17571P10"
        self.assertEqual(viator_url(plain, pid=self.PID).split("?")[0], plain)


class BlockedPreviewTests(TestCase):
    """Some sites refuse server-side fetches; say so plainly."""

    URL = "https://www.viator.com/tours/La-Romana/ATV/d4176-238420P3"

    def _fetch_with_status(self, status):
        with patch("concierge.referral_preview.requests.get") as get:
            get.return_value = Mock(status_code=status, text="<html>challenge</html>")
            with self.assertRaises(PreviewError) as ctx:
                fetch_preview(self.URL)
        return str(ctx.exception)

    def test_bot_protection_gets_an_actionable_message(self) -> None:
        for status in (401, 403, 429):
            message = self._fetch_with_status(status)
            self.assertIn("blocks automated fetches", message)
            self.assertIn("by hand", message)
            self.assertIn("viator.com", message)

    def test_other_failures_keep_the_plain_status(self) -> None:
        self.assertIn("HTTP 500", self._fetch_with_status(500))


class ServiceAdminFormTests(TestCase):
    """The form should only offer the fields the chosen channel actually uses."""

    def setUp(self) -> None:
        from django.contrib.admin.sites import site as admin_site

        self.model_admin = admin_site._registry[Service]

    def _fields(self):
        return {f for _, opts in self.model_admin.fieldsets for f in opts["fields"]}

    def test_every_editable_field_is_reachable(self) -> None:
        # fieldsets are a whitelist: a field missing here is invisible in the
        # admin with no error anywhere.
        editable = {
            f.name for f in Service._meta.get_fields()
            if getattr(f, "editable", False) and not f.auto_created and f.name != "id"
        }
        self.assertEqual(editable - self._fields(), set())

    def test_channel_specific_groups_are_marked_for_the_form_script(self) -> None:
        classes = {c for _, opts in self.model_admin.fieldsets for c in opts.get("classes", ())}
        self.assertIn("referral-only", classes)
        self.assertIn("whatsapp-only", classes)

    def test_the_referral_url_sits_in_the_referral_group(self) -> None:
        for name, opts in self.model_admin.fieldsets:
            if "referral_url" in opts["fields"]:
                self.assertIn("referral-only", opts.get("classes", ()))
                break
        else:
            self.fail("referral_url is not in any fieldset")

    def test_provider_routing_sits_in_the_whatsapp_group(self) -> None:
        for name, opts in self.model_admin.fieldsets:
            if "default_provider" in opts["fields"]:
                self.assertIn("whatsapp-only", opts.get("classes", ()))
                break
        else:
            self.fail("default_provider is not in any fieldset")

    def test_the_toggle_script_is_loaded(self) -> None:
        self.assertIn("concierge/admin/service_form.js", list(self.model_admin.media._js))
