import base64
import io
from datetime import timedelta
from decimal import Decimal
from typing import Any

import qrcode
import requests
from django.utils import timezone

try:
    from bolt11 import decode as bolt11_decode
except ImportError:
    bolt11_decode = None

try:
    from decouple import config
except ImportError:
    config = None


def _coerce_invoice_amount_msat(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, int):
        return value
    decimal_value = Decimal(str(value))
    if decimal_value == decimal_value.to_integral_value():
        return int(decimal_value)
    btc_to_msat = Decimal("100000000000")
    return int((decimal_value * btc_to_msat).to_integral_value())


def is_lightning_address(value: str) -> bool:
    normalized_value = (value or "").strip()
    return "@" in normalized_value and " " not in normalized_value


def is_lnurl(value: str) -> bool:
    normalized_value = (value or "").strip().lower()
    return normalized_value.startswith("lnurl1") or normalized_value.startswith("lnurlp://")


def decode_blink_payment_request(payment_request: str, *, require_amount: bool = True) -> dict[str, Any]:
    normalized_request = (payment_request or "").strip()
    if not normalized_request:
        raise ValueError("Please enter a Lightning invoice.")
    if bolt11_decode is None:
        raise RuntimeError(
            "Lightning invoice decoding is unavailable because the bolt11 package is not installed "
            "(it requires the `coincurve` native extension, which currently has no wheel for this "
            "Python version). Install it in a Python 3.11/3.12 environment to enable this feature."
        )
    try:
        invoice = bolt11_decode(normalized_request)
    except Exception as exc:
        raise ValueError("Enter a valid Lightning invoice.") from exc

    amount_msat = _coerce_invoice_amount_msat(getattr(invoice, "amount_msat", None))
    if amount_msat is None:
        amount_msat = _coerce_invoice_amount_msat(getattr(invoice, "amount", None))
    if amount_msat is None and require_amount:
        raise ValueError("The Lightning invoice must include an amount.")
    if amount_msat is None:
        amount_sat = None
    elif amount_msat <= 0:
        if require_amount:
            raise ValueError("The Lightning invoice amount must be greater than 0.")
        amount_msat = None
        amount_sat = None
    elif amount_msat % 1000 != 0:
        raise ValueError("The Lightning invoice amount must be a whole number of SAT.")
    else:
        amount_sat = (Decimal(amount_msat) / Decimal("1000")).quantize(Decimal("1"))
    return {
        "payment_request": normalized_request,
        "amount_msat": amount_msat,
        "amount_sat": amount_sat,
        "currency": getattr(invoice, "currency", ""),
    }


class BlinkWalletError(Exception):
    """Base exception for Blink/LN wallet operations."""


