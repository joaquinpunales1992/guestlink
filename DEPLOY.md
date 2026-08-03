# Deploying guestlink to Namecheap shared hosting

Target: `https://bookyourtickets.online` on a Namecheap cPanel shared plan.

Concrete values for this account — substitute if anything differs:

| Thing | Value |
| --- | --- |
| cPanel user | `bookyoq0` |
| Home | `/home/bookyoq0` |
| Application root (code) | `/home/bookyoq0/guestlink` |
| Document root (web) | `/home/bookyoq0/bookyourtickets.online` |
| Virtualenv | `/home/bookyoq0/virtualenv/guestlink/3.13` |
| Python | 3.13 → Django 6.0.x (same as local dev) |

The code lives **outside** the document root on purpose. The SQLite database sits
next to the code, so nothing the web server can serve directly touches it.

---

## 0. Prerequisites (already done)

- DNS: nameservers `dns1/dns2.namecheaphosting.com`, A record → `162.255.119.223`
- Domain created in cPanel → Domains with docroot `/bookyourtickets.online`
- Python app created via Setup Python App

---

## 1. Protect the ACME path before anything else

cPanel's Passenger `.htaccess` hands every request to Django, including the
`/.well-known/` files AutoSSL uses to prove domain ownership. Without this,
certificate issuance *and* every 90-day renewal fails silently.

Edit `/home/bookyoq0/bookyourtickets.online/.htaccess` and put these two lines
**above** the `PassengerAppRoot` block:

```apache
RewriteEngine On
RewriteRule ^\.well-known/ - [L]
```

## 2. Issue the certificate

cPanel → **SSL/TLS Status** → tick `bookyourtickets.online` and
`www.bookyourtickets.online` → **Run AutoSSL**.

Verify from anywhere:

```bash
curl -I https://bookyourtickets.online/
```

> **Known blocker on this account:** port 443 currently times out from outside
> while port 80 answers. If it is still closed after AutoSSL, this is not a
> setting you can fix — open a Namecheap ticket: *"port 443 is not responding
> for bookyourtickets.online on shared IP 162.255.119.223; port 80 works."*
> No HTTPS means no WhatsApp webhook, so this gates everything below.

Once the cert is live, turn on **Force HTTPS Redirect** in cPanel → Domains.
Do not set `DJANGO_SECURE_SSL_REDIRECT=1` as well — two redirect layers is how
you get a loop.

## 3. Get the code onto the server

cPanel → **Terminal**. The repo is private, so authenticate with a GitHub
deploy key rather than pasting a token into `.git/config`:

```bash
ssh-keygen -t ed25519 -C "bookyoq0-guestlink" -f ~/.ssh/github_guestlink -N ""
cat ~/.ssh/github_guestlink.pub
```

Add that public key at GitHub → repo **guestlink** → Settings → Deploy keys →
Add deploy key (read-only is enough). Then:

```bash
cat >> ~/.ssh/config <<'EOF'
Host github.com
  IdentityFile ~/.ssh/github_guestlink
  StrictHostKeyChecking accept-new
EOF
chmod 600 ~/.ssh/config
```

Setup Python App already created `/home/bookyoq0/guestlink` with a stub
`passenger_wsgi.py`. Clone into a temp dir and move the contents in, so cPanel's
own app registration is left intact:

```bash
git clone git@github.com:joaquinpunales1992/guestlink.git ~/src-guestlink
cp -r ~/src-guestlink/. /home/bookyoq0/guestlink/
rm -rf ~/src-guestlink
```

The repo's `passenger_wsgi.py` intentionally overwrites cPanel's stub.

For later updates:

```bash
cd /home/bookyoq0/guestlink && git pull
```

## 4. Install dependencies

```bash
source /home/bookyoq0/virtualenv/guestlink/3.13/bin/activate
cd /home/bookyoq0/guestlink
pip install -r requirements.txt
```

`uv` is not available on shared hosting — that is what `requirements.txt` is
for. Keep it in sync with `pyproject.toml` when you add a dependency.

## 5. Create the production `.env`

```bash
cp .env.production.example .env
python -c "from django.core.management.utils import get_random_secret_key as k; print(k())"
python -c "import secrets; print(secrets.token_urlsafe(32))"   # verify token
nano .env
```

