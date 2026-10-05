from __future__ import annotations

from typing import Any

from django.conf import settings as django_settings
from django.db import models
from django.db.models import Count, Sum

from wallet.blink_wallet import BlinkWallet
from wallet.models import TransactionStatus, TransactionType, Wallet, WalletTransaction
from wallet.options import SETTINGS


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def get_blink_status() -> dict[str, Any]:
    if not SETTINGS.has_blink:
        return {
            "success": False,
            "configured": False,
            "balance_sats": 0,
            "wallet_id": "",
            "wallets": [],
            "btc_wallet": None,
            "message": "BLINK_API_KEY missing from settings.",
        }

    try:
        blink = BlinkWallet(api_url=SETTINGS.BLINK_API_URL, timeout=8)
        wallets = blink.get_wallets()
        btc_wallet = next((w for w in wallets if w.get("walletCurrency") == "BTC"), None)
        if btc_wallet:
            blink._wallet_id = btc_wallet.get("id")

        return {
            "success": True,
            "configured": True,
            "balance_sats": _to_int((btc_wallet or {}).get("balance")),
            "wallet_id": blink.wallet_id if btc_wallet else "",
            "wallets": wallets,
            "btc_wallet": btc_wallet,
            "message": "Blink wallet connected.",
        }
    except Exception as exc:
        return {
            "success": False,
            "configured": True,
            "balance_sats": 0,
            "wallet_id": "",
            "wallets": [],
            "btc_wallet": None,
            "message": str(exc),
        }


def _explorer_providers() -> list[dict[str, Any]]:
    from wallet.models import BlockExplorerProvider

    rows = BlockExplorerProvider.objects.filter(network=getattr(django_settings, "BITCOIN_NETWORK", ""))
    return [
        {
            "name": r.name, "api_url": r.api_url, "priority": r.priority, "active": r.is_active,
            "health": r.health, "latency_ms": r.last_latency_ms, "tip_height": r.last_tip_height,
            "consecutive_failures": r.consecutive_failures, "last_checked_at": r.last_checked_at,
            "message": r.last_check_message or r.last_error,
        }
        for r in rows
    ]


def get_onchain_status() -> dict[str, Any]:
    address_summary = Wallet.objects.aggregate(
        addresses=Count("id", filter=~models.Q(bitcoin_address="")),
    )
    deposit_summary = WalletTransaction.objects.filter(type=TransactionType.DEPOSIT).aggregate(
        confirmed_sats=Sum("amount", filter=models.Q(status=TransactionStatus.CONFIRMED)),
        pending_sats=Sum("amount", filter=models.Q(status=TransactionStatus.PENDING)),
        transactions=Count("id"),
    )

    provider_balance = 0
    provider_incomplete = False
    provider_message = ""
    provider_online = False
    try:
        from wallet.bitcoin import CustodialBitcoinService

        provider_balance, provider_incomplete = CustodialBitcoinService().get_platform_balance()
        provider_online = not provider_incomplete
        provider_message = (
            "On-chain wallet provider connected."
            if not provider_incomplete
            else "Some addresses could not be checked (explorer unreachable?) — balance may be an undercount."
        )
    except Exception as exc:
        provider_message = str(exc)

    try:
        from wallet.bitcoin import CustodialBitcoinService

        cached = CustodialBitcoinService().cached_balance()
    except Exception:  # noqa: BLE001 — status endpoint must never crash
        cached = {}

    return {
        "success": provider_online,
        "configured": True,
        "network": getattr(django_settings, "BITCOIN_NETWORK", ""),
        "providers": _explorer_providers(),
        "cached_balance": cached,
        "provider_balance_sats": provider_balance,
        "provider_balance_incomplete": provider_incomplete,
        "confirmed_deposits_sats": _to_int(deposit_summary["confirmed_sats"]),
        "pending_deposits_sats": _to_int(deposit_summary["pending_sats"]),
        "addresses": _to_int(address_summary["addresses"]),
        "transactions": _to_int(deposit_summary["transactions"]),
        "message": provider_message,
    }


def get_amatopay_status() -> dict[str, Any]:
    if not SETTINGS.has_amatopay:
        return {
            "success": False,
            "configured": False,
            "message": "AMATOPAY_API_KEY missing from settings.",
        }
    return {
        "success": True,
        "configured": True,
        "base_url": SETTINGS.AMATOPAY_BASE_URL,
        "message": "AmatoPay merchant key configured.",
    }
