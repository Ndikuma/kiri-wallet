"""
BIF (Burundian Franc) exchange + point-of-sale logic.

Two related but distinct features:
  - Exchange: convert between the user's own BTC (sats) and BIF ledger
    balances, at the current admin-configured ExchangeRate. Pure internal
    bookkeeping, no external calls.
  - POS charge: a merchant quotes a Lightning invoice for `amount_sats`,
    locking in the BIF equivalent at creation time. When the Blink invoice
    is paid, `wallet/invoice_updater.py` credits `bif_balance` (not
    `available_balance`) so the merchant is insulated from BTC price moves
    between charge and settlement.
"""
from __future__ import annotations

from typing import Any

from django.db import transaction as db_transaction

from wallet.blink_wallet import BlinkWallet, BlinkWalletError
from wallet.models import (
    ExchangeRate,
    POSCharge,
    TransactionCurrency,
    TransactionStatus,
    TransactionType,
    Wallet,
    WalletTransaction,
)
from wallet.options import SETTINGS


def get_quote(*, amount_sats: int | None = None, amount_bif: int | None = None) -> dict[str, Any]:
    if not amount_sats and not amount_bif:
        raise ValueError("Provide amount_sats or amount_bif.")
    rate = ExchangeRate.current()
    if amount_sats:
        bif = rate.sats_to_bif(amount_sats)
        return {
            "rate_id": str(rate.pk), "bif_per_btc": str(rate.bif_per_btc),
            "amount_sats": int(amount_sats), "amount_bif": bif,
        }
    sats = rate.bif_to_sats(amount_bif)
    return {
        "rate_id": str(rate.pk), "bif_per_btc": str(rate.bif_per_btc),
        "amount_sats": sats, "amount_bif": int(amount_bif),
    }


def convert_sats_to_bif(wallet: Wallet, amount_sats: int) -> dict[str, Any]:
    if amount_sats <= 0:
        raise ValueError("amount_sats must be positive.")
    rate = ExchangeRate.current()
    bif_amount = rate.sats_to_bif(amount_sats)

    with db_transaction.atomic():
        locked = Wallet.objects.select_for_update().get(pk=wallet.pk)
        if locked.available_balance < amount_sats:
            raise ValueError(f"Insufficient BTC balance. Available: {locked.available_balance} sats.")

        locked.available_balance -= amount_sats
        locked.bif_balance += bif_amount
        locked.save(update_fields=["available_balance", "bif_balance", "updated_at"])

        WalletTransaction.objects.create(
            user=locked.user, wallet=locked, type=TransactionType.EXCHANGE_SATS_TO_BIF,
            currency=TransactionCurrency.SATS, amount=amount_sats, balance_after=locked.available_balance,
            status=TransactionStatus.CONFIRMED, description=f"Exchanged for {bif_amount} BIF @ {rate.bif_per_btc} BIF/BTC",
        )
        WalletTransaction.objects.create(
            user=locked.user, wallet=locked, type=TransactionType.EXCHANGE_SATS_TO_BIF,
            currency=TransactionCurrency.BIF, amount=bif_amount, balance_after=locked.bif_balance,
            status=TransactionStatus.CONFIRMED, description=f"Exchanged from {amount_sats} sats @ {rate.bif_per_btc} BIF/BTC",
        )

    return {
        "amount_sats": amount_sats, "amount_bif": bif_amount, "rate_bif_per_btc": str(rate.bif_per_btc),
        "available_balance": locked.available_balance, "bif_balance": locked.bif_balance,
    }


def convert_bif_to_sats(wallet: Wallet, amount_bif: int) -> dict[str, Any]:
    if amount_bif <= 0:
        raise ValueError("amount_bif must be positive.")
    rate = ExchangeRate.current()
    sats_amount = rate.bif_to_sats(amount_bif)

    with db_transaction.atomic():
        locked = Wallet.objects.select_for_update().get(pk=wallet.pk)
        if locked.bif_balance < amount_bif:
            raise ValueError(f"Insufficient BIF balance. Available: {locked.bif_balance} BIF.")

        locked.bif_balance -= amount_bif
        locked.available_balance += sats_amount
        locked.save(update_fields=["available_balance", "bif_balance", "updated_at"])

        WalletTransaction.objects.create(
            user=locked.user, wallet=locked, type=TransactionType.EXCHANGE_BIF_TO_SATS,
            currency=TransactionCurrency.BIF, amount=amount_bif, balance_after=locked.bif_balance,
            status=TransactionStatus.CONFIRMED, description=f"Exchanged for {sats_amount} sats @ {rate.bif_per_btc} BIF/BTC",
        )
        WalletTransaction.objects.create(
            user=locked.user, wallet=locked, type=TransactionType.EXCHANGE_BIF_TO_SATS,
            currency=TransactionCurrency.SATS, amount=sats_amount, balance_after=locked.available_balance,
            status=TransactionStatus.CONFIRMED, description=f"Exchanged from {amount_bif} BIF @ {rate.bif_per_btc} BIF/BTC",
        )

    return {
        "amount_sats": sats_amount, "amount_bif": amount_bif, "rate_bif_per_btc": str(rate.bif_per_btc),
        "available_balance": locked.available_balance, "bif_balance": locked.bif_balance,
    }


def create_pos_charge(wallet: Wallet, *, amount_sats: int, memo: str = "") -> tuple[POSCharge, dict[str, Any]]:
    """Quote a Lightning invoice for `amount_sats`; on payment, credit the BIF equivalent (locked in now)."""
    if amount_sats < SETTINGS.MIN_DEPOSIT:
        raise ValueError(f"Minimum POS charge is {SETTINGS.MIN_DEPOSIT} sats.")

    rate = ExchangeRate.current()
    bif_equivalent = rate.sats_to_bif(amount_sats)

    try:
        blink = BlinkWallet()
        invoice = blink.create_ln_invoice(amount_sats, memo=memo or "POS charge")
    except BlinkWalletError:
        raise
    except Exception as exc:
        raise BlinkWalletError(str(exc)) from exc

    pos_charge = POSCharge.objects.create(
        wallet=wallet,
        amount_sats=amount_sats,
        bif_equivalent=bif_equivalent,
        rate_bif_per_btc=rate.bif_per_btc,
        payment_hash=invoice.get("paymentHash", ""),
        payment_request=invoice.get("paymentRequest", ""),
        memo=memo,
    )

    wallet.add_pending_balance(amount_sats)
    wallet.settle(
        amount_sats,
        TransactionType.DEPOSIT,
        lnd_invoice=invoice.get("paymentRequest", ""),
        lnd_payment_hash=invoice.get("paymentHash", ""),
        status=TransactionStatus.PENDING,
        description=memo or "POS charge",
        balance_after=wallet.available_balance,
        linked_object_type="wallet.POSCharge",
        linked_object_id=str(pos_charge.pk),
    )

    return pos_charge, invoice
