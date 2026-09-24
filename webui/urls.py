from django.urls import path

from webui import views

app_name = "webui"

urlpatterns = [
    path("", views.landing, name="landing"),
    path("register/", views.register_view, name="register"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),

    path("app/", views.dashboard, name="dashboard"),
    path("app/deposit/", views.deposit_view, name="deposit"),
    path("app/deposit/status/<str:payment_hash>/", views.deposit_status_json, name="deposit_status"),
    path("app/withdraw/", views.withdraw_view, name="withdraw"),
    path("app/bitcoin/", views.bitcoin_address_view, name="bitcoin_address"),
    path("app/transactions/", views.transactions_view, name="transactions"),
    path("app/exchange/", views.exchange_view, name="exchange"),
    path("app/amatopay/verify-alias/", views.amatopay_verify_alias_json, name="amatopay_verify_alias"),
    path("app/pos/", views.pos_view, name="pos"),
    path("app/pos/status/<uuid:charge_id>/", views.pos_status_json, name="pos_status"),
    path("app/pos/return/", views.pos_return_view, name="pos_return"),
    path("app/pos/<uuid:charge_id>/confirm-delivery/", views.pos_confirm_delivery_view, name="pos_confirm_delivery"),
    path("app/topup/", views.topup_view, name="topup"),
    path("app/topup/return/", views.topup_return_view, name="topup_return"),
    path("app/topup/status/<str:session_id>/", views.topup_status_json, name="topup_status"),
    path("app/topup/<str:session_id>/confirm-delivery/", views.topup_confirm_delivery_view, name="topup_confirm_delivery"),
]
