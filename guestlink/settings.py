"""Django settings for guestlink."""

import os
import secrets
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _env_bool(key: str, default: bool = False) -> bool:
    return os.environ.get(key, "1" if default else "0").lower() in {"1", "true", "yes"}


def _env_list(key: str, default: str = "") -> list[str]:
    return [v.strip() for v in os.environ.get(key, default).split(",") if v.strip()]


DEBUG = _env_bool("DJANGO_DEBUG", True)

# Committed to git, so it is only ever acceptable in DEBUG. Production either
# gets an explicit DJANGO_SECRET_KEY or a key provisioned on disk (below).
_INSECURE_KEY = "django-insecure-l)=$30y67zj(m+u8o1u3+pqs5f=r*0++-hxhi80(d%xsml($eu"

# Django's own get_random_secret_key charset, inlined: importing
# django.core.management from a settings module that is still executing risks
# a circular import.
_KEY_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789!@#$%^&*(-_=+)"
SECRET_KEY_FILE = BASE_DIR / ".secret_key"


def _provisioned_secret_key() -> str:
    """Return the on-disk secret key, minting it once on first boot.

    Hand-editing `.env` on shared hosting proved error-prone — a blank
    `DJANGO_SECRET_KEY=` line reads as set-but-empty and took the whole site
    down. This removes the manual step: the file lives next to the code
    (outside the web docroot), is gitignored, and is created 0600 so only the
    hosting user can read it.

    Created with O_EXCL because Passenger boots several workers at once and two
    of them racing here would otherwise end up with different keys, logging
    guests out at random.

    Deleting the file mints a new key and invalidates every existing session.
    """
    if not SECRET_KEY_FILE.exists():
        generated = "".join(secrets.choice(_KEY_CHARS) for _ in range(50))
        try:
            fd = os.open(SECRET_KEY_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass  # another worker won the race; fall through and read theirs
        except OSError as exc:
            raise ImproperlyConfigured(
                f"No DJANGO_SECRET_KEY is set and {SECRET_KEY_FILE} could not be "
                f"created ({exc}). Either make that directory writable or set "
                "DJANGO_SECRET_KEY in .env."
            ) from exc
        else:
            with os.fdopen(fd, "w") as handle:
                handle.write(generated)

    key = SECRET_KEY_FILE.read_text().strip()
    if not key:
        raise ImproperlyConfigured(
            f"{SECRET_KEY_FILE} is empty. Delete it and restart to mint a new key "
            "(this logs out every existing session)."
        )
    return key


# `.strip()` matters: an env var set to whitespace is the same mistake as a
# blank one, and Django would otherwise accept it and fail much later.
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "").strip()
if not SECRET_KEY:
    SECRET_KEY = _INSECURE_KEY if DEBUG else _provisioned_secret_key()

ALLOWED_HOSTS = _env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")

# Django 4+ needs the scheme-qualified origin for admin POSTs over HTTPS.
CSRF_TRUSTED_ORIGINS = _env_list("DJANGO_CSRF_TRUSTED_ORIGINS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "concierge",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Serves collected static files straight from the WSGI app. On cPanel this
    # avoids fighting Passenger over which requests Apache handles.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "guestlink.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "guestlink.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        # Keep this OUTSIDE the web docroot in production, or the whole
        # database is downloadable over HTTP.
        "NAME": os.environ.get("DJANGO_DB_PATH", BASE_DIR / "db.sqlite3"),
        "OPTIONS": {"timeout": 20},
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = Path(os.environ.get("DJANGO_STATIC_ROOT", BASE_DIR / "staticfiles"))
MEDIA_URL = "media/"
MEDIA_ROOT = Path(os.environ.get("DJANGO_MEDIA_ROOT", BASE_DIR / "media"))
SERVE_MEDIA = _env_bool("DJANGO_SERVE_MEDIA", True)
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

# --- production hardening (only bites when DJANGO_DEBUG=0) ---
if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    X_FRAME_OPTIONS = "DENY"
    # Off by default: on cPanel the HTTP->HTTPS redirect belongs in .htaccess.
    # Turning this on without a correct forwarded-proto header loops forever.
    SECURE_SSL_REDIRECT = _env_bool("DJANGO_SECURE_SSL_REDIRECT", False)
    if _env_bool("DJANGO_TRUST_FORWARDED_PROTO", False):
        SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    # Start at 0. Raise to 31536000 only once HTTPS is confirmed working —
    # HSTS is hard to undo in guests' browsers.
    SECURE_HSTS_SECONDS = int(os.environ.get("DJANGO_HSTS_SECONDS", "0"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = SECURE_HSTS_SECONDS > 0
    SECURE_HSTS_PRELOAD = False

# --- guestlink config ---
HOST_NAME = os.environ.get("HOST_NAME", "Joaquin")
HOST_APARTMENT_LABEL = os.environ.get("HOST_APARTMENT_LABEL", "Reef")

# Optional second contact route on /privacy/. Left blank the page points guests
# at the WhatsApp business number, which is the channel they already use.
PRIVACY_CONTACT_EMAIL = os.environ.get("PRIVACY_CONTACT_EMAIL", "").strip()

# The path baked into the printed QR cards in print/ (payload:
# https://bookyourtickets.online/the-reef-401). Serves the same landing page as
# "/". Changing this orphans every card already hanging in the apartment.
APARTMENT_SLUG = os.environ.get("APARTMENT_SLUG", "the-reef-401").strip("/")

WHATSAPP_PHONE_NUMBER_ID = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
WHATSAPP_BUSINESS_NUMBER = os.environ.get("WHATSAPP_BUSINESS_NUMBER", "")
WHATSAPP_ACCESS_TOKEN = os.environ.get("WHATSAPP_ACCESS_TOKEN", "")
WHATSAPP_VERIFY_TOKEN = os.environ.get("WHATSAPP_VERIFY_TOKEN", "local-verify-token")
WHATSAPP_DRY_RUN = _env_bool("WHATSAPP_DRY_RUN", True)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"simple": {"format": "[{levelname}] {name}: {message}", "style": "{"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "simple"}},
    "loggers": {
        "concierge": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}
