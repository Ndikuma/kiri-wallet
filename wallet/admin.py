from django.contrib import admin

from .models import (
    AmatoPayCheckoutSession,
    ExchangeRate,
    POSCharge,
    Wallet,
    WalletTransaction,
    WithdrawalFeePolicy,
)


@admin.register(Wallet)
class WalletAdmin(admin.ModelAdmin):
    list_display = ["user", "available_balance", "pending_balance", "bif_balance", "is_platform", "updated_at"]
    list_filter = ["is_platform"]
    search_fields = ["user__username", "user__email", "bitcoin_address"]


@admin.register(WalletTransaction)
class WalletTransactionAdmin(admin.ModelAdmin):
    list_display = ["id", "wallet", "type", "currency", "amount", "status", "created_at"]
    list_filter = ["type", "currency", "status"]
    search_fields = ["id", "lnd_payment_hash", "onchain_txid", "wallet__user__username"]


@admin.register(WithdrawalFeePolicy)
class WithdrawalFeePolicyAdmin(admin.ModelAdmin):
    list_display = ["target_type", "display_label", "fixed_fee_sats", "percent_fee_bps", "charge_to_user", "is_active"]
    list_filter = ["is_active", "charge_to_user"]


@admin.register(ExchangeRate)
class ExchangeRateAdmin(admin.ModelAdmin):
    list_display = ["bif_per_btc", "source", "is_active", "created_at"]
    list_filter = ["is_active", "source"]


@admin.register(POSCharge)
class POSChargeAdmin(admin.ModelAdmin):
    list_display = ["id", "wallet", "amount_sats", "bif_equivalent", "status", "created_at"]
    list_filter = ["status"]
    search_fields = ["id", "payment_hash", "wallet__user__username"]


@admin.register(AmatoPayCheckoutSession)
class AmatoPayCheckoutSessionAdmin(admin.ModelAdmin):
    list_display = ["session_id", "wallet", "payer_alias", "amount_bif", "status", "created_at"]
    list_filter = ["status"]
    search_fields = ["session_id", "payer_alias", "wallet__user__username"]
