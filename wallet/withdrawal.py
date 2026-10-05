from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from django.utils import timezone

from wallet.bitcoin import CustodialBitcoinService, parse_bip21, validate_destination
from wallet.esplora_client import EsploraError
from wallet.blink_wallet import BlinkWallet, decode_blink_payment_request, is_lightning_address, is_lnurl
from wallet.models import (
    TransactionStatus,
    TransactionType,
    Wallet,
    WalletTransaction,
    WithdrawalFeePolicy,
)
from wallet.options import SETTINGS

LIGHTNING_INVOICE_PREFIXES = ("lnbc", "lntb", "lnbcrt")
BITCOIN_ADDRESS_RE = re.compile(r"^(bc1|tb1|bcrt1|[13]|[mn2])[a-zA-HJ-NP-Z0-9]{20,90}$", re.IGNORECASE)


@dataclass(frozen=True)
class WithdrawalTarget:
    raw: str
    rail: str
    target_type: str
    amount_sats: int | None = None
    payment_request: str = ""
    lightning_address: str = ""
    lnurl: str = ""
    bitcoin_address: str = ""
    currency: str = ""

    def as_dict(self) -> dict[str, Any]:
        amount_source = "invoice" if self.amount_sats is not None else "user_input"
        return {
            "raw": self.raw,
            "rail": self.rail,
            "target_type": self.target_type,
            "amount_sats": self.amount_sats,
            "amount_source": amount_source,
            "payment_request": self.payment_request,
            "lightning_address": self.lightning_address,
            "lnurl": self.lnurl,
            "bitcoin_address": self.bitcoin_address,
            "currency": self.currency,
            "requires_amount": self.amount_sats is None,
        }


@dataclass(frozen=True)
class WithdrawalFeeQuote:
    amount_sats: int
    fee_sats: int
    wallet_debit_sats: int
    charge_to_user: bool
    policy: WithdrawalFeePolicy

    def as_dict(self) -> dict[str, Any]:
        return {
            "amount_sats": self.amount_sats,
            "estimated_fee_sats": self.fee_sats,
            "wallet_debit_sats": self.wallet_debit_sats,
            "fee_charged_to_user": self.charge_to_user,
            "fee_policy": {
                "id": self.policy.pk,
                "target_type": self.policy.target_type,
                "display_label": self.policy.display_label,
                "fixed_fee_sats": self.policy.fixed_fee_sats,
                "percent_fee_bps": self.policy.percent_fee_bps,
                "min_fee_sats": self.policy.min_fee_sats,
                "max_fee_sats": self.policy.max_fee_sats,
                "charge_to_user": self.policy.charge_to_user,
                "is_active": self.policy.is_active,
            },
        }


def decode_withdrawal_target(value: str) -> WithdrawalTarget:
    target = (value or "").strip()
    if not target:
        raise ValueError("Withdrawal destination is required.")

    uri_amount_sats = None
    lowered = target.lower()
    if lowered.startswith("lightning:"):
        target = target.split(":", 1)[1].strip()
    elif lowered.startswith("bitcoin:"):
        target, uri_amount_sats = parse_bip21(target)

    lowered = target.lower()
    if lowered.startswith(LIGHTNING_INVOICE_PREFIXES):
        invoice = decode_blink_payment_request(target, require_amount=False)
        return WithdrawalTarget(
            raw=target,
            rail="lightning",
            target_type="lightning_invoice",
            amount_sats=int(invoice["amount_sat"]) if invoice.get("amount_sat") is not None else None,
            payment_request=invoice["payment_request"],
            currency=invoice.get("currency", ""),
        )

    if is_lightning_address(target):
        return WithdrawalTarget(raw=target, rail="lightning", target_type="lightning_address", lightning_address=target)

    if is_lnurl(target):
        return WithdrawalTarget(raw=target, rail="lightning", target_type="lnurl", lnurl=target)

    if BITCOIN_ADDRESS_RE.match(target):
        validate_destination(target)  # checksum + network (e.g. a mainnet address on testnet4)
        return WithdrawalTarget(
            raw=target, rail="bitcoin", target_type="bitcoin_address", bitcoin_address=target,
            amount_sats=uri_amount_sats,
        )

    raise ValueError("Enter a valid Lightning invoice, Lightning address, or Bitcoin address.")


