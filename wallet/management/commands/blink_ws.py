"""
Management command: blink_ws — runs the Blink WebSocket subscriber.

This is the monitoring process that watches for incoming Lightning payments
in real time and credits wallets (or settles POS charges as BIF) as they
arrive. Run it as a long-lived worker/systemd service alongside the API:

    python manage.py blink_ws                       # websocket only
    python manage.py blink_ws --backfill             # + one-time backfill on startup
    python manage.py blink_ws --header "X-Foo=bar"   # extra WS headers, repeatable
"""

import asyncio
import logging

from django.conf import settings
from django.core.management.base import BaseCommand

from wallet.subscriber import BlinkInvoiceSubscriber

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run the Blink WebSocket invoice subscriber (long-running)."

    key = settings.BLINK_API_KEY
    url = settings.BLINK_WS_URL

    def add_arguments(self, parser):
        parser.add_argument(
            "--batch-size", type=int, default=50,
            help="Number of recent transactions to scan when --backfill is used.",
        )
        parser.add_argument(
            "--backfill", action="store_true", dest="backfill",
            help="Run one initial Blink backfill before starting websocket subscriptions.",
        )
        parser.add_argument(
            "--header", action="append", default=[], metavar="NAME=VALUE",
            help="Extra WebSocket connection header. Can be provided multiple times.",
        )

    def handle(self, *args, **options):
        if not self.key:
            self.stderr.write(self.style.ERROR("BLINK_API_KEY is not set. Add it to .env before running this command."))
            return

        logger.info("=" * 60)
        logger.info("Starting Blink WebSocket Subscriber")
        logger.info("WebSocket URL: %s", self.url)
        logger.info("Environment: %s", "development" if settings.DEBUG else "production")
        logger.info("=" * 60)

        try:
            asyncio.run(self._run(options))
        except KeyboardInterrupt:
            logger.warning("Blink subscriber interrupted by user")
            self.stdout.write(self.style.WARNING("\nSubscriber stopped manually."))
        except Exception as exc:
            logger.exception("Fatal error in Blink subscriber: %s", exc)

    async def _run(self, options):
        logger.info("Initializing BlinkInvoiceSubscriber...")

        batch_size = int(options.get("batch_size") or 50)
        await_backfill = bool(options.get("backfill", False))
        headers = self._parse_headers(options.get("header") or [])
        logger.info(
            "Blink WS options | backfill=%s batch_size=%s extra_headers=%s",
            await_backfill, batch_size, sorted(headers),
        )

        sub = BlinkInvoiceSubscriber(api_key=self.key, ws_url=self.url, headers=headers)
        logger.info("BlinkInvoiceSubscriber initialized successfully")

        if await_backfill:
            logger.info("Running initial Blink backfill (batch_size=%s)...", batch_size)
            await sub.backfill_and_process(batch_size=batch_size)
            logger.info("Initial Blink backfill finished")
        else:
            logger.info("Initial Blink backfill disabled; running websocket only")

        await sub.run_forever()

    def _parse_headers(self, values: list[str]) -> dict[str, str]:
        headers: dict[str, str] = {}
        for value in values:
            if "=" not in value:
                raise ValueError(f"Invalid header {value!r}. Use NAME=VALUE.")
            name, header_value = value.split("=", 1)
            name = name.strip()
            if not name:
                raise ValueError("Header name cannot be empty.")
            headers[name] = header_value.strip()
        return headers
