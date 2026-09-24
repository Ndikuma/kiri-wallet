"""
BIF (Burundian Franc) exchange + point-of-sale logic.

Two related but distinct features:
  - Exchange: convert the user's own sats to BIF (one-directional — sats to
    BIF only, no BIF back to sats) at the current admin-configured
    ExchangeRate. Pure internal bookkeeping, no external calls.
  - POS charge, in either direction:
      - SATS_TO_BIF: a merchant quotes a Lightning invoice for `amount_sats`,
        locking in the BIF equivalent at creation time. When the Blink invoice
        is paid, `wallet/invoice_updater.py` credits `bif_balance` (not
        `available_balance`) so the merchant is insulated from BTC price moves
        between charge and settlement.
      - BIF_TO_SATS: a merchant quotes a charge in BIF, collected from the
        payer via an AmatoPay mobile-money checkout (the same collection
        mechanism `wallet/amatopay_topup.py` uses for BIF top-ups). Once
        collected, `settle_pos_charge_from_bif_payment` below (called from
        `amatopay_topup._credit_topup`) credits `available_balance` with the
        sats equivalent locked in at creation time.
"""
from __future__ import annotations

from typing import Any

from django.db import transaction as db_transaction
from django.utils import timezone

from wallet.blink_wallet import BlinkWallet, BlinkWalletError
from wallet.models import (
    ExchangeRate,
    POSCharge,
    POSChargeStatus,
    POSChargeType,
    TransactionCurrency,
    TransactionStatus,
    TransactionType,
    Wallet,
    WalletTransaction,
)
from wallet.options import SETTINGS


def get_quote(*, amount_sats: int) -> dict[str, Any]:
    if not amount_sats or amount_sats <= 0:
        raise ValueError("Provide a positive amount_sats.")
    rate = ExchangeRate.current()
    bif = rate.sats_to_bif(amount_sats)
    return {
        "rate_id": str(rate.pk), "bif_per_btc": str(rate.bif_per_btc),
        "amount_sats": int(amount_sats), "amount_bif": bif,
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


def create_pos_charge_bif_to_sats(
    wallet: Wallet, *, amount_bif: int, payer_alias: str, memo: str = "", return_url: str = "",
) -> tuple[POSCharge, Any]:
    """Quote a charge in BIF, collected from the payer via an AmatoPay mobile-money
    checkout; on collection, credit the sats equivalent (locked in now) to `available_balance`."""
    if amount_bif <= 0:
        raise ValueError("amount_bif must be positive.")

    rate = ExchangeRate.current()
    sats_equivalent = rate.bif_to_sats(amount_bif)

    from wallet.amatopay_topup import create_topup_session
    session = create_topup_session(wallet, amount_bif=amount_bif, payer_alias=payer_alias, return_url=return_url)

    pos_charge = POSCharge.objects.create(
        wallet=wallet,
        charge_type=POSChargeType.BIF_TO_SATS,
        amount_sats=sats_equivalent,
        bif_equivalent=amount_bif,
        rate_bif_per_btc=rate.bif_per_btc,
        payer_alias=payer_alias,
        amatopay_session=session,
        memo=memo,
    )

    return pos_charge, session


def settle_pos_charge_from_bif_payment(wallet: Wallet, pos_charge: POSCharge) -> None:
    """Credit a BIF_TO_SATS POS charge's locked-in sats to `available_balance` once its
    AmatoPay mobile-money collection is confirmed. Caller (`amatopay_topup._credit_topup`)
    already holds row locks on both `wallet` and the linked AmatoPayCheckoutSession."""
    wallet.available_balance += pos_charge.amount_sats
    wallet.save(update_fields=["available_balance", "updated_at"])

    pos_charge.status = POSChargeStatus.PAID
    pos_charge.paid_at = timezone.now()
    pos_charge.save(update_fields=["status", "paid_at"])

    WalletTransaction.objects.create(
        user=wallet.user, wallet=wallet, type=TransactionType.POS_SETTLEMENT,
        currency=TransactionCurrency.SATS, amount=pos_charge.amount_sats,
        balance_after=wallet.available_balance, status=TransactionStatus.CONFIRMED,
        settled_at=timezone.now(), linked_object_type="wallet.POSCharge",
        linked_object_id=str(pos_charge.pk),
        description=f"POS settlement for {pos_charge.bif_equivalent} BIF @ {pos_charge.rate_bif_per_btc} BIF/BTC",
    )
