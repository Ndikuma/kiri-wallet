"""
Management command: scan_bitcoin — scan every on-chain deposit address for new
UTXOs and credit wallets accordingly (pending until BITCOIN_DEPOSIT_CONFIRMATIONS,
then spendable), and advance pending on-chain withdrawals.

This is the on-chain counterpart to `blink_ws` (which does the same job for
Lightning, but by subscription instead of polling — there is no equivalent
push mechanism for on-chain deposits, so this has to poll an explorer).

    python manage.py scan_bitcoin                          # one pass
    python manage.py scan_bitcoin --watch                  # loop forever
    python manage.py scan_bitcoin --watch --interval 30     # custom interval
"""

import logging
import time

from django.core.management.base import BaseCommand

from wallet.bitcoin import CustodialBitcoinService

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Scan every on-chain Bitcoin deposit address for new deposits and credit wallets."

    def add_arguments(self, parser):
        parser.add_argument(
            "--watch", action="store_true",
            help="Keep running, rescanning every --interval seconds, instead of a single pass.",
        )
        parser.add_argument(
            "--interval", type=int, default=60,
            help="Seconds between scans when --watch is set (default: 60).",
        )

    def handle(self, *args, **options):
        if not options["watch"]:
            self._scan_once()
            return

        interval = options["interval"]
        self.stdout.write(self.style.SUCCESS(f"Watching on-chain deposits every {interval}s (Ctrl+C to stop)."))
        try:
            while True:
                self._scan_once()
                time.sleep(interval)
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING("\nStopped."))

    def _scan_once(self):
        service = CustodialBitcoinService()
        try:
            result = service.scan_all_users()
        except Exception as exc:  # noqa: BLE001
            logger.exception("On-chain scan failed: %s", exc)
            self.stderr.write(self.style.ERROR(f"Scan failed: {exc}"))
            return

        processed = result["processed"]
        promoted = result.get("promoted", [])
        dropped = result.get("dropped", [])
        withdrawals = result.get("withdrawals", {})
        failed = result["failed"]

        if processed:
            logger.info("On-chain scan found %d new deposit(s): %s", len(processed), processed)
            self.stdout.write(self.style.SUCCESS(f"Found {len(processed)} new deposit(s)."))
        if promoted:
            logger.info("On-chain scan confirmed %d deposit(s): %s", len(promoted), promoted)
            self.stdout.write(self.style.SUCCESS(f"Confirmed {len(promoted)} pending deposit(s)."))
        if dropped:
            self.stdout.write(self.style.WARNING(f"{len(dropped)} unconfirmed deposit(s) dropped from the mempool: {dropped}"))
        for key, label in (("confirmed", "withdrawal(s) confirmed"), ("rebroadcast", "withdrawal(s) rebroadcast")):
            if withdrawals.get(key):
                self.stdout.write(self.style.SUCCESS(f"{len(withdrawals[key])} {label}."))
        if withdrawals.get("unresolved"):
            self.stderr.write(self.style.ERROR(
                f"{len(withdrawals['unresolved'])} withdrawal(s) are not on the network and could not be "
                f"rebroadcast — review them in the admin: {withdrawals['unresolved']}"
            ))
        if not (processed or promoted or dropped or failed or any(withdrawals.values())):
            logger.debug("On-chain scan: nothing new")
            self.stdout.write("No new deposits.")

        if failed:
            # Distinct from "no new deposits": these addresses could not be checked at all
            # (e.g. the configured explorer provider is unreachable) — never conflate the two.
            logger.warning("On-chain scan: %d address(es) could not be checked: %s", len(failed), failed)
            self.stderr.write(self.style.ERROR(
                f"{len(failed)} address(es) could NOT be checked (explorer unreachable?):"
            ))
            for item in failed:
                self.stderr.write(self.style.ERROR(f"  - {item['address']}: {item['error']}"))
