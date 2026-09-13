"""Sidebar badge counts for the Unfold admin nav — each runs on every admin page load, so keep them cheap (counts only, no joins)."""

from wallet.models import (
    AmatoPayCheckoutSession,
    AmatoPayCheckoutSessionStatus,
    POSCharge,
    POSChargeStatus,
    TransactionStatus,
    TransactionType,
    WalletTransaction,
)
from wallet.options import SETTINGS


def _badge(count):
    return str(count) if count else None


def pending_deposits(request):
    return _badge(
        WalletTransaction.objects.filter(
            type=TransactionType.DEPOSIT, status=TransactionStatus.PENDING
        ).count()
    )


def pending_pos_charges(request):
    return _badge(POSCharge.objects.filter(status=POSChargeStatus.PENDING).count())


def pending_topups(request):
    return _badge(
        AmatoPayCheckoutSession.objects.filter(
            status=AmatoPayCheckoutSessionStatus.PENDING
        ).count()
    )


def lightning_status(request):
    return "Live" if SETTINGS.has_blink else "Setup"


def amatopay_status(request):
    return "Live" if SETTINGS.has_amatopay else "Setup"
