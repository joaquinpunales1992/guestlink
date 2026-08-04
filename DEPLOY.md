# Deploying guestlink to Namecheap shared hosting

Target: `https://bookyourtickets.online` on a Namecheap cPanel shared plan.

| Thing | Value |
| --- | --- |
| cPanel user | `akiyuvpp` (verify with `echo $HOME`) |
| Application root (code) | `~/guestlink` |
| Document root (web) | `~/bookyourtickets.online` |
| Virtualenv | `~/virtualenv/guestlink/3.9` |
| Python | 3.9.23 — the highest the cPanel selector offers |
| Django | 4.2 LTS — the last release supporting Python 3.9 |

Commands below use `~` so they work regardless of the exact username. The code
lives **outside** the document root on purpose, and the SQLite database sits
next to the code, so nothing web-servable touches it.

---

## Already done

- DNS: nameservers `dns1/dns2.namecheaphosting.com`, A record → `162.255.119.223`
- Domain created in cPanel → Domains, document root `/bookyourtickets.online`
- **Valid SSL certificate** (SSL.com via AutoSSL, expires 2027-02-17)
- HTTP → HTTPS redirect active
- Python app registered in Setup Python App

## Step 1 — Switch the app to Python 3.9.23

The app was created on **3.6.15**, which cannot run this stack: Django 4.2 needs
3.9+, and the `anthropic` SDK needs 3.8+. `pip install` fails on the first
package.

Setup Python App → open `guestlink` → change **Python version** to **3.9.23** →
Save. cPanel rebuilds the virtualenv (nothing is installed in the 3.6 one, so
nothing is lost). Confirm the shell prompt afterwards reads `((guestlink:3.9))`.

## Step 2 — Protect the ACME path

cPanel's Passenger `.htaccess` hands every request to Django, including the
`/.well-known/` files AutoSSL uses to prove domain ownership. Without this the
certificate fails to renew — silently, in February.

Edit `~/bookyourtickets.online/.htaccess` and put these lines **above** the
`PassengerAppRoot` block:

```apache
RewriteEngine On
RewriteRule ^\.well-known/ - [L]
```

Re-add this after any change made in Setup Python App — cPanel rewrites that
file and will drop the rule.

## Step 3 — Get the code onto the server

The repo is **private**, so cloning needs a deploy key. In cPanel → Terminal:

```bash
ssh-keygen -t ed25519 -C "guestlink-deploy" -f ~/.ssh/github_guestlink -N ""
cat ~/.ssh/github_guestlink.pub
```

Add that public key at GitHub → **guestlink** → Settings → Deploy keys (read-only
is enough). Then:

```bash
cat >> ~/.ssh/config <<'EOF'
Host github.com
  IdentityFile ~/.ssh/github_guestlink
  StrictHostKeyChecking accept-new
EOF
chmod 600 ~/.ssh/config
```

Setup Python App already created `~/guestlink` with a stub `passenger_wsgi.py`.
Clone to a temp directory and copy the contents in, leaving cPanel's app
registration intact:

```bash
git clone git@github.com:joaquinpunales1992/guestlink.git ~/src-guestlink
cp -r ~/src-guestlink/. ~/guestlink/
rm -rf ~/src-guestlink
```

The repo's `passenger_wsgi.py` intentionally replaces cPanel's stub.

Later updates: `cd ~/guestlink && git pull`

## Step 4 — Install dependencies

```bash
source ~/virtualenv/guestlink/3.9/bin/activate
cd ~/guestlink
pip install -r requirements.txt
```

`uv` is not available on shared hosting — that is what `requirements.txt` is for.
Keep it in sync with `pyproject.toml` when adding a dependency.

Expected versions on 3.9: Django 4.2.30, anthropic 0.120.x, Pillow 11.3.x,
whitenoise 6.11.x.

## Step 5 — Create the production `.env`

```bash
cp .env.production.example .env
python -c "import secrets; print(secrets.token_urlsafe(32))"   # verify token
echo $HOME    # confirm the absolute paths below
nano .env
```

**You do not need to generate a secret key.** With `DJANGO_SECRET_KEY` left
blank and `DEBUG=0`, settings mints one on first boot and stores it in
`~/guestlink/.secret_key` (mode 0600, gitignored, outside the docroot). Set the
variable only if you want to manage the key yourself — a non-empty value always
wins over the file. Deleting `.secret_key` mints a new one and logs out every
session.

Minimum to fill in (substitute your real `$HOME`):

```
DJANGO_DEBUG=0
DJANGO_ALLOWED_HOSTS=bookyourtickets.online,www.bookyourtickets.online
DJANGO_CSRF_TRUSTED_ORIGINS=https://bookyourtickets.online,https://www.bookyourtickets.online
DJANGO_DB_PATH=/home/akiyuvpp/guestlink/db.sqlite3
DJANGO_STATIC_ROOT=/home/akiyuvpp/guestlink/staticfiles
DJANGO_MEDIA_ROOT=/home/akiyuvpp/guestlink/media
WHATSAPP_VERIFY_TOKEN=<second command's output>
WHATSAPP_DRY_RUN=1
```

