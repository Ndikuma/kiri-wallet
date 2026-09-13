"""
Reusable Blink GraphQL WebSocket client.

Keeps connection headers, user-agent handling, and graphql-transport-ws
handshake in one place so subscribers/commands do not repeat fragile setup.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
import inspect
import json
import logging
from typing import Any

import websockets

logger = logging.getLogger(__name__)

DEFAULT_BLINK_WS_URL = "wss://ws.blink.sv/graphql"
DEFAULT_USER_AGENT = "BtcWalletBlinkWS/1.0"
DEFAULT_SUBPROTOCOLS = ("graphql-transport-ws",)
DEFAULT_CONNECTION_TIMEOUT = 30
DEFAULT_ACK_TIMEOUT = 10


class BlinkWebSocketHandshakeError(RuntimeError):
    """Raised when Blink does not acknowledge the GraphQL WebSocket session."""


class BlinkGraphQLWebSocketClient:
    """Small GraphQL-over-WebSocket client for Blink subscriptions."""

    def __init__(
        self,
        *,
        api_key: str,
        ws_url: str = DEFAULT_BLINK_WS_URL,
        user_agent: str = DEFAULT_USER_AGENT,
        headers: Mapping[str, str] | None = None,
        subprotocols: Sequence[str] = DEFAULT_SUBPROTOCOLS,
        connection_timeout: int = DEFAULT_CONNECTION_TIMEOUT,
        ack_timeout: int = DEFAULT_ACK_TIMEOUT,
        ping_interval: int = 20,
        ping_timeout: int = 10,
        close_timeout: int = 5,
    ) -> None:
        if not api_key:
            raise ValueError("BLINK_API_KEY not provided")

        self.api_key = api_key
        self.ws_url = ws_url
        self.user_agent = user_agent
        self.headers = dict(headers or {})
        self.subprotocols = list(subprotocols)
        self.connection_timeout = connection_timeout
        self.ack_timeout = ack_timeout
        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout
        self.close_timeout = close_timeout

    def connect_options(self) -> dict[str, Any]:
        options: dict[str, Any] = {
            "subprotocols": self.subprotocols,
            "user_agent_header": self.user_agent,
            "open_timeout": self.connection_timeout,
            "ping_interval": self.ping_interval,
            "ping_timeout": self.ping_timeout,
            "close_timeout": self.close_timeout,
        }

        custom_headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() != "user-agent"
        }
        if custom_headers:
            signature = inspect.signature(websockets.connect)
            if "extra_headers" in signature.parameters:
                options["extra_headers"] = custom_headers
            elif "additional_headers" in signature.parameters:
                options["additional_headers"] = custom_headers
            else:
                logger.warning(
                    "Installed websockets=%s does not support custom connection headers; ignoring %s header(s)",
                    getattr(websockets, "__version__", "unknown"),
                    len(custom_headers),
                )

        logger.info(
            "Blink WebSocket client | websockets=%s ws=%s subprotocols=%s headers=%s",
            getattr(websockets, "__version__", "unknown"),
            self.ws_url,
            ",".join(self.subprotocols),
            sorted(custom_headers),
        )
        return options

    async def connect(self) -> Any:
        logger.info("Connecting to Blink WebSocket: %s", self.ws_url)
        ws = await websockets.connect(self.ws_url, **self.connect_options())

        try:
            await self.send_json(ws, {
                "type": "connection_init",
                "payload": {"X-API-KEY": self.api_key},
            })
            logger.info("Waiting for Blink connection_ack")

            raw_ack = await asyncio.wait_for(ws.recv(), timeout=self.ack_timeout)
            ack = json.loads(raw_ack)
            logger.debug("Blink handshake response: %s", ack)

            if ack.get("type") != "connection_ack":
                raise BlinkWebSocketHandshakeError(f"Unexpected Blink handshake response: {ack}")

            logger.info("Blink handshake successful")
            return ws
        except Exception:
            await ws.close()
            raise

    @asynccontextmanager
    async def session(self) -> AsyncIterator[Any]:
        ws = await self.connect()
        try:
            yield ws
        finally:
            await ws.close()

    async def subscribe(self, ws: Any, *, query: str, subscription_id: str) -> None:
        await self.send_json(ws, {
            "id": subscription_id,
            "type": "subscribe",
            "payload": {"query": query},
        })

    async def send_json(self, ws: Any, payload: dict[str, Any]) -> None:
        await ws.send(json.dumps(payload))
