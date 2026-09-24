from django.contrib import admin
from django.urls import include, path

from wallet.amatopay_webhooks import amatopay_webhook

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/auth/", include("accounts.urls")),
    path("api/wallet/", include("wallet.urls")),
    path("webhooks/amatopay/", amatopay_webhook, name="amatopay-webhook"),
    path("", include("webui.urls")),
]