def resolve_withdrawal_amount(decoded: WithdrawalTarget, requested_amount: int | None) -> int:
    if decoded.amount_sats is not None:
        if requested_amount and requested_amount != decoded.amount_sats:
            raise ValueError(f"Amount must match the payment request amount: {decoded.amount_sats} sats.")
        return decoded.amount_sats

    if not requested_amount:
        raise ValueError("Amount is required for amountless invoices, Lightning addresses, and Bitcoin addresses.")
    return int(requested_amount)


def calculate_withdrawal_fee_quote(decoded: WithdrawalTarget, amount_sats: int) -> WithdrawalFeeQuote:
    policy = WithdrawalFeePolicy.get_for_target_type(decoded.target_type)
    fee = policy.calculate_fee(amount_sats) if policy.is_active else 0
    charge_to_user = bool(policy.is_active and policy.charge_to_user and fee)
    wallet_debit = amount_sats + fee if charge_to_user else amount_sats
    return WithdrawalFeeQuote(
        amount_sats=amount_sats, fee_sats=fee, wallet_debit_sats=wallet_debit,
        charge_to_user=charge_to_user, policy=policy,
    )


def estimate_withdrawal_fees(*, wallet: Wallet, destination: str, amount_sats: int | None) -> dict[str, Any]:
    decoded = decode_withdrawal_target(destination)

    try:
        amount = resolve_withdrawal_amount(decoded, amount_sats)
    except ValueError as exc:
        return {
            "target": decoded.as_dict(),
            "can_calculate": False,
            "can_withdraw": False,
            "message": str(exc),
            "amount_sats": None,
            "estimated_fee_sats": None,
            "wallet_debit_sats": None,
            "available_balance": wallet.available_balance,
            "fee_charged_to_user": False,
        }

    quote = calculate_withdrawal_fee_quote(decoded, amount)
    minimum = int(getattr(SETTINGS, "MIN_WITHDRAWAL", 1000))
    below_minimum = amount < minimum
    quote_data = quote.as_dict()
    message = f"Minimum withdrawal is {minimum} sats." if below_minimum else ""
    onchain = {}
    if decoded.target_type == "bitcoin_address" and not below_minimum:
        try:
            onchain = CustodialBitcoinService().quote_withdrawal(decoded.bitcoin_address, amount)
        except EsploraError as exc:
            onchain = {"error": f"Bitcoin network unreachable: {exc}"}
        except ValueError as exc:  # e.g. not enough on-chain liquidity
            onchain = {"error": str(exc)}
            message = str(exc)
    return {
        "target": decoded.as_dict(),
        "can_calculate": True,
        "can_withdraw": wallet.available_balance >= quote.wallet_debit_sats and not below_minimum
        and "error" not in onchain,
        "message": message,
        "onchain": onchain,
        **quote_data,
        "available_balance": wallet.available_balance,
        "balance_after": wallet.available_balance - quote.wallet_debit_sats,
        "minimum_withdrawal_sats": minimum,
        "fee_note": "Fee is calculated from the active Withdrawal Fee Policy for this target type.",
    }


def process_withdrawal(
    *, wallet: Wallet, destination: str, amount_sats: int | None, memo: str = "",
) -> tuple[WalletTransaction, dict[str, Any]]:
    decoded = decode_withdrawal_target(destination)
    amount = resolve_withdrawal_amount(decoded, amount_sats)
    quote = calculate_withdrawal_fee_quote(decoded, amount)

    if wallet.available_balance < quote.wallet_debit_sats:
        raise ValueError(f"Insufficient balance. Available: {wallet.available_balance:,.0f} sats.")

    if decoded.target_type == "lightning_invoice":
        return _withdraw_lightning_invoice(wallet, decoded, quote, memo)
    if decoded.target_type == "lightning_address":
        return _withdraw_lightning_address(wallet, decoded, quote, memo)
    if decoded.target_type == "lnurl":
        return _withdraw_lnurl(wallet, decoded, quote, memo)
    if decoded.target_type == "bitcoin_address":
        return _withdraw_bitcoin_address(wallet, decoded, quote, memo)

    raise ValueError("Unsupported withdrawal destination.")


