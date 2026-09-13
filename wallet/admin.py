from django.contrib import admin

from .models import Transaction, Wallet


@admin.register(Wallet)
class WalletAdmin(admin.ModelAdmin):
    list_display = ["user", "available_balance", "pending_balance", "updated_at"]
    search_fields = ["user__username", "user__email", "bitcoin_address", "lightning_address"]


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ["id", "wallet", "type", "status", "amount_sats", "created_at"]
    list_filter = ["type", "status"]
    search_fields = ["id", "payment_hash", "onchain_txid", "wallet__user__username"]