Fill in at minimum:

```
DJANGO_SECRET_KEY=<first command's output>
DJANGO_DEBUG=0
DJANGO_ALLOWED_HOSTS=bookyourtickets.online,www.bookyourtickets.online
DJANGO_CSRF_TRUSTED_ORIGINS=https://bookyourtickets.online,https://www.bookyourtickets.online
DJANGO_DB_PATH=/home/bookyoq0/guestlink/db.sqlite3
DJANGO_STATIC_ROOT=/home/bookyoq0/guestlink/staticfiles
DJANGO_MEDIA_ROOT=/home/bookyoq0/guestlink/media
WHATSAPP_VERIFY_TOKEN=<second command's output>
WHATSAPP_DRY_RUN=1
```

`.env` is gitignored and must never be committed. Settings refuse to boot with
`DEBUG=0` and the committed placeholder secret key, so a missing key fails loudly
rather than shipping a forgeable session cookie.

## 6. Migrate, collect static, create the admin user

```bash
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py createsuperuser
```

Static files are served by WhiteNoise from inside the WSGI app, and `/media/` is
served by Django. Neither needs an Apache rule, which is why the empty docroot
is fine.

## 7. Pre-flight check

```bash
python manage.py check_deploy
```

This verifies config, file permissions, and — the real shared-hosting unknown —
whether outbound HTTPS to `graph.facebook.com` and `api.anthropic.com` is
permitted. If those probes fail, the relay will accept webhooks and create
tickets but never deliver a single message. Exit code is non-zero on failure.

## 8. Restart Passenger

Code changes are not picked up until the app restarts:

```bash
mkdir -p /home/bookyoq0/guestlink/tmp
touch /home/bookyoq0/guestlink/tmp/restart.txt
```

Or use the **Restart** button in Setup Python App.

## 9. Smoke test

```bash
curl -s https://bookyourtickets.online/healthz/
curl -sI https://bookyourtickets.online/the-reef-401 | head -1
```

- `/` and `/the-reef-401` → the landing page. **`/the-reef-401` is the URL baked
  into the printed QR cards in `print/`** — verify it returns 200, not 404.
- `/admin/` → log in with the superuser you just made.

## 10. Register the webhook with Meta

Only after HTTPS is confirmed working.

- Callback URL: `https://bookyourtickets.online/webhook/whatsapp/`
- Verify token: exactly the `WHATSAPP_VERIFY_TOKEN` from `.env`
- Subscribe to the **messages** field

Meta immediately GETs the callback to verify. Then fill in
`WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_BUSINESS_NUMBER`, `WHATSAPP_ACCESS_TOKEN`,
flip `WHATSAPP_DRY_RUN=0`, and restart Passenger.

---

## Known limitations of this setup

**Webhook latency.** `handle_inbound` classifies with Claude and calls the Graph
API synchronously inside the request. On shared hosting that can take several
seconds; Meta retries webhooks it considers slow, which can duplicate tickets.
If that shows up in practice, move the relay work to a thread or a queue and
return 200 immediately.

**SQLite under Passenger.** Passenger may run several worker processes against
one SQLite file. Fine at one-apartment volume (a 20s busy timeout is set), but
it is the first thing to outgrow.

**No background jobs.** Follow-up automation from the README's roadmap needs
cPanel cron, not a long-running worker.

**Deploys are not atomic.** `git pull` + restart has a brief window of mixed
state. Irrelevant at this scale, worth knowing.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| 500 on every page | Check `stderr` in Setup Python App, or `~/guestlink/tmp/`. Usually a missing `.env` value. |
| Static files 404 | `collectstatic` not run, or `DJANGO_STATIC_ROOT` unwritable. |
| Admin login "CSRF verification failed" | `DJANGO_CSRF_TRUSTED_ORIGINS` missing the `https://` scheme. |
| Infinite redirect loop | Force HTTPS Redirect *and* `DJANGO_SECURE_SSL_REDIRECT=1` both on. Turn the Django one off. |
| Code changes do nothing | Passenger not restarted — `touch tmp/restart.txt`. |
| AutoSSL renewal fails | The `.well-known` rule in step 1 is missing from `.htaccess`. |
| Tickets created, nothing delivered | Outbound HTTPS blocked — run `check_deploy`. |
