from django.contrib import admin
from unfold.admin import ModelAdmin
from unfold.decorators import display

from .models import (
    AmatoPayCheckoutSession,
    BitcoinHDWallet,
    ExchangeRate,
    PlatformBitcoinAddress,
    POSCharge,
    Wallet,
    WalletTransaction,
    WithdrawalFeePolicy,
)

_TRANSACTION_STATUS_LABELS = {
    "pending": "warning",
    "confirmed": "success",
    "failed": "danger",
    "expired": "danger",
    "refunded": "info",
}
_POS_CHARGE_STATUS_LABELS = {"pending": "warning", "paid": "success", "expired": "danger"}
_TOPUP_STATUS_LABELS = {"pending": "warning", "confirmed": "success", "failed": "danger", "expired": "danger"}


@admin.register(Wallet)
class WalletAdmin(ModelAdmin):
    list_display = ["user", "available_balance", "pending_balance", "bif_balance", "is_platform", "updated_at"]
    list_filter = ["is_platform"]
    search_fields = ["user__username", "user__email", "bitcoin_address"]
    readonly_fields = ["id", "created_at", "updated_at"]


@admin.register(WalletTransaction)
class WalletTransactionAdmin(ModelAdmin):
    list_display = ["id", "wallet", "display_type", "currency", "amount", "display_status", "created_at"]
    list_filter = ["type", "currency", "status"]
    search_fields = ["id", "lnd_payment_hash", "onchain_txid", "wallet__user__username"]
    readonly_fields = ["id", "created_at"]
    date_hierarchy = "created_at"

    @display(description="Type", ordering="type")
    def display_type(self, obj):
        return obj.get_type_display()

    @display(description="Status", label=_TRANSACTION_STATUS_LABELS, ordering="status")
    def display_status(self, obj):
        return obj.status


@admin.register(WithdrawalFeePolicy)
class WithdrawalFeePolicyAdmin(ModelAdmin):
    list_display = ["target_type", "display_label", "fixed_fee_sats", "percent_fee_bps", "charge_to_user", "is_active"]
    list_filter = ["is_active", "charge_to_user"]


@admin.register(ExchangeRate)
class ExchangeRateAdmin(ModelAdmin):
    list_display = ["bif_per_btc", "source", "is_active", "created_at"]
    list_filter = ["is_active", "source"]
    readonly_fields = ["id", "created_at"]


@admin.register(POSCharge)
class POSChargeAdmin(ModelAdmin):
    list_display = ["id", "wallet", "amount_sats", "bif_equivalent", "display_status", "created_at"]
    list_filter = ["status"]
    search_fields = ["id", "payment_hash", "wallet__user__username"]
    readonly_fields = ["id", "created_at"]
    date_hierarchy = "created_at"

    @display(description="Status", label=_POS_CHARGE_STATUS_LABELS, ordering="status")
    def display_status(self, obj):
        return obj.status


@admin.register(AmatoPayCheckoutSession)
class AmatoPayCheckoutSessionAdmin(ModelAdmin):
    list_display = ["session_id", "wallet", "payer_alias", "amount_bif", "display_status", "created_at"]
    list_filter = ["status"]
    search_fields = ["session_id", "payer_alias", "wallet__user__username"]
    readonly_fields = ["id", "created_at"]
    date_hierarchy = "created_at"

    @display(description="Status", label=_TOPUP_STATUS_LABELS, ordering="status")
    def display_status(self, obj):
        return obj.status


@admin.register(BitcoinHDWallet)
class BitcoinHDWalletAdmin(ModelAdmin):
    """Never exposes the encrypted root key; created/rotated only via wallet/onchain_keys.py."""

    list_display = ["network", "next_index", "created_at"]
    readonly_fields = ["id", "network", "next_index", "created_at"]
    fields = readonly_fields

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PlatformBitcoinAddress)
class PlatformBitcoinAddressAdmin(ModelAdmin):
    list_display = ["address", "derivation_index", "label", "created_at"]
    search_fields = ["address", "label"]
    readonly_fields = ["id", "address", "derivation_index", "label", "created_at"]

    def has_add_permission(self, request):
        return False
