"""
Custodial on-chain Bitcoin service, built on `btclib` (pure-Python, no
native build) plus Esplora-compatible block explorer APIs (blockstream.info
/ mempool.space) for chain data and broadcast — btclib itself has no
address-indexed UTXO/balance lookup (see wallet/esplora_client.py's module
docstring for why).

Architecture:
  - One BIP32 HD wallet for the whole platform (`wallet/onchain_keys.py`):
    every address is a deterministic child key, not an independently
    generated one, so there is exactly one secret to back up and rotate.
  - Withdrawals build a legacy P2PKH transaction, sign each input, and
    locally re-run it through btclib's own consensus script engine
    (`verify_transaction`) before ever broadcasting — a signing bug raises
    an error instead of broadcasting an invalid/incorrect spend.
  - Coin selection scans every derived address's UTXOs on each withdrawal.
    Fine for a first version, but doesn't scale past a modest number of
    addresses — a production version should maintain a local UTXO cache
    updated by the deposit scanner instead.

SECURITY NOTE: this custodial hot-wallet model (the platform holds the one
root key that derives every address, encrypted with one symmetric key) is
standard for a simple custodial product but concentrates risk in
`WALLET_ENCRYPTION_KEY` and its backup. Get an independent security review
before handling real (mainnet) funds; default network is testnet
(`BITCOIN_NETWORK` in .env) specifically to make that mistake hard to make
by accident.
"""
from __future__ import annotations

import base64
import io
import logging

import qrcode
from django.db import transaction as db_transaction
from django.utils import timezone

from btclib.ecc.dsa import sign_
from btclib.script.script import serialize as script_serialize
from btclib.script.script_pub_key import ScriptPubKey
from btclib.script.sig_hash import legacy as legacy_sig_hash
from btclib.script.engine import verify_transaction
from btclib.tx.out_point import OutPoint
from btclib.tx.tx import Tx
from btclib.tx.tx_in import TxIn
from btclib.tx.tx_out import TxOut

from wallet import onchain_keys
from wallet.esplora_client import DUST_THRESHOLD_SATS, EsploraError, broadcast_tx, get_fee_rate_sat_per_vb, get_utxos
from wallet.models import (
    PlatformBitcoinAddress,
    TransactionStatus,
    TransactionType,
    Wallet as UserWallet,
    WalletTransaction,
)

logger = logging.getLogger(__name__)

SIGHASH_ALL = 1

# Rough legacy P2PKH size estimate: 148 bytes/input, 34 bytes/output, 10 bytes overhead.
BYTES_PER_INPUT = 148
BYTES_PER_OUTPUT = 34
TX_OVERHEAD_BYTES = 10


def _estimate_fee_sats(num_inputs: int, num_outputs: int, fee_rate_sat_per_vb: float) -> int:
    vsize = num_inputs * BYTES_PER_INPUT + num_outputs * BYTES_PER_OUTPUT + TX_OVERHEAD_BYTES
    return int(vsize * fee_rate_sat_per_vb)


