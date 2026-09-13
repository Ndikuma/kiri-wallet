"""
Management command: worker — runs both wallet monitors in one process:

  - Lightning: the Blink WebSocket invoice subscriber (push, real-time).
  - On-chain: periodic Bitcoin address scanning (poll, no push mechanism
    exists for on-chain deposits).

Both run concurrently on one asyncio event loop, so one process/systemd
unit/container is enough to keep both currencies' deposits flowing. Each
side can be disabled independently, and each degrades gracefully (logs a
warning and skips itself) if its provider isn't configured — a missing
BLINK_API_KEY or WALLET_ENCRYPTION_KEY won't take the other side down.

    python manage.py worker                              # both monitors
    python manage.py worker --backfill                   # + one Blink backfill on startup
    python manage.py worker --onchain-interval 30
    python manage.py worker --skip-onchain                # Lightning only (same as blink_ws)
    python manage.py worker --skip-lightning              # on-chain only (same as scan_bitcoin --watch)
"""

import asyncio
import logging

from django.core.management.base import BaseCommand, CommandError

from wallet.bitcoin import CustodialBitcoinService
from wallet.options import SETTINGS
from wallet.subscriber import BlinkInvoiceSubscriber

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run the Lightning (Blink WebSocket) and on-chain Bitcoin monitors together, in one process."

    def add_arguments(self, parser):
        parser.add_argument(
            "--onchain-interval", type=int, default=60,
            help="Seconds between on-chain scans (default: 60).",
        )
        parser.add_argument(
            "--backfill", action="store_true",
            help="Run one initial Blink backfill before starting websocket subscriptions.",
        )
        parser.add_argument(
            "--batch-size", type=int, default=50,
            help="Number of recent transactions to scan when --backfill is used.",
        )
        parser.add_argument(
            "--skip-lightning", action="store_true",
            help="Only run the on-chain scanner.",
        )
        parser.add_argument(
            "--skip-onchain", action="store_true",
            help="Only run the Blink WebSocket subscriber.",
        )

    def handle(self, *args, **options):
        if options["skip_lightning"] and options["skip_onchain"]:
            raise CommandError("--skip-lightning and --skip-onchain cannot both be set — there'd be nothing to run.")

        logger.info("=" * 60)
        logger.info("Starting wallet worker (lightning=%s, onchain=%s)", not options["skip_lightning"], not options["skip_onchain"])
        logger.info("=" * 60)

        try:
            asyncio.run(self._run(options))
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING("\nWorker stopped manually."))

    async def _run(self, options):
        tasks = []
        if not options["skip_lightning"]:
            tasks.append(asyncio.create_task(self._run_lightning(options), name="lightning"))
        if not options["skip_onchain"]:
            tasks.append(asyncio.create_task(self._run_onchain(options["onchain_interval"]), name="onchain"))

        await asyncio.gather(*tasks)

    async def _run_lightning(self, options):
        if not SETTINGS.has_blink:
            message = "BLINK_API_KEY is not set — Lightning subscriber disabled for this run."
            logger.warning(message)
            self.stdout.write(self.style.WARNING(message))
            return

        subscriber = BlinkInvoiceSubscriber(ws_url=SETTINGS.BLINK_WS_URL)

        if options["backfill"]:
            logger.info("Running initial Blink backfill (batch_size=%s)...", options["batch_size"])
            await subscriber.backfill_and_process(batch_size=options["batch_size"])
            logger.info("Initial Blink backfill finished")

        await subscriber.run_forever()

    async def _run_onchain(self, interval: int):
        service = CustodialBitcoinService()
        while True:
            try:
                processed = await asyncio.to_thread(service.scan_all_users)
                if processed:
                    logger.info("On-chain scan processed %d deposit(s): %s", len(processed), processed)
            except Exception as exc:  # noqa: BLE001
                logger.exception("On-chain scan failed: %s", exc)
            await asyncio.sleep(interval)
