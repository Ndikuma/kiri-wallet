"""
Management command: scan_bitcoin — scan every on-chain deposit address for new
UTXOs and credit wallets accordingly (0-3 conf = pending, 4+ conf = confirmed).

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
            processed = service.scan_all_users()
        except Exception as exc:  # noqa: BLE001
            logger.exception("On-chain scan failed: %s", exc)
            self.stderr.write(self.style.ERROR(f"Scan failed: {exc}"))
            return

        if processed:
            logger.info("On-chain scan processed %d deposit(s): %s", len(processed), processed)
            self.stdout.write(self.style.SUCCESS(f"Processed {len(processed)} deposit(s)."))
        else:
            logger.debug("On-chain scan: no new deposits")
            self.stdout.write("No new deposits.")
