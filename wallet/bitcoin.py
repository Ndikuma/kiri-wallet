import base64
import io
from decimal import Decimal
import requests

import qrcode

from django.db import transaction
from django.utils import timezone

try:
    from bitcoinlib.wallets import Wallet, wallet_exists
    from bitcoinlib.services.services import Service
except ImportError:
    Wallet = wallet_exists = Service = None

from wallet.models import (
    TransactionStatus,
    TransactionType,
    Wallet as UserWallet,
    WalletTransaction,
)

SATOSHI = Decimal("100000000")

BITCOINLIB_MISSING_MESSAGE = (
    "On-chain Bitcoin support is unavailable because `bitcoinlib` is not installed "
    "(its `coincurve` dependency currently has no prebuilt wheel for this Python "
    "version). Install it in a Python 3.11/3.12 environment to enable on-chain "
    "deposits/withdrawals. Lightning (Blink) deposits/withdrawals are unaffected."
)


class CustodialBitcoinService:
    """
    Custodial Bitcoin system:

    - On-chain BTC only for deposit/withdraw
    - Internal wallet is a SAT ledger
    - Fee split applied on deposits
    - 0-3 conf = pending
    - 4+ conf = confirmed
    """

    def __init__(self, wallet_name="platform_wallet", network="bitcoin"):
        if Wallet is None:
            raise RuntimeError(BITCOINLIB_MISSING_MESSAGE)

        self.wallet_name = wallet_name
        self.network = network

        if wallet_exists(wallet_name):
            self.wallet = Wallet(wallet_name)
        else:
            self.wallet = Wallet.create(
                wallet_name,
                network=network,
                witness_type="segwit",
                scheme="bip32",
            )

    # ─────────────────────────────────────
    # ADDRESS GENERATION
    # ─────────────────────────────────────

    def get_or_create_user_address(self, wallet: UserWallet):
        if wallet.bitcoin_address:
            return {"address": wallet.bitcoin_address, "qr": self.generate_qr(wallet.bitcoin_address)}

        key = self.wallet.new_key(name=f"user-{wallet.user_id}")

        wallet.bitcoin_address = key.address
        wallet.save(update_fields=["bitcoin_address"])

        return {"address": wallet.bitcoin_address, "qr": self.generate_qr(wallet.bitcoin_address)}

    def get_platform_balance(self) -> int:
        """
        Return the custodial on-chain wallet balance in sats.

        bitcoinlib can return balances as ints, Decimals, strings, or dict-like
        structures depending on provider/backend, so normalize defensively.
        """
        raw_balance = self.wallet.balance()
        if isinstance(raw_balance, dict):
            raw_balance = raw_balance.get("confirmed") or raw_balance.get("balance") or 0
        if isinstance(raw_balance, Decimal):
            if raw_balance < 1:
                return int(raw_balance * SATOSHI)
            return int(raw_balance)
        if isinstance(raw_balance, str):
            value = Decimal(raw_balance)
            if value < 1:
                return int(value * SATOSHI)
            return int(value)
        return int(raw_balance or 0)

    # ─────────────────────────────────────
    # SCAN CHAIN
    # ─────────────────────────────────────

    def scan_address_transactions(self, address: str):
        providers = self._get_providers(address)
        for provider in providers:
            try:
                utxos = provider()
                if utxos:
                    return utxos
            except Exception:
                continue
        return []

    def _get_providers(self, address: str):
        def mempool():
            base = "https://mempool.space/testnet/api" if self.network == "testnet4" else "https://mempool.space/api"
            r = requests.get(f"{base}/address/{address}/utxo", timeout=10)
            r.raise_for_status()
            data = r.json()
            return [
                {
                    "txid": x["txid"],
                    "address": address,
                    "amount_sats": x["value"],
                    "confirmations": x.get("status", {}).get("confirmations", 0),
                }
                for x in data
            ]

        def blockstream():
            base = "https://blockstream.info/testnet/api" if self.network == "testnet4" else "https://blockstream.info/api"
            r = requests.get(f"{base}/address/{address}/utxo", timeout=10)
            r.raise_for_status()
            data = r.json()
            return [
                {
                    "txid": x["txid"],
                    "address": address,
                    "amount_sats": x["value"],
                    "confirmations": x.get("status", {}).get("confirmations", 0),
                }
                for x in data
            ]

        def bitcoinlib_local():
            service = Service(network=self.network)
            txs = service.gettransactions(address)
            result = []
            for tx in txs:
                received = 0
                for o in tx.outputs:
                    if o.address == address:
                        received += o.value
                if received > 0:
                    result.append({
                        "txid": tx.txid,
                        "address": address,
                        "amount_sats": received,
                        "confirmations": tx.confirmations,
                    })
            return result

        return [mempool, blockstream, bitcoinlib_local]

    # ─────────────────────────────────────
    # PROCESS DEPOSITS (CORE LOGIC)
    # ─────────────────────────────────────

    def process_deposits(self, wallet: UserWallet):
        address = wallet.bitcoin_address
        if not address:
            return []

        deposits = self.scan_address_transactions(address)
        results = []

        for deposit in deposits:
            txid = deposit["txid"]
            conf = deposit["confirmations"]
            amount_sats = int(deposit["amount_sats"])

            if WalletTransaction.objects.filter(onchain_txid=txid, type=TransactionType.DEPOSIT).exists():
                continue

            with transaction.atomic():
                if conf == 0:
                    wallet.add_pending_balance(amount_sats)
                    WalletTransaction.objects.create(
                        user=wallet.user, wallet=wallet, type=TransactionType.DEPOSIT,
                        amount=amount_sats, status=TransactionStatus.PENDING, confirmations=0,
                        onchain_txid=txid, onchain_address=address,
                        description="Deposit detected (0 conf)", balance_after=wallet.available_balance,
                    )
                    results.append(txid)
                    continue

                if 1 <= conf < 4:
                    wallet.add_pending_balance(amount_sats)
                    WalletTransaction.objects.create(
                        user=wallet.user, wallet=wallet, type=TransactionType.DEPOSIT,
                        amount=amount_sats, status=TransactionStatus.PENDING, confirmations=conf,
                        onchain_txid=txid, onchain_address=address,
                        description="Deposit pending confirmations", balance_after=wallet.available_balance,
                    )
                    results.append(txid)
                    continue

                if conf >= 4:
                    wallet.available_balance += amount_sats
                    wallet.total_deposited += amount_sats
                    wallet.save(update_fields=["available_balance", "total_deposited", "updated_at"])

                    WalletTransaction.objects.create(
                        user=wallet.user, wallet=wallet, type=TransactionType.DEPOSIT,
                        amount=amount_sats, status=TransactionStatus.CONFIRMED, confirmations=conf,
                        onchain_txid=txid, onchain_address=address, settled_at=timezone.now(),
                        balance_after=wallet.available_balance, description="Deposit confirmed (4+ conf)",
                    )
                    results.append(txid)

        return results

    # ─────────────────────────────────────
    # SCAN ALL USERS
    # ─────────────────────────────────────

    def scan_all_users(self):
        wallets = UserWallet.objects.select_related("user")
        results = []
        for wallet in wallets:
            if not wallet.bitcoin_address:
                key = self.wallet.new_key(name=f"user-{wallet.user_id}")
                wallet.bitcoin_address = key.address
                wallet.save(update_fields=["bitcoin_address"])
            results.extend(self.process_deposits(wallet))
        return results

    # ─────────────────────────────────────
    # WITHDRAW
    # ─────────────────────────────────────

    def withdraw(self, user, address: str, amount_sats: int):
        wallet = user.wallet

        if wallet.available_balance < amount_sats:
            raise ValueError("Insufficient balance")

        amount_btc = Decimal(amount_sats) / SATOSHI

        with transaction.atomic():
            tx = self.wallet.send_to(address, float(amount_btc))

            wallet.available_balance -= amount_sats
            wallet.total_withdrawn += amount_sats
            wallet.save(update_fields=["available_balance", "total_withdrawn", "updated_at"])

            WalletTransaction.objects.create(
                user=user, wallet=wallet, type=TransactionType.WITHDRAWAL,
                amount=amount_sats, status=TransactionStatus.CONFIRMED,
                onchain_txid=tx.txid, onchain_address=address, settled_at=timezone.now(),
                balance_after=wallet.available_balance, description="BTC withdrawal",
            )

        return {"txid": tx.txid, "status": "broadcasted"}

    # ─────────────────────────────────────
    # QR CODE
    # ─────────────────────────────────────

    def generate_qr(self, data: str):
        qr = qrcode.QRCode(version=1, box_size=10, border=4)
        qr.add_data(data)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()
