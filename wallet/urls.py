from django.urls import path

from wallet.views import WalletViewSet

urlpatterns = [
    path("", WalletViewSet.as_view({"get": "me"}), name="wallet-me"),
    path("transactions/", WalletViewSet.as_view({"get": "transactions"}), name="wallet-txns"),

    path("deposit/", WalletViewSet.as_view({"post": "deposit"}), name="wallet-deposit"),
    path("deposit_status/", WalletViewSet.as_view({"get": "deposit_status"}), name="wallet-deposit-status"),

    path("withdraw/decode/", WalletViewSet.as_view({"post": "decode_withdrawal"}), name="wallet-withdraw-decode"),
    path("withdraw/fees/", WalletViewSet.as_view({"post": "withdrawal_fees"}), name="wallet-withdraw-fees"),
    path("withdraw/", WalletViewSet.as_view({"post": "withdraw"}), name="wallet-withdraw"),

    path("bitcoin/", WalletViewSet.as_view({"get": "my_bitcoin_address", "post": "generate_deposit_address"}), name="wallet-bitcoin"),

    path("blink/", WalletViewSet.as_view({"get": "blink_status"}), name="wallet-blink"),
    path("onchain/", WalletViewSet.as_view({"get": "onchain_status"}), name="wallet-onchain"),
    path("amatopay/", WalletViewSet.as_view({"get": "amatopay_status"}), name="wallet-amatopay"),
    path("amatopay/verify-alias/", WalletViewSet.as_view({"post": "amatopay_verify_alias"}), name="wallet-amatopay-verify-alias"),

    path("exchange/rate/", WalletViewSet.as_view({"get": "exchange_rate"}), name="wallet-exchange-rate"),
    path("exchange/quote/", WalletViewSet.as_view({"post": "exchange_quote"}), name="wallet-exchange-quote"),
    path("exchange/convert/", WalletViewSet.as_view({"post": "exchange_convert"}), name="wallet-exchange-convert"),

    path("pos/charge/", WalletViewSet.as_view({"post": "pos_charge"}), name="wallet-pos-charge"),
    path("pos/charge/bif/", WalletViewSet.as_view({"post": "pos_charge_bif"}), name="wallet-pos-charge-bif"),
    path("pos/charge/<str:pos_charge_id>/", WalletViewSet.as_view({"get": "pos_charge_status"}), name="wallet-pos-charge-status"),
    path("pos/charge/<str:pos_charge_id>/confirm-delivery/", WalletViewSet.as_view({"post": "pos_charge_confirm_delivery"}), name="wallet-pos-charge-confirm-delivery"),

    path("bif/topup/", WalletViewSet.as_view({"post": "bif_topup"}), name="wallet-bif-topup"),
    path("bif/topup/<str:session_id>/", WalletViewSet.as_view({"get": "bif_topup_status"}), name="wallet-bif-topup-status"),
    path("bif/topup/<str:session_id>/confirm-delivery/", WalletViewSet.as_view({"post": "bif_topup_confirm_delivery"}), name="wallet-bif-topup-confirm-delivery"),
]