`.env` is gitignored and must never be committed. The committed placeholder
secret key is never used when `DEBUG=0` — production always gets either your
explicit key or the provisioned one, so a forgeable session cookie cannot ship
by accident.

## Step 6 — Migrate, collect static, create the admin user

```bash
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py createsuperuser
```

Static files are served by WhiteNoise from inside the WSGI app and `/media/` by
Django, so neither needs an Apache rule.

## Step 7 — Pre-flight check

```bash
python manage.py check_deploy
```

Verifies the interpreter, config, file permissions, and — the real
shared-hosting unknown — whether outbound HTTPS to `graph.facebook.com` and
`api.anthropic.com` is permitted. If those probes fail, the relay accepts
webhooks and creates tickets but never delivers a message, which is invisible
from the outside. Non-zero exit on failure.

## Step 8 — Restart Passenger

```bash
mkdir -p ~/guestlink/tmp && touch ~/guestlink/tmp/restart.txt
```

Or the **Restart** button in Setup Python App. Code changes do nothing until
this happens.

## Step 9 — Smoke test

```bash
curl -s https://bookyourtickets.online/healthz/
curl -so /dev/null -w "%{http_code}\n" https://bookyourtickets.online/nonexistent-xyz
```

`/healthz/` must return JSON, and a nonsense URL must return **404**. While the
cPanel placeholder is still active, *every* URL returns 200 with
`It works! Python v3.6.15` — that 404 is how you know Django is actually live.

Then check `/` and `/the-reef-401` render the landing page, and log in at
`/admin/`.

## Step 10 — Register the webhook with Meta

- Callback URL: `https://bookyourtickets.online/webhook/whatsapp/`
- Verify token: exactly the `WHATSAPP_VERIFY_TOKEN` from `.env`
- Subscribe to the **messages** field

Meta immediately GETs the callback with a `hub.challenge` and requires that value
echoed back verbatim. Then fill in `WHATSAPP_PHONE_NUMBER_ID`,
`WHATSAPP_BUSINESS_NUMBER`, `WHATSAPP_ACCESS_TOKEN`, set `WHATSAPP_DRY_RUN=0`,
and restart Passenger.

---

## Known limitations

**Django 4.2 is past end-of-life (April 2026).** It receives no security
patches. This is the cost of Namecheap's Python selector capping at 3.9, and it
is the most significant compromise in this deployment. `check_deploy` prints a
standing reminder.

Newer interpreters *are* installed on the server — `/opt/alt/python310` through
`/opt/alt/python313` — they are simply not exposed by the cPanel selector. Two
ways to reach a supported Django later:

1. Ask Namecheap support to expose 3.11+ in the Python selector, then bump
   `requirements.txt`/`pyproject.toml` to Django 5.2 LTS (supported to 2028).
2. Override the interpreter yourself with `PassengerPython
   "/opt/alt/python313/bin/python3"` in the docroot `.htaccess` and build the
   virtualenv by hand. Works, but cPanel overwrites that file whenever the app
   is edited, silently reverting you to the selector's Python.

**Webhook latency.** `handle_inbound` classifies with Claude and calls the Graph
API synchronously inside the request. On shared hosting that can take seconds;
Meta retries webhooks it considers slow, which can duplicate tickets. If that
appears in practice, move the relay work off the request path and return 200
immediately.

**SQLite under Passenger.** Passenger may run several worker processes against
one SQLite file. Fine at one-apartment volume (20s busy timeout set), but the
first thing to outgrow.

**No background jobs.** Follow-up automation from the README roadmap needs cPanel
cron, not a long-running worker.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| Every URL returns `It works! Python v3.6.15` | Placeholder still active — code not deployed, or Passenger not restarted. |
| `pip install` fails on the first package | Still on Python 3.6/3.7/3.8. Switch the app to 3.9.23 (step 1). |
| 500 on every page | Check stderr in Setup Python App, or `~/guestlink/tmp/`. Usually a missing `.env` value. |
| Static files 404 | `collectstatic` not run, or `DJANGO_STATIC_ROOT` unwritable. |
| Admin login "CSRF verification failed" | `DJANGO_CSRF_TRUSTED_ORIGINS` missing the `https://` scheme. |
| Infinite redirect loop | cPanel Force HTTPS Redirect *and* `DJANGO_SECURE_SSL_REDIRECT=1` both on. Turn the Django one off. |
| Code changes do nothing | Passenger not restarted — `touch ~/guestlink/tmp/restart.txt`. |
| AutoSSL renewal fails | The `.well-known` rule (step 2) is missing from `.htaccess`. |
| Tickets created, nothing delivered | Outbound HTTPS blocked — run `check_deploy`. |
