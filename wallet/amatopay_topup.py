"""
BIF top-up via AmatoPay hosted checkout (mobile-money collection).

Flow: create a checkout session addressed to the payer's mobile alias ->
payer approves the payment request on their phone -> we poll the session
status -> once AmatoPay reports the payment collected, we credit the
wallet's `bif_balance` immediately (this credits our *internal* ledger;
AmatoPay's own merchant settlement/payout to our operating account follows
its normal fiduciary-hold timeline separately).
"""
from __future__ import annotations

from typing import Any

from django.db import transaction as db_transaction
from django.utils import timezone

from wallet.amatopay_client import AmatoPayClient, AmatoPayError
from wallet.models import (
    AmatoPayCheckoutSession,
    AmatoPayCheckoutSessionStatus,
    TransactionCurrency,
    TransactionStatus,
    TransactionType,
    Wallet,
    WalletTransaction,
)

CREDITABLE_PAYMENT_STATUSES = {
    "paid", "funds_held", "delivery_pending", "release_pending",
    "settlement_processing", "settled",
}
FAILED_PAYMENT_STATUSES = {
    "failed", "rejected", "cancelled", "reversed", "refunded", "expired", "fraud_blocked",
}


def create_topup_session(
    wallet: Wallet, *, amount_bif: int, payer_alias: str, return_url: str = "",
) -> AmatoPayCheckoutSession:
    if amount_bif <= 0:
        raise ValueError("amount_bif must be positive.")

    client = AmatoPayClient()
    response = client.create_checkout_session(
        amount_bif=amount_bif,
        payer_alias=payer_alias,
        description="Wallet BIF top-up",
        return_url=return_url,
        metadata={"wallet_id": str(wallet.pk)},
    )

    return AmatoPayCheckoutSession.objects.create(
        wallet=wallet,
        session_id=response["session_id"],
        payer_alias=payer_alias,
        amount_bif=amount_bif,
        checkout_url=response.get("checkout_url", ""),
    )


def check_topup_session(session: AmatoPayCheckoutSession) -> AmatoPayCheckoutSession:
    """Poll AmatoPay for this session's payment status; credit BIF once collected (idempotent)."""
    if session.status != AmatoPayCheckoutSessionStatus.PENDING:
        return session

    client = AmatoPayClient()
    try:
        data = client.get_checkout_status(session.session_id)
    except AmatoPayError:
        data = client.get_checkout_session(session.session_id)

    payment_status = str(data.get("payment_status") or data.get("status") or "").lower()

    if payment_status in CREDITABLE_PAYMENT_STATUSES:
        _credit_topup(session)
    elif payment_status in FAILED_PAYMENT_STATUSES:
        session.status = AmatoPayCheckoutSessionStatus.FAILED
        session.save(update_fields=["status"])

    return session


def _credit_topup(session: AmatoPayCheckoutSession) -> None:
    with db_transaction.atomic():
        locked_session = AmatoPayCheckoutSession.objects.select_for_update().get(pk=session.pk)
        if locked_session.status != AmatoPayCheckoutSessionStatus.PENDING:
            return

        wallet = Wallet.objects.select_for_update().get(pk=locked_session.wallet_id)
        wallet.bif_balance += locked_session.amount_bif
        wallet.save(update_fields=["bif_balance", "updated_at"])

        WalletTransaction.objects.create(
            user=wallet.user, wallet=wallet, type=TransactionType.BIF_TOPUP,
            currency=TransactionCurrency.BIF, amount=locked_session.amount_bif,
            balance_after=wallet.bif_balance, status=TransactionStatus.CONFIRMED,
            settled_at=timezone.now(),
            description=f"AmatoPay top-up {locked_session.session_id}",
            linked_object_type="wallet.AmatoPayCheckoutSession",
            linked_object_id=str(locked_session.pk),
        )

        locked_session.status = AmatoPayCheckoutSessionStatus.CONFIRMED
        locked_session.confirmed_at = timezone.now()
        locked_session.save(update_fields=["status", "confirmed_at"])

        session.status = locked_session.status
        session.confirmed_at = locked_session.confirmed_at
