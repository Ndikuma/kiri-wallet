"""
Management command: bitcoin_info — summary of the platform's on-chain wallet,
from the local database only (no explorer calls, safe to run any time).

    python manage.py bitcoin_info
"""
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Count, Sum

from wallet.bitcoin import CustodialBitcoinService, deposit_confirmations_required
from wallet.models import (
    BitcoinHDWallet,
    PlatformBitcoinAddress,
    TransactionStatus,
    TransactionType,
    WalletTransaction,
)


class Command(BaseCommand):
    help = "Show the platform's on-chain Bitcoin wallet state (network, addresses, cached coins, pending activity)."

    def handle(self, *args, **options):
        hd_wallet = BitcoinHDWallet.objects.first()
        self.stdout.write(f"Network:            {settings.BITCOIN_NETWORK}")
        if hd_wallet is None:
            self.stdout.write("HD wallet:          not created yet (created with the first address)")
            return
        self.stdout.write(f"HD wallet:          created {hd_wallet.created_at:%Y-%m-%d}, next index {hd_wallet.next_index}")
        self.stdout.write(f"New address type:   {settings.BITCOIN_ADDRESS_TYPE}")
        self.stdout.write(f"Deposit confs:      {deposit_confirmations_required()}")

        self.stdout.write("\nAddresses:")
        for row in PlatformBitcoinAddress.objects.values("purpose", "script_type").annotate(n=Count("id")).order_by("purpose"):
            self.stdout.write(f"  {row['purpose']:<8} {row['script_type']:<7} {row['n']}")

        cached = CustodialBitcoinService().cached_balance()
        self.stdout.write("\nCoins held (local cache, run scan_bitcoin to refresh):")
        self.stdout.write(f"  confirmed:   {cached['confirmed_sats']:>14,} sats in {cached['utxo_count']} coin(s)")
        self.stdout.write(f"  unconfirmed: {cached['unconfirmed_sats']:>14,} sats")
        self.stdout.write(f"  reserved:    {cached['reserved_sats']:>14,} sats (in-flight withdrawals)")

        pending = WalletTransaction.objects.filter(status=TransactionStatus.PENDING).exclude(onchain_txid="")
        deposits = pending.filter(type=TransactionType.DEPOSIT).aggregate(n=Count("id"), s=Sum("amount"))
        withdrawals = pending.filter(type=TransactionType.WITHDRAWAL).aggregate(n=Count("id"), s=Sum("amount"))
        self.stdout.write("\nPending:")
        self.stdout.write(f"  deposits:    {deposits['n']} ({deposits['s'] or 0:,} sats)")
        self.stdout.write(f"  withdrawals: {withdrawals['n']} ({withdrawals['s'] or 0:,} sats)")
