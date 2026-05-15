from django.contrib import admin
from django.urls import path

from concierge import views as concierge_views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", concierge_views.landing, name="landing"),
    path("webhook/whatsapp/", concierge_views.whatsapp_webhook, name="whatsapp_webhook"),
    path("healthz/", concierge_views.healthz, name="healthz"),
]
