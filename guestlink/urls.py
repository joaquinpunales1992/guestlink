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
    path("healthz/", concierge_views.healthz, name="healthz"),
]

# The QR cards already printed for the apartment encode
# /the-reef-401, not /. Route it to the same landing page so those
# cards keep working; guests who type the bare domain land in the same place.
if settings.APARTMENT_SLUG:
    urlpatterns.append(
        path(f"{settings.APARTMENT_SLUG}/", concierge_views.landing, name="landing_apartment"),
    )
    # Without the trailing slash Django's APPEND_SLASH would 301 first; the QR
    # payload has no trailing slash, so serve it directly and skip the redirect.
    urlpatterns.append(
        path(settings.APARTMENT_SLUG, concierge_views.landing, name="landing_apartment_noslash"),
    )

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
