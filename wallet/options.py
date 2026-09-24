"""
wallet/options.py — Blink / LND / AmatoPay configuration.
"""
import logging

from decouple import config as env

logger = logging.getLogger(__name__)


class BlinkWalletOptions:
    """Lightning network options for wallets."""

    FETCH_POLL_TIME = 2
    SETTLE_POLL_TIME = 3
    FETCH_LIMIT = 10
    SETTLE_LIMIT = 60
    ESTIMATED_FEE = 10
    MIN_WITHDRAWAL = 1_000
    MIN_DEPOSIT = 1_000

    INVOICE_MEMO_MAX = 120
    INVOICE_EXPIRY_SEC = 3600

    LN_ADDRESS_PATTERN = r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$"


class Options(BlinkWalletOptions):
    """Aggregates Blink + LND + AmatoPay options in one namespace."""

    BLINK_API_KEY = env("BLINK_API_KEY", default="")
    BLINK_API_URL = env("BLINK_API_URL", default="https://api.blink.sv/graphql")
    BLINK_WS_URL = env("BLINK_WS_URL", default="wss://ws.blink.sv/graphql")
    BLINK_WS_USER_AGENT = env("BLINK_WS_USER_AGENT", default="BtcWalletBlinkWS/1.0")

    LND_REST_URL = env("LND_REST_URL", default="")
    LND_MACAROON = env("LND_MACAROON", default="")
    LND_CERT_PATH = env("LND_CERT_PATH", default="./tls.cert")

    AMATOPAY_API_KEY = env("AMATOPAY_API_KEY", default="")
    AMATOPAY_BASE_URL = env("AMATOPAY_BASE_URL", default="http://localhost:8000")
    AMATOPAY_WEBHOOK_SECRET = env("AMATOPAY_WEBHOOK_SECRET", default="")

    @property
    def has_blink(self) -> bool:
        return bool(self.BLINK_API_KEY)

    @property
    def has_lnd(self) -> bool:
        return bool(self.LND_REST_URL and self.LND_MACAROON)

    @property
    def has_amatopay(self) -> bool:
        return bool(self.AMATOPAY_API_KEY)


SETTINGS = Options()