class CustodialBitcoinService:
    """
    Custodial Bitcoin system:

    - On-chain BTC only for deposit/withdraw
    - Internal wallet is a SAT ledger
    - 0-3 conf = pending, 4+ conf = confirmed
    """

    def get_or_create_user_address(self, wallet: UserWallet) -> dict:
        if wallet.bitcoin_address:
            return {"address": wallet.bitcoin_address, "qr": self.generate_qr(wallet.bitcoin_address)}

        address_row = onchain_keys.create_address(label=f"user-{wallet.user_id}")
        wallet.bitcoin_address = address_row.address
        wallet.save(update_fields=["bitcoin_address"])

        return {"address": wallet.bitcoin_address, "qr": self.generate_qr(wallet.bitcoin_address)}

    def get_platform_balance(self) -> tuple[int, bool]:
        """Sum of confirmed balances across every address the platform has ever generated.

        Returns (total_sats, had_failures). had_failures=True means at least one
        address could not be checked (e.g. explorer unreachable) — the total is
        then an undercount, never a confirmed zero; the caller must not present
        it as if every address was successfully checked.
        """
        from wallet.esplora_client import get_address_balance

        total = 0
        had_failures = False
        for row in PlatformBitcoinAddress.objects.all().iterator():
            try:
                total += get_address_balance(row.address)
            except EsploraError as exc:
                had_failures = True
                logger.warning("Could not fetch balance for %s: %s", row.address, exc)
        return total, had_failures

    # ─────────────────────────────────────
    # SCAN CHAIN
    # ─────────────────────────────────────

    def scan_address_transactions(self, address: str) -> list[dict]:
        """Raises EsploraError if no configured provider could be reached — the caller decides
        how to handle that; it must never be silently swallowed into a false 'no deposits'."""
        utxos = get_utxos(address)
        return [
            {
                "txid": u["txid"],
                "address": address,
                "amount_sats": u["value"],
                "confirmations": u["confirmations"],
            }
            for u in utxos
        ]

    # ─────────────────────────────────────
    # PROCESS DEPOSITS (CORE LOGIC)
    # ─────────────────────────────────────

    def process_deposits(self, wallet: UserWallet) -> list[str]:
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

            with db_transaction.atomic():
                if conf < 4:
                    wallet.add_pending_balance(amount_sats)
                    WalletTransaction.objects.create(
                        user=wallet.user, wallet=wallet, type=TransactionType.DEPOSIT,
                        amount=amount_sats, status=TransactionStatus.PENDING, confirmations=conf,
                        onchain_txid=txid, onchain_address=address,
                        description=f"Deposit pending ({conf} conf)", balance_after=wallet.available_balance,
                    )
                else:
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

    def scan_all_users(self) -> dict:
        """Returns {"processed": [txid, ...], "failed": [{"address", "wallet_id", "error"}, ...]}.

        A per-address explorer failure (e.g. the only configured provider being
        unreachable) does not abort the whole batch, but it is never silently
        treated as "this address has no deposits" — that distinction matters:
        a real negative result and "we couldn't check" must stay visibly
        different to whoever is reading scan_bitcoin/worker's output.
        """
        wallets = UserWallet.objects.select_related("user")
        processed = []
        failed = []
        for wallet in wallets:
            if not wallet.bitcoin_address:
                address_row = onchain_keys.create_address(label=f"user-{wallet.user_id}")
                wallet.bitcoin_address = address_row.address
                wallet.save(update_fields=["bitcoin_address"])
            try:
                processed.extend(self.process_deposits(wallet))
            except EsploraError as exc:
                logger.warning("Could not scan %s (wallet %s): %s", wallet.bitcoin_address, wallet.pk, exc)
                failed.append({"wallet_id": str(wallet.pk), "address": wallet.bitcoin_address, "error": str(exc)})
        return {"processed": processed, "failed": failed}

    # ─────────────────────────────────────
    # WITHDRAW
    # ─────────────────────────────────────

    def _select_utxos(self, amount_sats: int, fee_rate: float) -> tuple[list[tuple[dict, PlatformBitcoinAddress]], int]:
        selected: list[tuple[dict, PlatformBitcoinAddress]] = []
        total = 0
        for row in PlatformBitcoinAddress.objects.all().iterator():
            try:
                utxos = get_utxos(row.address)
            except EsploraError:
                continue
            for u in utxos:
                if not u["confirmed"]:
                    continue
                selected.append((u, row))
                total += u["value"]
                fee = _estimate_fee_sats(len(selected), 2, fee_rate)
                if total >= amount_sats + fee:
                    return selected, total

        fee = _estimate_fee_sats(len(selected), 2, fee_rate)
        if total < amount_sats + fee:
            raise ValueError(
                f"Insufficient on-chain platform liquidity: found {total} sats across {len(selected)} confirmed "
                f"UTXO(s), need at least {amount_sats + fee} sats (amount + estimated network fee)."
            )
        return selected, total

    def withdraw(self, user, address: str, amount_sats: int) -> dict:
        wallet = user.wallet
        if wallet.available_balance < amount_sats:
            raise ValueError("Insufficient balance")

        dest_script_pub_key = ScriptPubKey.from_address(address)
        fee_rate = get_fee_rate_sat_per_vb()
        selected, total_in = self._select_utxos(amount_sats, fee_rate)

        fee = _estimate_fee_sats(len(selected), 2, fee_rate)
        change_sats = total_in - amount_sats - fee

        txins = [TxIn(OutPoint(bytes.fromhex(u["txid"]), u["vout"]), sequence=0xFFFFFFFF) for u, _row in selected]
        txouts = [TxOut(amount_sats, dest_script_pub_key)]

        if change_sats > DUST_THRESHOLD_SATS:
            change_row = onchain_keys.create_address(label="change")
            _scalar, change_spk = onchain_keys.load_signing_key(change_row)
            txouts.append(TxOut(change_sats, change_spk))
        # else: change_sats <= dust is folded into the miner fee, which is normal/expected.

        tx = Tx(1, 0, txins, txouts)

        prevout_txouts = []
        for i, (u, row) in enumerate(selected):
            scalar, owner_script_pub_key = onchain_keys.load_signing_key(row)
            sighash = legacy_sig_hash(owner_script_pub_key.script, tx, i, SIGHASH_ALL)
            sig = sign_(sighash, scalar)
            signature = sig.serialize() + bytes([SIGHASH_ALL])
            tx.vin[i].script_sig = script_serialize([signature, onchain_keys.pub_key_bytes(scalar, owner_script_pub_key.network)])
            prevout_txouts.append(TxOut(u["value"], owner_script_pub_key))

        # Re-verify locally before broadcasting: a signing bug raises here instead of
        # broadcasting a transaction that either fails or (worse) spends incorrectly.
        verify_transaction(prevout_txouts, tx)

        raw_tx_hex = tx.serialize(include_witness=False).hex()
        txid = broadcast_tx(raw_tx_hex)

        with db_transaction.atomic():
            wallet.available_balance -= amount_sats
            wallet.total_withdrawn += amount_sats
            wallet.save(update_fields=["available_balance", "total_withdrawn", "updated_at"])

            WalletTransaction.objects.create(
                user=user, wallet=wallet, type=TransactionType.WITHDRAWAL,
                amount=amount_sats, status=TransactionStatus.CONFIRMED,
                onchain_txid=txid, onchain_address=address, settled_at=timezone.now(),
                balance_after=wallet.available_balance, description="BTC withdrawal",
            )

        return {"txid": txid, "status": "broadcasted", "fee_sats": fee, "inputs": len(selected)}

    # ─────────────────────────────────────
    # QR CODE
    # ─────────────────────────────────────

    def generate_qr(self, data: str) -> str:
        qr = qrcode.QRCode(version=1, box_size=10, border=4)
        qr.add_data(data)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()
