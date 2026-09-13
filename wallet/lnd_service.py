import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


class LNDError(Exception):
    pass


class LNDService:
    """Thin wrapper around an LND REST node for BOLT11 invoice ops. Optional: only used if LND_REST_URL/LND_MACAROON are set."""

    def __init__(self):
        self.rest_url = settings.LND_REST_URL
        self.headers = {"Grpc-Metadata-macaroon": settings.LND_MACAROON}
        self._verify = settings.LND_CERT_PATH

    def _post(self, path: str, body: dict) -> dict:
        url = f"{self.rest_url}{path}"
        try:
            r = requests.post(url, json=body, headers=self.headers, verify=self._verify, timeout=15)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as exc:
            logger.error("LND error for %s: %s", path, exc)
            raise LNDError(str(exc))

    def create_invoice(self, amount_sats: int, memo: str = "Wallet deposit", expiry: int = 3600) -> dict:
        data = {"value": amount_sats, "memo": memo, "expiry": expiry, "private": True, "settle_date": 0}
        return self._post("/v1/invoices", data)

    def decode_payment_request(self, payment_request: str) -> dict:
        return self._post("/v1/payreq/decode", {"pay_req": payment_request})

    def send_payment(self, payment_request: str) -> dict:
        return self._post("/v2/router/send", {"payment_request": payment_request})
