"""
BIF top-up via AmatoPay hosted checkout (mobile-money collection, push-to-alias only —
AmatoPay's QR/scan-to-pay mechanism is not used here).

Flow: verify the payer's alias -> create a checkout session addressed to it -> redirect
the payer's browser to the returned `checkout_url` -> payer approves the payment request
on their phone -> AmatoPay redirects the browser back to our `return_url` and, in
parallel, notifies us via webhook (`wallet/amatopay_webhooks.py`) — either path calls
back into `check_topup_session` / `process_webhook_event` below, which credit the
wallet's `bif_balance` once AmatoPay reports the payment collected (this credits our
*internal* ledger; AmatoPay's own merchant settlement/payout to our operating account
follows its normal fiduciary-hold timeline separately).

The same checkout session is also reused as the collection leg of a BIF_TO_SATS POS
charge (`wallet.bif.create_pos_charge_bif_to_sats`) — when a session is linked to one
(`POSCharge.amatopay_session`), `_credit_topup` credits that charge's locked-in sats
instead of crediting plain BIF top-up.
"""
from __future__ import annotations

import uuid
from typing import Any

from django.db import transaction as db_transaction
from django.utils import timezone

from wallet.amatopay_client import AmatoPayClient, AmatoPayError
from wallet.models import (
    AmatoPayCheckoutSession,
    AmatoPayCheckoutSessionStatus,
    AmatoPayWebhookEvent,
    POSCharge,
    POSChargeStatus,
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


def verify_payer_alias(payer_alias: str) -> dict[str, Any]:
    """Step 1: confirm the payer's mobile alias is active and payable before creating a
    checkout session, and return its resolved display name for a confirmation prompt.
    Raises AmatoPayError if the alias isn't payable (not found, inactive, wrong type)."""
    return AmatoPayClient().verify_alias(payer_alias)


def create_topup_session(
    wallet: Wallet, *, amount_bif: int, payer_alias: str, return_url: str = "",
) -> AmatoPayCheckoutSession:
    """Steps 1-2: verify the payer's alias, then create the checkout session (step 3 —
    redirecting the payer to `checkout_url` — is the caller's job)."""
    if amount_bif <= 0:
        raise ValueError("amount_bif must be positive.")

    client = AmatoPayClient()
    verification = client.verify_alias(payer_alias)

    order_number = f"TOPUP-{uuid.uuid4().hex[:16]}"
    response = client.create_checkout_session(
        amount_bif=amount_bif,
        payer_alias=payer_alias,
        description="Wallet BIF top-up",
        return_url=return_url,
        order_number=order_number,
        metadata={"wallet_id": str(wallet.pk)},
    )

    return AmatoPayCheckoutSession.objects.create(
        wallet=wallet,
        session_id=response["session_id"],
        order_number=order_number,
        payment_reference=response.get("payment_reference", ""),
        payer_alias=payer_alias,
        payer_display_name=response.get("payer_display_name") or verification.get("customer_full_name", ""),
        amount_bif=amount_bif,
        checkout_url=response.get("checkout_url", ""),
    )


def check_topup_session(session: AmatoPayCheckoutSession) -> AmatoPayCheckoutSession:
    """Poll AmatoPay for this session's payment status; credit BIF once collected (idempotent).
    Used to drive UI polling and to reconcile the browser's return from checkout — the
    authoritative outcome always comes from this call or from a webhook, never from the
    redirect itself. Keeps polling past our own "confirmed" (internal ledger credited)
    while AmatoPay's side is still awaiting delivery confirmation, so `payment_status`
    stays current for `awaiting_delivery_confirmation`."""
    if session.status in (AmatoPayCheckoutSessionStatus.FAILED, AmatoPayCheckoutSessionStatus.EXPIRED):
        return session
    if session.status == AmatoPayCheckoutSessionStatus.CONFIRMED and session.delivery_confirmed_at:
        return session

    client = AmatoPayClient()
    try:
        data = client.get_checkout_status(session.session_id)
    except AmatoPayError:
        data = client.get_checkout_session(session.session_id)

    payment_status = str(data.get("payment_status") or data.get("status") or "").lower()
    return _apply_payment_status(session, payment_status)


def process_webhook_event(*, event_id: str, event_type: str, data: dict[str, Any], raw_payload: dict[str, Any]) -> AmatoPayWebhookEvent:
    """Durably record a webhook delivery and apply it, idempotently keyed on AmatoPay's
    event `id` — a retried/replayed delivery of an id we've already stored is a no-op."""
    payment_reference = str(data.get("payment_reference") or "")

    event, created = AmatoPayWebhookEvent.objects.get_or_create(
        id=event_id,
        defaults={"event_type": event_type, "payment_reference": payment_reference, "payload": raw_payload},
    )
    if not created:
        return event

    try:
        session = AmatoPayCheckoutSession.objects.filter(payment_reference=payment_reference).first() if payment_reference else None
        if session:
            # AmatoPay's own `payment.paid` delivery already carries the *post-hold*
            # status (e.g. "delivery_pending") when the session requires delivery
            # confirmation — trust `data.status` over the event name.
            payment_status = str(data.get("status") or "").lower()
            if not payment_status and event_type == "payment.failed":
                payment_status = "failed"
            _apply_payment_status(session, payment_status)

            # `delivery.confirmed` fires however delivery got confirmed — our own
            # confirm-delivery call, the payer's public delivery page, or instant
            # settlement's auto-confirm — so mark it even if we weren't the ones
            # who submitted the code.
            if event_type == "delivery.confirmed" and not session.delivery_confirmed_at:
                session.delivery_confirmed_at = timezone.now()
                session.save(update_fields=["delivery_confirmed_at"])
    except Exception as exc:  # noqa: BLE001 — never let a processing bug break webhook durability
        event.error = str(exc)[:2000]
        event.save(update_fields=["error"])
    else:
        event.processed_at = timezone.now()
        event.save(update_fields=["processed_at"])

    return event


def _apply_payment_status(session: AmatoPayCheckoutSession, payment_status: str) -> AmatoPayCheckoutSession:
    if payment_status and payment_status != session.payment_status:
        session.payment_status = payment_status
        AmatoPayCheckoutSession.objects.filter(pk=session.pk).update(payment_status=payment_status)

    if payment_status in CREDITABLE_PAYMENT_STATUSES:
        _credit_topup(session)
    elif payment_status in FAILED_PAYMENT_STATUSES:
        _fail_session(session)
    return session


def confirm_delivery(session: AmatoPayCheckoutSession, secure_code: str) -> AmatoPayCheckoutSession:
    """Merchant-submitted step: the payer reads AmatoPay's six-digit release code off
    their own phone and gives it to whoever is running this checkout, who enters it
    here so AmatoPay releases the held funds to our settlement account. This is
    separate from — and doesn't replace — crediting the wallet's internal ledger,
    which already happened in `_credit_topup` once the payment was collected; this
    step only concerns AmatoPay's own payout of real money to us."""
    if not session.payment_reference:
        raise AmatoPayError("This session has no AmatoPay payment reference yet.")
    if session.delivery_confirmed_at:
        return session

    response = AmatoPayClient().confirm_delivery(session.payment_reference, secure_code)

    session.delivery_confirmed_at = timezone.now()
    payment_status = str(response.get("status") or "").lower()
    if payment_status:
        session.payment_status = payment_status
    session.save(update_fields=["delivery_confirmed_at", "payment_status"])
    return session


def _fail_session(session: AmatoPayCheckoutSession) -> None:
    with db_transaction.atomic():
        locked = AmatoPayCheckoutSession.objects.select_for_update().get(pk=session.pk)
        if locked.status != AmatoPayCheckoutSessionStatus.PENDING:
            return
        locked.status = AmatoPayCheckoutSessionStatus.FAILED
        locked.save(update_fields=["status"])
    session.status = AmatoPayCheckoutSessionStatus.FAILED


def _credit_topup(session: AmatoPayCheckoutSession) -> None:
    with db_transaction.atomic():
        locked_session = AmatoPayCheckoutSession.objects.select_for_update().get(pk=session.pk)
        if locked_session.status != AmatoPayCheckoutSessionStatus.PENDING:
            return

        wallet = Wallet.objects.select_for_update().get(pk=locked_session.wallet_id)

        pos_charge = POSCharge.objects.select_for_update().filter(
            amatopay_session_id=locked_session.pk, status=POSChargeStatus.PENDING,
        ).first()
        if pos_charge:
            from wallet.bif import settle_pos_charge_from_bif_payment
            settle_pos_charge_from_bif_payment(wallet, pos_charge)
        else:
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
