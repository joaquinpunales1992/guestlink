# guestlink

WhatsApp relay for a short-stay host: guest scans a QR in the apartment, taps a
service, and we (the host) route the conversation to a local provider while
keeping visibility for commission auditing.

**Status:** scaffold for the v1 side-project. Everything works end-to-end with
`WHATSAPP_DRY_RUN=1` (outbound messages logged, not sent). To actually wire it
to WhatsApp you need an approved Meta Business number — see "Going live" below.

## How it works

```
QR  →  landing page  →  wa.me deep link (pre-populated text)
                              ↓
   guest taps → WhatsApp opens → message sent to our business number
                              ↓
   Meta posts to /webhook/whatsapp/  →  Django relay
                              ↓
   classify (Claude / keywords) → pick Service → pick Provider →
   create Ticket(short_code) → DM provider with `[CODE]` prefix +
   ack the guest
                              ↓
   provider replies (starting with `[CODE]`) → relay strips code →
   forwards to guest. All messages persisted under the Ticket.
```

The host (you) sees the whole thread in `/admin/`. Neither side sees the other
number until you choose to surface it.

## Setup

```bash
cd guestlink
cp .env.example .env       # edit secrets if you want
uv sync
uv run python manage.py migrate
uv run python manage.py seed_demo          # 4 demo services + providers
uv run python manage.py createsuperuser
uv run python manage.py runserver
```

Open:
- `http://127.0.0.1:8000/` — the QR landing page
- `http://127.0.0.1:8000/admin/` — services, providers, tickets, messages
- `http://127.0.0.1:8000/healthz/` — quick liveness check

## Running tests

```bash
uv run python manage.py test concierge
```

## What's stubbed in v1

| Piece | Status | Why |
| --- | --- | --- |
| WhatsApp send | Dry-run logs to console | No business number yet. Flip `WHATSAPP_DRY_RUN=0` once Cloud API creds are in `.env`. |
| Claude classifier | Falls back to keyword matching | Works without `ANTHROPIC_API_KEY`. Set the key to use Claude Haiku 4.5. |
| Multi-host | Single host hardcoded via env (`HOST_NAME`, `HOST_APARTMENT_LABEL`) | v1 is laboratory only. Multi-host comes after we validate the model. |
| Payments | Not handled | The `expected_commission_usd` field is informational; reconciliation is manual via admin. |
| Auth on landing page | None | Public URL behind the QR is acceptable for v1. |
| Translation | EN/ES toggle on landing only | If a guest writes in DE/FR, the provider has to manage. Classifier still parses the language tag. |

## Going live (the order matters)

1. **Validate the model with providers first.** Call/visit the 3–4 providers
   you'd start with (Saona lanchero, taxi, car rental, food delivery). Confirm:
   - Fixed USD commission they'll accept per closed ticket.
   - They're OK with you forwarding leads and seeing the WhatsApp thread.
   - They'll reply prefixing their messages with the `[CODE]` you send.
   Without this, the rest is wasted work.
2. **Get a clean WhatsApp number.** A SIM in RD or a virtual number, with the
   constraint that it has **never** been used in WhatsApp personal or Business
   app. This trips a lot of people up.
3. **Create a Meta Business / WhatsApp Business Platform account**, register
   the number, generate a permanent access token, note the `phone_number_id`
   and the actual business phone number.
4. **Fill `.env`** with `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_BUSINESS_NUMBER`,
   `WHATSAPP_ACCESS_TOKEN`, and set `WHATSAPP_DRY_RUN=0`.
5. **Deploy** somewhere with HTTPS (Railway / Fly.io / Render). Set
   `DJANGO_ALLOWED_HOSTS`, `DJANGO_SECRET_KEY`, and a strong `WHATSAPP_VERIFY_TOKEN`.
6. **Register the webhook in Meta** pointing at `https://YOURDOMAIN/webhook/whatsapp/`
   with the same `WHATSAPP_VERIFY_TOKEN`. Subscribe to the `messages` field.
7. **Print the QR** pointing at your deployed landing URL (e.g.
   `https://guestlink.tu-dominio.com/`).

## Project layout

```
guestlink/
├── guestlink/settings.py        # env-driven config
├── guestlink/urls.py            # landing, webhook, admin, healthz
├── concierge/
│   ├── models.py                # Service, Provider, Guest, Ticket, Message
│   ├── admin.py                 # full-featured admin with inlines + actions
│   ├── classifier.py            # Claude / keyword classifier for first messages
│   ├── relay.py                 # the routing brain (handle_inbound)
│   ├── whatsapp.py              # Cloud API client (honors WHATSAPP_DRY_RUN)
│   ├── views.py                 # webhook + landing
│   ├── templates/concierge/landing.html
│   ├── management/commands/seed_demo.py
│   └── tests.py                 # relay routing smoke tests
```

## Next steps (in rough priority)

- Replace keyword classifier with Claude in production once you've validated
  what guests actually ask for (open the admin, sort tickets by `extracted_fields`).
- Follow-up automation: cron 24h after `created_at` → send guest "how did it go?"
  message → close ticket on response.
- Provider-side reconciliation report: weekly digest of closed tickets per
  provider with expected commission.
- Multi-host: once the model is proven, add a `Host` model and namespace
  Services/QRs under it.
