"""
Celery task runner for async Lightning operations (fallback polling for the WebSocket subscriber).
Requires: celery + redis (in requirements.txt).
"""
import logging

from celery import shared_task

from wallet.blink_wallet import BlinkWallet
from wallet.invoice_updater import (
    BLINK_INVOICE_TRANSACTION_TYPES,
    process_blink_invoice_terminal_status,
    process_blink_invoice_update,
)
from wallet.models import TransactionStatus, WalletTransaction

logger = logging.getLogger(__name__)
PAID_BLINK_STATUSES = {"PAID", "SETTLED", "SUCCESS", "CONFIRMED"}
PENDING_BLINK_STATUSES = {"PENDING", "UNPAID"}
EXPIRED_BLINK_STATUSES = {"EXPIRED"}
FAILED_BLINK_STATUSES = {"FAILED", "CANCELED", "CANCELLED"}
TERMINAL_BLINK_STATUSES = PAID_BLINK_STATUSES | EXPIRED_BLINK_STATUSES | FAILED_BLINK_STATUSES


@shared_task(bind=True, name="wallet.celery_tasks.poll_blink_invoice_update")
def poll_blink_invoice_update(
    self,
    payment_hash: str = "",
    payment_request: str = "",
    limit: int = 50,
) -> dict:
    """
    Poll Blink invoice status and update wallet transactions.

    With ``payment_hash`` or ``payment_request`` this checks one invoice.
    With no invoice arguments it scans all pending Blink deposit invoices
    (a fallback for the WebSocket subscriber missing an event).
    """
    wallet = BlinkWallet()

    if not payment_hash and not payment_request:
        return _poll_pending_blink_invoices(wallet, limit=limit)

    return _poll_one_invoice(wallet, payment_hash=payment_hash, payment_request=payment_request)


def _poll_pending_blink_invoices(wallet: BlinkWallet, *, limit: int = 50) -> dict:
    pending = (
        WalletTransaction.objects
        .filter(type__in=BLINK_INVOICE_TRANSACTION_TYPES, status=TransactionStatus.PENDING)
        .exclude(lnd_payment_hash="")
        .order_by("created_at")[:limit]
    )

    checked = 0
    pending_count = 0
    confirmed = 0
    expired = 0
    terminal = 0
    failed = 0
    results = []

    for tx in pending:
        checked += 1
        try:
            result = _poll_one_invoice(wallet, payment_hash=tx.lnd_payment_hash, payment_request=tx.lnd_invoice)
            if result.get("wallet_transaction_id"):
                if result.get("transaction_status") == TransactionStatus.CONFIRMED:
                    confirmed += 1
                elif result.get("transaction_status") == TransactionStatus.EXPIRED:
                    expired += 1
                elif result.get("transaction_status") == TransactionStatus.FAILED:
                    terminal += 1
            elif result.get("transaction_status") == TransactionStatus.PENDING:
                pending_count += 1
            results.append({
                "transaction_id": str(tx.id),
                "transaction_type": tx.type,
                "payment_hash": tx.lnd_payment_hash,
                "status": result.get("status", ""),
                "transaction_status": result.get("transaction_status", ""),
                "wallet_transaction_id": result.get("wallet_transaction_id"),
            })
        except Exception as exc:  # noqa: BLE001
            failed += 1
            logger.warning("Blink pending invoice poll failed for tx=%s: %s", tx.id, exc)
            results.append({
                "transaction_id": str(tx.id),
                "transaction_type": tx.type,
                "payment_hash": tx.lnd_payment_hash,
                "error": str(exc),
            })

    return {
        "status": "checked",
        "checked": checked,
        "pending": pending_count,
        "confirmed": confirmed,
        "expired": expired,
        "terminal": terminal,
        "failed": failed,
        "results": results,
    }


def _poll_one_invoice(
    wallet: BlinkWallet,
    *,
    payment_hash: str = "",
    payment_request: str = "",
) -> dict:
    try:
        result = wallet.get_ln_invoice_status(
            payment_hash=payment_hash or None,
            payment_request=payment_request or None,
        )
        status = str(result.get("status", "")).upper()
        processed = None
        resolved_hash = result.get("paymentHash") or payment_hash
        resolved_request = result.get("paymentRequest") or payment_request
        if status in TERMINAL_BLINK_STATUSES and not resolved_hash:
            logger.warning("Blink returned terminal status %s without paymentHash", status)
            result["transaction_status"] = ""
            return result
        if status in PAID_BLINK_STATUSES:
            processed = process_blink_invoice_update(
                payment_hash=resolved_hash, payment_request=resolved_request, status=status,
            )
        elif status in EXPIRED_BLINK_STATUSES:
            processed = process_blink_invoice_terminal_status(
                payment_hash=resolved_hash, payment_request=resolved_request,
                status=status, transaction_status=TransactionStatus.EXPIRED,
            )
        elif status in FAILED_BLINK_STATUSES:
            processed = process_blink_invoice_terminal_status(
                payment_hash=resolved_hash, payment_request=resolved_request,
                status=status, transaction_status=TransactionStatus.FAILED,
            )
        elif status in PENDING_BLINK_STATUSES:
            result["transaction_status"] = TransactionStatus.PENDING
        if processed:
            result["wallet_transaction_id"] = str(processed.id)
            result["transaction_status"] = processed.status
        return result
    except Exception as exc:
        logger.error("Blink poll error: %s", exc)
        raise
