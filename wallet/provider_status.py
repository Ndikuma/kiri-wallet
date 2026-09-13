from __future__ import annotations

from typing import Any

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
    provider_message = ""
    provider_online = False
    try:
        from wallet.bitcoin import CustodialBitcoinService

        provider_balance = CustodialBitcoinService().get_platform_balance()
        provider_online = True
        provider_message = "On-chain wallet provider connected."
    except Exception as exc:
        provider_message = str(exc)

    return {
        "success": provider_online,
        "configured": True,
        "provider_balance_sats": provider_balance,
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
