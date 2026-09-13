"""
Standalone BlinkInvoiceSubscriber — long-running Blink WebSocket monitor.
Run via: python manage.py blink_ws
"""

import asyncio
import json
import logging
import random
from typing import Any
from collections.abc import Callable

import requests
import websockets

from wallet.blink_ws_client import (
    DEFAULT_ACK_TIMEOUT,
    DEFAULT_BLINK_WS_URL,
    DEFAULT_CONNECTION_TIMEOUT,
    DEFAULT_USER_AGENT,
    BlinkGraphQLWebSocketClient,
)
from wallet.invoice_updater import process_blink_invoice_update
from wallet.options import SETTINGS

logger = logging.getLogger(__name__)

BLINK_WS_URL = DEFAULT_BLINK_WS_URL

INITIAL_RECONNECT_DELAY = 2
MAX_RECONNECT_DELAY = 300

CONNECTION_TIMEOUT = DEFAULT_CONNECTION_TIMEOUT
ACK_TIMEOUT = DEFAULT_ACK_TIMEOUT
WS_USER_AGENT = DEFAULT_USER_AGENT


class BlinkInvoiceSubscriber:
    def _short_hash(self, payment_hash: str) -> str:
        if not payment_hash:
            return "-"
        return payment_hash if len(payment_hash) <= 16 else f"{payment_hash[:10]}...{payment_hash[-6:]}"

    def fetch_received_transactions(self, batch_size: int = 50) -> list[dict[str, Any]]:
        """Fetch latest received transactions from Blink (GraphQL query).

        Used as a backfill when WebSocket disconnects / misses events.
        """
        logger.info("[Scan] Fetching last %s received transactions from Blink...", batch_size)

        query = """
        query Transactions($first: Int) {
          me {
            defaultAccount {
              transactions(first: $first) {
                edges {
                  node {
                    id
                    createdAt
                    settlementAmount
                    status
                    direction
                    initiationVia {
                      ... on InitiationViaLn { paymentHash }
                    }
                  }
                }
              }
            }
          }
        }
        """

        variables = {"first": batch_size}
        headers = {"X-API-KEY": self.api_key, "Content-Type": "application/json"}

        response = requests.post(
            "https://api.blink.sv/graphql",
            json={"query": query, "variables": variables},
            headers=headers,
            timeout=20,
        )
        logger.info("[Scan] Blink HTTP status=%s", response.status_code)
        if response.status_code != 200:
            logger.warning("[Blink] HTTP %s: %s", response.status_code, response.text)
            return []

        data = response.json()
        account = data.get("data", {}).get("me", {}).get("defaultAccount", {})

        if not account or "transactions" not in account or not account["transactions"]:
            logger.warning("[Blink] No transactions in response data")
            return []

        edges = account["transactions"].get("edges", [])
        if not isinstance(edges, list):
            logger.warning("[Blink] Unexpected transactions edges structure")
            return []

        txns: list[dict[str, Any]] = []
        for edge in edges:
            node = (edge or {}).get("node") or {}
            if node.get("direction") != "RECEIVE":
                continue

            initiation = node.get("initiationVia") or {}
            payment_hash = initiation.get("paymentHash")

            txns.append({
                "id": node.get("id"),
                "created_at": node.get("createdAt"),
                "payment_hash": payment_hash,
                "amount": node.get("settlementAmount"),
                "status": node.get("status"),
            })

        logger.info("[Scan] Received %s Blink RECEIVE transaction(s)", len(txns))
        return txns

    async def backfill_and_process(self, batch_size: int = 50) -> None:
        """Backfill pending Blink invoice transactions by scanning received transactions."""
        try:
            logger.info("[Backfill] Starting scan batch_size=%s", batch_size)
            transactions = await asyncio.to_thread(self.fetch_received_transactions, batch_size)

            if not transactions:
                logger.info("[Backfill] Finished: scanned=0 paid=0 matched=0 processed=0 skipped=0")
                return

            paid_transactions = []
            ignored = 0

            for txn in transactions:
                payment_hash = txn.get("payment_hash") or ""
                if not payment_hash:
                    ignored += 1
                    continue

                status = (txn.get("status") or "").upper()
                if status not in {"SUCCESS", "SETTLED", "PAID"}:
                    ignored += 1
                    continue

                paid_transactions.append(txn)

            logger.info(
                "[Backfill] Scanned=%s paid_receive=%s ignored=%s",
                len(transactions), len(paid_transactions), ignored,
            )

            if not paid_transactions:
                logger.info("[Backfill] Finished: matched=0 processed=0 skipped=0")
                return

            from wallet.invoice_updater import BLINK_INVOICE_TRANSACTION_TYPES
            from wallet.models import TransactionStatus, WalletTransaction

            payment_hashes = [txn["payment_hash"] for txn in paid_transactions]
            pending_hashes = set(
                await asyncio.to_thread(
                    lambda: list(
                        WalletTransaction.objects.filter(
                            type__in=BLINK_INVOICE_TRANSACTION_TYPES,
                            status=TransactionStatus.PENDING,
                            lnd_payment_hash__in=payment_hashes,
                        ).values_list("lnd_payment_hash", flat=True)
                    )
                )
            )

            logger.info(
                "[Backfill] Local pending matches=%s for paid Blink transaction(s)=%s",
                len(pending_hashes), len(paid_transactions),
            )

            if not pending_hashes:
                logger.info(
                    "[Backfill] No local pending WalletTransaction matched %s paid Blink transaction(s)",
                    len(paid_transactions),
                )
                for txn in paid_transactions[:10]:
                    logger.info(
                        "[Backfill] Unmatched paid Blink tx=%s hash=%s status=%s amount=%s",
                        txn.get("id") or "-",
                        self._short_hash(txn.get("payment_hash") or ""),
                        txn.get("status") or "-",
                        txn.get("amount") or 0,
                    )
                logger.info("[Backfill] Finished: matched=0 processed=0 skipped=%s", len(paid_transactions))
                return

            processed_any = False
            skipped = 0
            processed_count = 0

            for txn in paid_transactions:
                payment_hash = txn.get("payment_hash") or ""
                if payment_hash not in pending_hashes:
                    skipped += 1
                    logger.info(
                        "[Backfill] Skip already-processed/untracked Blink tx=%s hash=%s status=%s amount=%s",
                        txn.get("id") or "-",
                        self._short_hash(payment_hash),
                        txn.get("status") or "-",
                        txn.get("amount") or 0,
                    )
                    continue

                status = (txn.get("status") or "").upper()
                settlement_amount = txn.get("amount")
                logger.info(
                    "[Backfill] Processing Blink tx=%s hash=%s status=%s amount=%s",
                    txn.get("id") or "-",
                    self._short_hash(payment_hash),
                    status,
                    settlement_amount,
                )

                processed = await asyncio.to_thread(
                    process_blink_invoice_update,
                    payment_hash=payment_hash,
                    payment_request="",
                    status=status,
                    settlement_amount=settlement_amount,
                )

                if processed:
                    processed_any = True
                    processed_count += 1
                    logger.info(
                        "[Backfill] Processed Blink tx=%s wallet_tx=%s type=%s status=%s",
                        txn.get("id"), processed.pk, processed.type, processed.status,
                    )

            if not processed_any:
                logger.info("[Backfill] No pending WalletTransaction matched")
            elif skipped:
                logger.info("[Backfill] Skipped %s already-processed Blink transaction(s)", skipped)
            logger.info(
                "[Backfill] Finished: scanned=%s paid=%s matched=%s processed=%s skipped=%s",
                len(transactions), len(paid_transactions), len(pending_hashes), processed_count, skipped,
            )

        except Exception as exc:
            logger.exception("[Backfill] Failed: %s", exc)

    def __init__(
        self,
        api_key: str | None = None,
        ws_url: str = BLINK_WS_URL,
        user_agent: str | None = None,
        headers: dict[str, str] | None = None,
        heartbeat: Callable[[], None] | None = None,
    ):
        resolved_key = api_key or SETTINGS.BLINK_API_KEY

        if not resolved_key:
            logger.error("BLINK_API_KEY missing")
            raise ValueError("BLINK_API_KEY not provided")

        self.api_key = resolved_key
        self.ws_url = ws_url
        self.reconnect_delay = INITIAL_RECONNECT_DELAY
        self.heartbeat = heartbeat
        resolved_user_agent = user_agent or SETTINGS.BLINK_WS_USER_AGENT or WS_USER_AGENT
        self.ws_client = BlinkGraphQLWebSocketClient(
            api_key=self.api_key,
            ws_url=self.ws_url,
            user_agent=resolved_user_agent,
            headers=headers,
            connection_timeout=CONNECTION_TIMEOUT,
            ack_timeout=ACK_TIMEOUT,
        )

        logger.info(
            "BlinkInvoiceSubscriber initialized | ws=%s user_agent=%s extra_headers=%s",
            self.ws_url, resolved_user_agent, sorted((headers or {}).keys()),
        )

    async def run_forever(self) -> None:
        logger.info("Blink subscriber started")

        while True:
            try:
                self._heartbeat()
                logger.info("Opening Blink WebSocket session...")

                await self._run_session()

                logger.info("Blink session ended normally")
                self.reconnect_delay = INITIAL_RECONNECT_DELAY

            except asyncio.CancelledError:
                logger.warning("Blink subscriber cancelled")
                raise

            except TimeoutError:
                logger.warning("Blink WebSocket session timed out during connect/handshake")

            except websockets.ConnectionClosed as exc:
                logger.warning("Blink WebSocket connection closed: code=%s reason=%s", exc.code, exc.reason)

            except Exception as exc:
                logger.exception("Blink WebSocket session failed: %s", exc)

            jitter = random.uniform(0, self.reconnect_delay * 0.3)
            delay = min(self.reconnect_delay + jitter, MAX_RECONNECT_DELAY)

            logger.warning("Reconnecting to Blink in %.1f seconds", delay)
            await asyncio.sleep(delay)

            self.reconnect_delay = min(delay * 2, MAX_RECONNECT_DELAY)

    def _heartbeat(self) -> None:
        if not self.heartbeat:
            return
        try:
            self.heartbeat()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Blink subscriber heartbeat failed: %s", exc)

    async def _run_session(self) -> None:
        async with self.ws_client.session() as ws:
            logger.info("Connected to Blink WebSocket")
            self._heartbeat()

            # Backfill is handled by the management command to avoid duplicating work.

            logger.info("Subscribing to invoice updates")
            await self.ws_client.subscribe(
                ws, query=self._subscription_query(), subscription_id="blink-invoice-updates",
            )

            logger.info("Subscribed to Blink invoice updates; waiting for live events")

            async for raw_msg in ws:
                self._heartbeat()
                logger.debug(
                    "Received raw WebSocket message bytes=%s",
                    len(raw_msg) if isinstance(raw_msg, (str, bytes)) else "-",
                )
                await self._handle_message(ws, raw_msg)

    async def _handle_message(self, ws: Any, raw_message: str | bytes) -> None:
        try:
            payload = json.loads(raw_message)
        except (json.JSONDecodeError, TypeError):
            logger.warning("Invalid WebSocket JSON payload received")
            return

        message_type = payload.get("type")
        logger.debug("Received WebSocket message type=%s", message_type)

        if message_type == "ping":
            logger.debug("Received ping -> sending pong")
            await ws.send(json.dumps({"type": "pong"}))
            return

        if message_type != "next":
            logger.debug("Ignoring unsupported message type=%s", message_type)
            return

        transaction = (
            payload.get("payload", {})
            .get("data", {})
            .get("myUpdates", {})
            .get("update", {})
            .get("transaction", {})
        )

        if not transaction:
            logger.debug("No transaction in payload")
            return

        status = (transaction.get("status") or "").upper()
        direction = (transaction.get("direction") or "").upper()

        logger.info(
            "Transaction received | status=%s direction=%s amount=%s",
            status, direction, transaction.get("settlementAmount"),
        )

        if status not in {"SUCCESS", "SETTLED", "PAID"}:
            logger.debug("Ignoring transaction with status=%s", status)
            return

        if direction != "RECEIVE":
            logger.debug("Ignoring outgoing transaction")
            return

        initiation = transaction.get("initiationVia", {}) or {}
        payment_hash = initiation.get("paymentHash", "") or ""
        payment_request = initiation.get("paymentRequest", "") or ""
        settlement_amount = transaction.get("settlementAmount")

        logger.info(
            "Processing Blink payment | hash=%s amount=%s",
            self._short_hash(payment_hash), settlement_amount,
        )

        try:
            processed = await asyncio.to_thread(
                process_blink_invoice_update,
                payment_hash=payment_hash,
                payment_request=payment_request,
                status=status,
                settlement_amount=settlement_amount,
            )

            if processed:
                logger.info(
                    "WalletTransaction processed successfully | id=%s type=%s status=%s",
                    processed.pk, processed.type, processed.status,
                )
            else:
                logger.info(
                    "No pending WalletTransaction matched payment_hash=%s; event may already be processed",
                    payment_hash,
                )

        except Exception as exc:
            logger.exception("Invoice processing failed: %s", exc)

    def _subscription_query(self) -> str:
        return """
        subscription InvoiceUpdates {
          myUpdates {
            update {
              ... on LnUpdate {
                transaction {
                  direction
                  settlementAmount
                  status
                  initiationVia {
                    ... on InitiationViaLn {
                      paymentHash
                      paymentRequest
                    }
                  }
                }
              }
            }
          }
        }
        """
