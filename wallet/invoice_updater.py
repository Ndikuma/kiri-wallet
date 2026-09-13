"""
Utility: process inbound Blink invoice status updates and credit wallets.
Called both from the WebSocket subscriber and Celery retry tasks.
"""
import logging

from django.db import transaction
from django.utils import timezone

from wallet.models import (
    POSCharge,
    POSChargeStatus,
    TransactionCurrency,
    TransactionStatus,
    TransactionType,
    Wallet,
    WalletTransaction,
)

logger = logging.getLogger(__name__)

BLINK_INVOICE_TRANSACTION_TYPES = (TransactionType.DEPOSIT,)


@transaction.atomic
def process_blink_invoice_update(
    payment_hash: str,
    payment_request: str,
    status: str,
    settlement_amount: float | int | None = None,
) -> WalletTransaction | None:
    """
    Idempotently mark a pending Blink deposit invoice as settled.

    If the invoice was created as a POS charge, the settled sats are converted
    to BIF at the rate that was locked in when the charge was created, and the
    merchant's `bif_balance` is credited instead of `available_balance`.
    Otherwise, it's a plain wallet top-up and `available_balance` is credited.

    Returns the updated :class:`WalletTransaction` or ``None``.
    """
    qs = WalletTransaction.objects.select_for_update().filter(
        lnd_payment_hash=payment_hash,
        type=TransactionType.DEPOSIT,
        status=TransactionStatus.PENDING,
    ).select_related("user", "wallet")

    if not qs.exists():
        logger.info("No pending wallet transaction found for payment_hash %s; skipping", payment_hash)
        return None

    tx: WalletTransaction = qs.first()

    pos_charge = POSCharge.objects.select_for_update().filter(
        payment_hash=payment_hash,
        status=POSChargeStatus.PENDING,
    ).first()
    if pos_charge:
        return _confirm_pos_charge(tx, pos_charge, payment_request, settlement_amount)

    return _confirm_deposit_invoice(tx, payment_request, settlement_amount)


def _update_invoice_fields(tx: WalletTransaction, payment_request: str) -> list[str]:
    update_fields = ["status", "settled_at", "balance_after"]
    if payment_request:
        tx.lnd_invoice = payment_request
        update_fields.append("lnd_invoice")
    return update_fields


def _confirm_deposit_invoice(
    tx: WalletTransaction,
    payment_request: str,
    settlement_amount: float | int | None = None,
) -> WalletTransaction:
    wallet: Wallet = tx.wallet
    amount_sats = int(settlement_amount) if settlement_amount is not None else tx.amount
    pending_debit = min(wallet.pending_balance, tx.amount)

    wallet.pending_balance -= pending_debit
    wallet.available_balance += amount_sats
    wallet.total_deposited += amount_sats
    wallet.save(update_fields=["pending_balance", "available_balance", "total_deposited", "updated_at"])

    tx.status = TransactionStatus.CONFIRMED
    tx.settled_at = timezone.now()
    tx.balance_after = wallet.available_balance
    tx.save(update_fields=_update_invoice_fields(tx, payment_request))

    logger.info(
        "Wallet %s confirm_pending -%d sats / credit +%d sats (tx %s)",
        wallet.pk, pending_debit, amount_sats, tx.pk,
    )
    return tx


def _confirm_pos_charge(
    tx: WalletTransaction,
    pos_charge: POSCharge,
    payment_request: str,
    settlement_amount: float | int | None = None,
) -> WalletTransaction:
    wallet: Wallet = tx.wallet
    amount_sats = int(settlement_amount) if settlement_amount is not None else tx.amount
    pending_debit = min(wallet.pending_balance, tx.amount)

    wallet.pending_balance -= pending_debit
    wallet.bif_balance += pos_charge.bif_equivalent
    wallet.save(update_fields=["pending_balance", "bif_balance", "updated_at"])

    tx.status = TransactionStatus.CONFIRMED
    tx.settled_at = timezone.now()
    tx.balance_after = wallet.available_balance
    tx.description = tx.description or "POS charge (settled as BIF)"
    tx.linked_object_type = "wallet.POSCharge"
    tx.linked_object_id = str(pos_charge.pk)
    tx.save(update_fields=_update_invoice_fields(tx, payment_request) + ["description", "linked_object_type", "linked_object_id"])

    pos_charge.status = POSChargeStatus.PAID
    pos_charge.paid_at = timezone.now()
    pos_charge.save(update_fields=["status", "paid_at"])

    WalletTransaction.objects.create(
        user=wallet.user,
        wallet=wallet,
        type=TransactionType.POS_SETTLEMENT,
        currency=TransactionCurrency.BIF,
        amount=pos_charge.bif_equivalent,
        balance_after=wallet.bif_balance,
        status=TransactionStatus.CONFIRMED,
        settled_at=timezone.now(),
        linked_object_type="wallet.POSCharge",
        linked_object_id=str(pos_charge.pk),
        description=f"POS settlement for {amount_sats} sats @ {pos_charge.rate_bif_per_btc} BIF/BTC",
    )

    logger.info(
        "POS charge %s settled: wallet %s credited +%d BIF for %d sats",
        pos_charge.pk, wallet.pk, pos_charge.bif_equivalent, amount_sats,
    )
    return tx


@transaction.atomic
def process_blink_invoice_terminal_status(
    payment_hash: str,
    payment_request: str,
    status: str,
    transaction_status: str,
) -> WalletTransaction | None:
    """Move an unpaid Blink deposit invoice out of pending when Blink reports a terminal state (expired/failed)."""
    qs = WalletTransaction.objects.select_for_update().filter(
        lnd_payment_hash=payment_hash,
        type=TransactionType.DEPOSIT,
        status=TransactionStatus.PENDING,
    ).select_related("user", "wallet")

    if not qs.exists():
        logger.info("No pending wallet transaction found for terminal payment_hash %s; skipping", payment_hash)
        return None

    tx: WalletTransaction = qs.first()
    wallet: Wallet = tx.wallet
    pending_debit = min(wallet.pending_balance, tx.amount)

    if pending_debit:
        wallet.pending_balance -= pending_debit
        wallet.save(update_fields=["pending_balance", "updated_at"])

    tx.status = transaction_status
    tx.balance_after = wallet.available_balance
    update_fields = ["status", "balance_after"]
    if payment_request:
        tx.lnd_invoice = payment_request
        update_fields.append("lnd_invoice")
    tx.save(update_fields=update_fields)

    pos_charge = POSCharge.objects.select_for_update().filter(
        payment_hash=payment_hash, status=POSChargeStatus.PENDING,
    ).first()
    if pos_charge:
        pos_charge.status = POSChargeStatus.EXPIRED
        pos_charge.save(update_fields=["status"])

    logger.info(
        "Wallet %s released pending invoice %s as %s from Blink status %s (tx %s)",
        wallet.pk, pending_debit, transaction_status, status, tx.pk,
    )
    return tx
