from django.contrib import admin
from django.utils.html import format_html
from unfold.admin import ModelAdmin
from unfold.decorators import display

from .esplora_client import explorer_web_base
from .models import (
    AmatoPayCheckoutSession,
    AmatoPayWebhookEvent,
    BitcoinHDWallet,
    BitcoinUTXO,
    BlockExplorerProvider,
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


def _explorer_link(kind: str, value: str) -> str:
    """Renders `value` (a txid or address) as a link to a block explorer page for the
    configured BITCOIN_NETWORK, or as plain text if no public explorer is configured
    for it (e.g. regtest) or there's nothing to link."""
    if not value:
        return "—"
    base = explorer_web_base()
    if not base:
        return value
    return format_html('<a href="{}/{}/{}" target="_blank" rel="noopener noreferrer">{}</a>', base, kind, value, value)


@admin.register(Wallet)
class WalletAdmin(ModelAdmin):
    list_display = [
        "user", "available_balance", "pending_balance", "bif_balance", "display_bitcoin_address",
        "is_platform", "updated_at",
    ]
    list_filter = ["is_platform"]
    search_fields = ["user__username", "user__email", "bitcoin_address"]
    readonly_fields = ["id", "created_at", "updated_at"]

    @display(description="Bitcoin Address")
    def display_bitcoin_address(self, obj):
        return _explorer_link("address", obj.bitcoin_address)


@admin.register(WalletTransaction)
class WalletTransactionAdmin(ModelAdmin):
    list_display = [
        "id", "wallet", "display_type", "currency", "amount", "display_status", "display_onchain_txid",
        "created_at",
    ]
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

    @display(description="On-chain TXID", ordering="onchain_txid")
    def display_onchain_txid(self, obj):
        return _explorer_link("tx", obj.onchain_txid)

    actions = ["refund_stuck_onchain_withdrawal"]

    @admin.action(description="Refund stuck on-chain withdrawal (only if not on the network)")
    def refund_stuck_onchain_withdrawal(self, request, queryset):
        from wallet.bitcoin import CustodialBitcoinService
        from wallet.esplora_client import EsploraError, EsploraNotFound, get_tx_status

        service = CustodialBitcoinService()
        candidates = queryset.filter(type="withdrawal", status="pending").exclude(onchain_txid="")
        for withdrawal in candidates:
            try:
                get_tx_status(withdrawal.onchain_txid)
            except EsploraNotFound:
                if service.refund_withdrawal(withdrawal, "refunded by an admin: transaction not on the network"):
                    self.message_user(request, f"Refunded {withdrawal.amount} sats for {withdrawal.onchain_txid}.")
                continue
            except EsploraError as exc:
                self.message_user(request, f"Could not check {withdrawal.onchain_txid}: {exc}", level="error")
                continue
            self.message_user(
                request, f"{withdrawal.onchain_txid} is on the network; it will confirm, so it was not refunded.",
                level="warning",
            )
        skipped = queryset.count() - candidates.count()
        if skipped:
            self.message_user(request, f"Skipped {skipped} row(s) that aren't pending on-chain withdrawals.", level="warning")


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
    list_display = ["id", "wallet", "display_charge_type", "amount_sats", "bif_equivalent", "display_status", "created_at"]
    list_filter = ["status", "charge_type"]
    search_fields = ["id", "payment_hash", "payer_alias", "wallet__user__username"]
    readonly_fields = ["id", "created_at"]
    date_hierarchy = "created_at"

    @display(description="Type", ordering="charge_type")
    def display_charge_type(self, obj):
        return obj.get_charge_type_display()

    @display(description="Status", label=_POS_CHARGE_STATUS_LABELS, ordering="status")
    def display_status(self, obj):
        return obj.status


@admin.register(AmatoPayCheckoutSession)
class AmatoPayCheckoutSessionAdmin(ModelAdmin):
    list_display = [
        "session_id", "wallet", "payer_alias", "amount_bif", "display_status",
        "payment_status", "display_delivery", "created_at",
    ]
    list_filter = ["status"]
    search_fields = ["session_id", "payment_reference", "order_number", "payer_alias", "wallet__user__username"]
    readonly_fields = ["id", "created_at"]
    date_hierarchy = "created_at"

    @display(description="Status", label=_TOPUP_STATUS_LABELS, ordering="status")
    def display_status(self, obj):
        return obj.status

    @display(description="Delivery", boolean=True)
    def display_delivery(self, obj):
        return bool(obj.delivery_confirmed_at)


@admin.register(AmatoPayWebhookEvent)
class AmatoPayWebhookEventAdmin(ModelAdmin):
    list_display = ["id", "event_type", "payment_reference", "processed", "received_at"]
    list_filter = ["event_type"]
    search_fields = ["id", "payment_reference"]
    readonly_fields = ["id", "event_type", "payment_reference", "payload", "received_at", "processed_at", "error"]
    date_hierarchy = "received_at"

    @display(description="Processed", boolean=True)
    def processed(self, obj):
        return bool(obj.processed_at) and not obj.error

    def has_add_permission(self, request):
        return False


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
    list_display = ["display_address", "script_type", "purpose", "wallet", "derivation_path", "created_at"]
    list_filter = ["script_type", "purpose"]
    search_fields = ["address", "label", "wallet__user__username"]
    readonly_fields = [
        "id", "address", "derivation_index", "derivation_path", "script_type", "purpose", "wallet", "label", "created_at",
    ]

    def has_add_permission(self, request):
        return False

    @display(description="Address", ordering="address")
    def display_address(self, obj):
        return _explorer_link("address", obj.address)


@admin.register(BitcoinUTXO)
class BitcoinUTXOAdmin(ModelAdmin):
    """The platform's on-chain coins, as cached by the scanner. Read-only."""

    list_display = ["display_outpoint", "value", "status", "block_height", "display_address", "first_seen_at"]
    list_filter = ["status"]
    search_fields = ["txid", "address__address", "spent_by_txid"]
    readonly_fields = [
        "id", "address", "txid", "vout", "value", "block_height", "status", "spent_by_txid",
        "reserved_by", "first_seen_at", "updated_at",
    ]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @display(description="Outpoint", ordering="txid")
    def display_outpoint(self, obj):
        return format_html("{}:{}", _explorer_link("tx", obj.txid), obj.vout)

    @display(description="Address")
    def display_address(self, obj):
        return _explorer_link("address", obj.address.address)


_PROVIDER_HEALTH_LABELS = {"healthy": "success", "degraded": "warning", "down": "danger", "unchecked": "info"}


@admin.register(BlockExplorerProvider)
class BlockExplorerProviderAdmin(ModelAdmin):
    """Block explorer APIs used for on-chain data and broadcast. Active providers for
    the configured BITCOIN_NETWORK are tried in priority order (lowest first)."""

    list_display = [
        "name", "network", "api_url", "priority", "is_active", "display_health", "last_latency_ms",
        "last_tip_height", "consecutive_failures", "last_checked_at",
    ]
    list_editable = ["priority", "is_active"]
    list_filter = ["network", "is_active"]
    search_fields = ["name", "api_url"]
    actions = ["check_now", "activate", "deactivate"]
    readonly_fields = [
        "display_health", "last_checked_at", "last_check_ok", "last_check_message", "last_latency_ms",
        "last_tip_height", "last_success_at", "last_failure_at", "last_error", "consecutive_failures",
        "total_requests", "total_failures", "created_at", "updated_at",
    ]
    fieldsets = [
        (None, {"fields": ["name", "network", "api_url", "web_url", "priority", "is_active", "timeout_seconds"]}),
        ("Authentication (private or paid instances only)", {"fields": ["auth_header", "auth_value"]}),
        ("Health", {"fields": readonly_fields}),
    ]

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        if "auth_value" in form.base_fields:
            from django import forms

            # Don't echo a stored secret back into the page.
            form.base_fields["auth_value"].widget = forms.PasswordInput(render_value=False)
            form.base_fields["auth_value"].required = False
            form.base_fields["auth_value"].help_text = "Leave empty to keep the current value."
        return form

    def save_model(self, request, obj, form, change):
        if change and not form.cleaned_data.get("auth_value"):
            obj.auth_value = BlockExplorerProvider.objects.get(pk=obj.pk).auth_value
        super().save_model(request, obj, form, change)
        if not change:
            self._check(request, [obj])

    @display(description="Health", label=_PROVIDER_HEALTH_LABELS)
    def display_health(self, obj):
        return obj.health

    def _check(self, request, rows):
        from wallet.esplora_client import check_providers

        for result in check_providers(rows):
            provider = result["provider"]
            level = "success" if result["ok"] else "error"
            self.message_user(request, f"{provider}: {result['message'] or 'failed'}", level=level)

    @admin.action(description="Check now (reachability, chain, tip, fees)")
    def check_now(self, request, queryset):
        self._check(request, queryset)

    @admin.action(description="Enable selected providers")
    def activate(self, request, queryset):
        self.message_user(request, f"Enabled {queryset.update(is_active=True)} provider(s).")

    @admin.action(description="Disable selected providers")
    def deactivate(self, request, queryset):
        self.message_user(request, f"Disabled {queryset.update(is_active=False)} provider(s).")
