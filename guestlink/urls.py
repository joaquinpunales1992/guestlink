import re

from django.conf import settings
from django.contrib import admin
from django.urls import path, re_path
from django.views.static import serve

from concierge import views as concierge_views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", concierge_views.landing, name="landing"),
    path("webhook/whatsapp/", concierge_views.whatsapp_webhook, name="whatsapp_webhook"),
    path("ticket/<str:short_code>/", concierge_views.ticket_thread, name="ticket_thread"),
    path("privacy/", concierge_views.privacy, name="privacy"),
    path("healthz/", concierge_views.healthz, name="healthz"),
    path("go/<slug:service_slug>/", concierge_views.go, name="go"),
    # Staff-only, and declared before the location catch-all below.
    path("qr/<slug:slug>.<str:fmt>", concierge_views.location_qr, name="location_qr"),
]

# Every QR code points at /<location-slug>. These come last so the fixed routes
# above always win, and an unknown slug 404s in the landing view.
#
# Both forms are registered because printed QR payloads have no trailing slash,
# and APPEND_SLASH would otherwise 301 before the page renders — a redirect the
# guest pays for on mobile data.
urlpatterns += [
    path("<slug:slug>/", concierge_views.landing, name="landing_location"),
    path("<slug:slug>", concierge_views.landing, name="landing_location_noslash"),
]

# Uploaded service images. WhiteNoise only covers collected *static* files, and
# on cPanel/Passenger you cannot rely on Apache picking up /media/ itself — so
# Django serves it. Fine at this scale (a handful of small images); set
# DJANGO_SERVE_MEDIA=0 if you later front it with a real web server rule.
if settings.SERVE_MEDIA:
    urlpatterns += [
        re_path(
            r"^%s(?P<path>.*)$" % re.escape(settings.MEDIA_URL.lstrip("/")),
            serve,
            {"document_root": settings.MEDIA_ROOT},
        ),
    ]