class BlinkWallet:
    """Thin Blink GraphQL client — all amounts in **satoshis**."""

    def __init__(self, api_key: str | None = None, api_url: str | None = None, timeout: int = 20):
        resolved_key = api_key or (config("BLINK_API_KEY") if config else None)
        if not resolved_key:
            raise ValueError("BLINK_API_KEY not provided")
        self.api_key = resolved_key
        self.api_url = api_url or "https://api.blink.sv/graphql"
        self.timeout = timeout
        self._wallet_id: str | None = None

    # ── low-level HTTP ─────────────────────────────────────

    def _post(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {"X-API-KEY": self.api_key, "Content-Type": "application/json"}
        payload = {"query": query, "variables": variables or {}}
        response = requests.post(self.api_url, json=payload, headers=headers, timeout=self.timeout)
        response.raise_for_status()
        data = response.json()
        if data.get("errors"):
            raise Exception(data["errors"])
        return data

    # ── wallet discovery ───────────────────────────────────

    def get_wallets(self) -> list[dict[str, Any]]:
        query = """
        query Me {
          me {
            defaultAccount {
              wallets {
                id
                walletCurrency
                balance
              }
            }
          }
        }
        """
        data = self._post(query)
        return data.get("data", {}).get("me", {}).get("defaultAccount", {}).get("wallets", [])

    def get_wallet_by_currency(self, currency: str = "BTC") -> dict[str, Any] | None:
        for wallet in self.get_wallets():
            if wallet.get("walletCurrency") == currency:
                self._wallet_id = wallet["id"]
                return wallet
        return None

    def get_btc_wallet(self) -> dict[str, Any] | None:
        return self.get_wallet_by_currency("BTC")

    @property
    def wallet_id(self) -> str:
        if not self._wallet_id:
            wallet = self.get_wallet_by_currency()
            if not wallet:
                raise Exception("BTC wallet not found or BLINK_API_KEY is invalid")
        return self._wallet_id

    # ── invoice / payment operations ───────────────────────

    def create_ln_invoice(
        self,
        amount: int,
        *,
        wallet_id: str | None = None,
        memo: str = "",
        expires_in_seconds: int = 3600,
    ) -> dict[str, Any]:
        if wallet_id is None:
            self.get_wallet_by_currency()
        query = """
        mutation LnInvoiceCreate($input: LnInvoiceCreateInput!) {
          lnInvoiceCreate(input: $input) {
            invoice {
              paymentRequest
              paymentHash
              paymentSecret
              satoshis
            }
            errors { message code }
          }
        }
        """
        result = self._post(query, {"input": {
            "walletId": wallet_id or self.wallet_id,
            "amount": int(amount),
            "memo": memo[:120],
        }})
        payload = result.get("data", {}).get("lnInvoiceCreate", {}) or {}
        errors = payload.get("errors") or []
        if errors:
            raise Exception(errors)
        invoice = payload.get("invoice")
        if not invoice:
            raise Exception("Invoice creation returned no invoice data")
        invoice["expiresAt"] = timezone.now() + timedelta(seconds=expires_in_seconds)
        invoice["qrCode"] = self.generate_qr(invoice.get("paymentRequest", ""))
        return invoice

    def get_ln_invoice_status(
        self, *, payment_hash: str | None = None, payment_request: str | None = None
    ) -> dict[str, Any]:
        if payment_hash:
            query = """
            query LnInvoicePaymentStatusByHash($input: LnInvoicePaymentStatusByHashInput!) {
              lnInvoicePaymentStatusByHash(input: $input) {
                paymentHash paymentPreimage paymentRequest status
              }
            }
            """
            result = self._post(query, {"input": {"paymentHash": payment_hash}})
            return result.get("data", {}).get("lnInvoicePaymentStatusByHash", {}) or {}

        if payment_request:
            query = """
            query LnInvoicePaymentStatusByPaymentRequest($input: LnInvoicePaymentStatusByPaymentRequestInput!) {
              lnInvoicePaymentStatusByPaymentRequest(input: $input) {
                paymentHash paymentPreimage paymentRequest status
              }
            }
            """
            result = self._post(query, {"input": {"paymentRequest": payment_request}})
            return result.get("data", {}).get("lnInvoicePaymentStatusByPaymentRequest", {}) or {}

        raise ValueError("payment_hash or payment_request is required")

    def pay_ln_invoice(
        self,
        payment_request: str,
        wallet_id: str | None = None,
        amount: int | None = None,
    ) -> dict[str, Any]:
        if wallet_id is None:
            self.get_wallet_by_currency()
        query = """
        mutation LnInvoicePaymentSend($input: LnInvoicePaymentInput!) {
          lnInvoicePaymentSend(input: $input) {
            status errors { message code path }
          }
        }
        """
        input_payload = {
            "walletId": wallet_id or self.wallet_id,
            "paymentRequest": payment_request,
        }
        if amount is not None:
            input_payload["amount"] = int(amount)
        result = self._post(query, {"input": input_payload})
        payload = (result.get("data", {}) or {}).get("lnInvoicePaymentSend", {}) or {}
        if payload.get("errors"):
            raise Exception(payload["errors"])
        return payload

    def pay_ln_address(self, ln_address: str, amount: int, wallet_id: str | None = None) -> dict[str, Any]:
        if wallet_id is None:
            self.get_wallet_by_currency()
        query = """
        mutation LnAddressPaymentSend($input: LnAddressPaymentSendInput!) {
          lnAddressPaymentSend(input: $input) {
            status errors { message }
          }
        }
        """
        result = self._post(query, {"input": {
            "walletId": wallet_id or self.wallet_id,
            "amount": int(amount),
            "lnAddress": ln_address,
        }})
        payload = result.get("data", {}).get("lnAddressPaymentSend", {}) or {}
        if payload.get("errors"):
            raise Exception(payload["errors"])
        return payload

    def pay_lnurl(self, lnurl: str, amount: int, wallet_id: str | None = None) -> dict[str, Any]:
        if wallet_id is None:
            btc_wallet = self.get_btc_wallet()
            if not btc_wallet:
                raise ValueError("No BTC wallet found.")
            wallet_id = btc_wallet["id"]

        query = """
        mutation LnurlPaymentSend($input: LnurlPaymentSendInput!) {
          lnurlPaymentSend(input: $input) {
            status
            errors { code message path }
          }
        }
        """
        result = self._post(query, {"input": {
            "walletId": str(wallet_id),
            "amount": int(amount),
            "lnurl": lnurl,
        }})
        payload = result.get("data", {}).get("lnurlPaymentSend", {}) or {}
        if payload.get("errors"):
            raise Exception(payload["errors"])
        return payload

    # ── QR code ────────────────────────────────────────────

    def generate_qr(self, data: str) -> str:
        qr = qrcode.QRCode(version=1, box_size=10, border=4)
        qr.add_data(data)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        encoded = base64.b64encode(buf.getvalue()).decode()
        return f"data:image/png;base64,{encoded}"
