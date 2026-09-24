"""
Thin HTTP client for AmatoPay's merchant API (https://github.com/.../AmatoPay).

We act as an AmatoPay *merchant*: this wallet creates a hosted checkout
session to collect a BIF top-up from a payer's mobile-money alias, then
polls the session/payment status. See AmatoPay's docs/ENDPOINTS.md.

Auth: `Authorization: Bearer sk_...` (merchant secret key).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import quote

import requests

from wallet.options import SETTINGS


class AmatoPayError(Exception):
    """Raised for AmatoPay API/network errors."""


def _error_message(status_code: int, text: str) -> str:
    """Pull a human-readable reason out of AmatoPay's DRF-shaped error body
    (`{"detail": "..."}` or `{"field": ["msg"]}`), falling back to raw text."""
    try:
        body = json.loads(text)
    except ValueError:
        return text[:500] or f"HTTP {status_code}"

    if isinstance(body, dict):
        if "detail" in body:
            return str(body["detail"])
        for value in body.values():
            if isinstance(value, list) and value:
                return str(value[0])
            if isinstance(value, str):
                return value
    return text[:500]


def verify_amatopay_signature(secret: str, signature_header: str, raw_body: bytes, tolerance: int = 300) -> bool:
    """Verify a webhook delivery's `AmatoPay-Signature: t=<ts>,v1=<hmac>` header.

    HMAC-SHA256 over `<timestamp>.<raw_body>` using the endpoint's webhook secret,
    compared in constant time; stale timestamps (beyond `tolerance` seconds) are rejected.
    """
    if not secret or not signature_header:
        return False
    try:
        parts = dict(p.split("=", 1) for p in signature_header.split(","))
        ts, v1 = parts["t"], parts["v1"]
        if abs(time.time() - int(ts)) > tolerance:
            return False
    except (KeyError, ValueError):
        return False

    expected = hmac.new(secret.encode(), f"{ts}.".encode() + raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, v1)


class AmatoPayClient:
    def __init__(self, api_key: str | None = None, base_url: str | None = None, timeout: int = 20):
        resolved_key = api_key or SETTINGS.AMATOPAY_API_KEY
        if not resolved_key:
            raise ValueError("AMATOPAY_API_KEY not provided")
        self.api_key = resolved_key
        self.base_url = (base_url or SETTINGS.AMATOPAY_BASE_URL).rstrip("/")
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        try:
            response = requests.request(method, url, headers=self._headers(), timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            raise AmatoPayError(f"AmatoPay request failed: {exc}") from exc

        if response.status_code >= 400:
            raise AmatoPayError(_error_message(response.status_code, response.text))
        if not response.content:
            return {}
        return response.json()

    def verify_alias(self, payer_alias: str) -> dict[str, Any]:
        """GET /api/v1/checkout/alias-verifications/ — confirm a MOBILE alias is active
        and payable, and resolve its registered display name. Raises AmatoPayError
        (400) if the alias isn't payable — the message carries a human-readable reason."""
        return self._request("GET", f"/api/v1/checkout/alias-verifications/?payer_alias={quote(payer_alias)}")

    def create_checkout_session(
        self,
        *,
        amount_bif: int,
        payer_alias: str,
        description: str = "Wallet BIF top-up",
        return_url: str = "",
        order_number: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """POST /api/v1/checkout/sessions/ — create a session pushed to the payer's mobile alias."""
        payload = {
            "amount": amount_bif,
            "currency": "BIF",
            "payer_alias": payer_alias,
            "description": description,
        }
        if return_url:
            payload["return_url"] = return_url
        if order_number:
            payload["order_number"] = order_number
        if metadata:
            payload["metadata"] = metadata
        return self._request("POST", "/api/v1/checkout/sessions/", json=payload)

    def get_checkout_session(self, session_id: str) -> dict[str, Any]:
        """GET /api/v1/checkout/sessions/{id}/"""
        return self._request("GET", f"/api/v1/checkout/sessions/{session_id}/")

    def get_checkout_status(self, session_id: str) -> dict[str, Any]:
        """GET /api/v1/checkout/sessions/{id}/status/ — lightweight status-only poll."""
        return self._request("GET", f"/api/v1/checkout/sessions/{session_id}/status/")

    def confirm_delivery(self, payment_reference: str, secure_code: str) -> dict[str, Any]:
        """POST /api/v1/payments/{reference}/confirm-delivery/ — release AmatoPay's
        held funds to our settlement account. `secure_code` is the payer's six-digit
        release code (shown only to the payer, never to the merchant API); only valid
        once AmatoPay has moved the payment to `delivery_pending`. Raises AmatoPayError
        on a wrong code, a wrong-status payment, or after 5 failed attempts (locked)."""
        return self._request(
            "POST", f"/api/v1/payments/{quote(payment_reference)}/confirm-delivery/",
            json={"secure_code": secure_code},
        )
