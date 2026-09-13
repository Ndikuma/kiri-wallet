"""
Thin HTTP client for AmatoPay's merchant API (https://github.com/.../AmatoPay).

We act as an AmatoPay *merchant*: this wallet creates a hosted checkout
session to collect a BIF top-up from a payer's mobile-money alias, then
polls the session/payment status. See AmatoPay's docs/ENDPOINTS.md.

Auth: `Authorization: Bearer sk_...` (merchant secret key).
"""
from __future__ import annotations

from typing import Any

import requests

from wallet.options import SETTINGS


class AmatoPayError(Exception):
    """Raised for AmatoPay API/network errors."""


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
            raise AmatoPayError(f"AmatoPay {method} {path} returned {response.status_code}: {response.text[:500]}")
        if not response.content:
            return {}
        return response.json()

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