def _withdraw_lightning_invoice(wallet, decoded, quote, memo):
    result = BlinkWallet().pay_ln_invoice(
        decoded.payment_request, amount=quote.amount_sats if decoded.amount_sats is None else None,
    )
    tx = _record_withdrawal(
        wallet=wallet, quote=quote, description=memo or "Lightning invoice withdrawal",
        lnd_invoice=decoded.payment_request, network="lightning",
    )
    return tx, {"provider": "blink", "rail": "lightning", "target": decoded.as_dict(), "fee": quote.as_dict(), "provider_result": result}


def _withdraw_lightning_address(wallet, decoded, quote, memo):
    result = BlinkWallet().pay_ln_address(decoded.lightning_address, quote.amount_sats)
    tx = _record_withdrawal(
        wallet=wallet, quote=quote, description=memo or "Lightning address withdrawal",
        onchain_address=decoded.lightning_address, network="lightning",
    )
    return tx, {"provider": "blink", "rail": "lightning", "target": decoded.as_dict(), "fee": quote.as_dict(), "provider_result": result}


def _withdraw_lnurl(wallet, decoded, quote, memo):
    result = BlinkWallet().pay_lnurl(decoded.lnurl, quote.amount_sats)
    tx = _record_withdrawal(
        wallet=wallet, quote=quote, description=memo or "LNURL withdrawal",
        onchain_address=decoded.lnurl, network="lightning",
    )
    return tx, {"provider": "blink", "rail": "lightning", "target": decoded.as_dict(), "fee": quote.as_dict(), "provider_result": result}


def _withdraw_bitcoin_address(wallet, decoded, quote, memo):
    minimum = int(getattr(SETTINGS, "MIN_WITHDRAWAL", 1000))
    if quote.amount_sats < minimum:
        raise ValueError(f"Minimum withdrawal is {minimum} sats.")
    # Debit (amount + platform fee), fee records, signing, broadcast and refund-on-failure
    # all happen inside the service, under a row lock on the wallet.
    tx, result = CustodialBitcoinService().withdraw(
        wallet, decoded.bitcoin_address, quote.amount_sats,
        platform_fee=quote.fee_sats if quote.charge_to_user else 0, memo=memo,
    )
    wallet.refresh_from_db()
    return tx, {"provider": "bitcoin", "rail": "bitcoin", "target": decoded.as_dict(), "fee": quote.as_dict(), "provider_result": result}


def _record_withdrawal(*, wallet: Wallet, quote: WithdrawalFeeQuote, description: str, lnd_invoice: str = "", onchain_address: str = "", network: str = "") -> WalletTransaction:
    wallet.available_balance -= quote.wallet_debit_sats
    wallet.total_withdrawn += quote.amount_sats
    wallet.save(update_fields=["available_balance", "total_withdrawn", "updated_at"])

    tx = WalletTransaction.objects.create(
        user=wallet.user, wallet=wallet, type=TransactionType.WITHDRAWAL,
        amount=quote.amount_sats, balance_after=wallet.available_balance,
        lnd_invoice=lnd_invoice, onchain_address=onchain_address, network=network,
        status=TransactionStatus.CONFIRMED, description=description, settled_at=timezone.now(),
    )
    if quote.charge_to_user:
        _create_fee_transaction(wallet, quote, f"{description} fee")
    return tx


def _create_fee_transaction(wallet: Wallet, quote: WithdrawalFeeQuote, description: str) -> None:
    if not quote.fee_sats:
        return
    platform_wallet = Wallet.get_platform_wallet()
    platform_wallet.available_balance += quote.fee_sats
    platform_wallet.total_deposited += quote.fee_sats
    platform_wallet.save(update_fields=["available_balance", "total_deposited", "updated_at"])

    WalletTransaction.objects.create(
        user=wallet.user, wallet=wallet, type=TransactionType.FEE,
        amount=quote.fee_sats, balance_after=wallet.available_balance,
        status=TransactionStatus.CONFIRMED, description=description,
        linked_object_type="wallet.WithdrawalFeePolicy", linked_object_id=str(quote.policy.pk),
        settled_at=timezone.now(),
    )
    WalletTransaction.objects.create(
        user=wallet.user, wallet=platform_wallet, type=TransactionType.FEE,
        amount=quote.fee_sats, balance_after=platform_wallet.available_balance,
        status=TransactionStatus.CONFIRMED, description=f"Platform {description}",
        linked_object_type="wallet.WithdrawalFeePolicy", linked_object_id=str(quote.policy.pk),
        settled_at=timezone.now(),
    )
