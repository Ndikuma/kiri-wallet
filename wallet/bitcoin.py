"""
Custodial on-chain Bitcoin service, built on `btclib` (pure-Python, no
native build) plus Esplora-compatible block explorer APIs (blockstream.info
/ mempool.space) for chain data and broadcast — btclib itself has no
network I/O (see wallet/esplora_client.py).

Architecture:
  - One BIP32 HD wallet for the whole platform (`wallet/onchain_keys.py`):
    every address is a deterministic child key, so there is exactly one
    secret to back up and rotate. New addresses are native SegWit (P2WPKH)
    by default; legacy P2PKH addresses created earlier stay spendable.
  - A local UTXO cache (`BitcoinUTXO`), filled by the deposit scanner and by
    our own withdrawals' change outputs, drives coin selection. Withdrawals
    claim rows with SELECT ... FOR UPDATE and mark them RESERVED, so two
    concurrent withdrawals can never pick the same coins.
  - Deposits are tracked per output (txid:vout): pending until
    BITCOIN_DEPOSIT_CONFIRMATIONS, then promoted to the spendable balance on
    a later scan. Only credited deposit outputs are ever spent, so a deposit
    can't disappear from the UTXO set before it is promoted.
  - Withdrawals debit the user *before* anything is broadcast, build and
    sign the transaction, re-run it through btclib's consensus script engine
    (`verify_transaction`), store the raw transaction, and only then
    broadcast. A definitive rejection refunds the user; an ambiguous
    failure (explorer unreachable mid-broadcast) leaves the withdrawal
    pending and the tracker rebroadcasts the stored transaction.

SECURITY NOTE: this custodial hot-wallet model (the platform holds the one
root key that derives every address, encrypted with one symmetric key) is
standard for a simple custodial product but concentrates risk in
`WALLET_ENCRYPTION_KEY` and its backup. Get an independent security review
before handling real (mainnet) funds; default network is testnet4
(`BITCOIN_NETWORK` in .env) specifically to make that mistake hard to make
by accident.
"""
from __future__ import annotations

import base64
import io
import logging
import math
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation

import qrcode
from django.conf import settings
from django.db import transaction as db_transaction
from django.db.models import Q, Sum
from django.utils import timezone

from btclib.ecc.dsa import sign_
from btclib.script import sig_hash
from btclib.script.engine import verify_transaction
from btclib.script.script import serialize as script_serialize
from btclib.script.script_pub_key import ScriptPubKey
from btclib.script.witness import Witness
from btclib.tx.out_point import OutPoint
from btclib.tx.tx import Tx
from btclib.tx.tx_in import TxIn
from btclib.tx.tx_out import TxOut

from wallet import onchain_keys
from wallet.esplora_client import (
    DUST_THRESHOLD_SATS,
    EsploraError,
    EsploraNotFound,
    EsploraRejected,
    broadcast_tx,
    get_fee_rate_sat_per_vb,
    get_tip_height,
    get_tx_status,
    get_utxos,
)
from wallet.models import (
    BitcoinAddressPurpose,
    BitcoinScriptType,
    BitcoinUTXO,
    BitcoinUTXOStatus,
    PlatformBitcoinAddress,
    TransactionStatus,
    TransactionType,
    Wallet,
    WalletTransaction,
)

logger = logging.getLogger(__name__)

SIGHASH_ALL = 1
# Opt in to replace-by-fee (BIP125), so a stuck withdrawal can be fee-bumped.
RBF_SEQUENCE = 0xFFFFFFFD
SATS_PER_BTC = Decimal("100000000")

# Worst-case input sizes in weight units (vbytes = weight / 4), assuming a
# 73-byte DER signature + sighash byte and a 33-byte compressed public key.
_INPUT_WEIGHT = {
    BitcoinScriptType.P2PKH: 149 * 4,          # 32 txid + 4 vout + 1 + 108 scriptSig + 4 sequence
    BitcoinScriptType.P2WPKH: 41 * 4 + 109,    # non-witness part + witness (count, sig, pubkey)
}
_TX_BASE_WEIGHT = 10 * 4      # version, input count, output count, locktime
_SEGWIT_MARKER_WEIGHT = 2     # marker + flag bytes, only when any input is SegWit


class BitcoinWithdrawalError(ValueError):
    """A withdrawal that was refused or failed before any coins left the platform."""


def _setting(name: str, default):
    return getattr(settings, name, default)


def deposit_confirmations_required() -> int:
    return int(_setting("BITCOIN_DEPOSIT_CONFIRMATIONS", 4))


def withdrawal_confirmations_required() -> int:
    return int(_setting("BITCOIN_WITHDRAWAL_CONFIRMATIONS", 1))


def estimate_vsize(input_types: list[str], outputs: list[ScriptPubKey]) -> int:
    weight = _TX_BASE_WEIGHT + sum(_INPUT_WEIGHT[t] for t in input_types)
    weight += sum((8 + 1 + len(spk.script)) * 4 for spk in outputs)
    if any(t == BitcoinScriptType.P2WPKH for t in input_types):
        weight += _SEGWIT_MARKER_WEIGHT
    return math.ceil(weight / 4)


def fee_for(input_types: list[str], outputs: list[ScriptPubKey], fee_rate: float) -> int:
    return math.ceil(estimate_vsize(input_types, outputs) * fee_rate)


def _placeholder_change_spk() -> ScriptPubKey:
    """A script of the same size as our change output, for fee estimation only."""
    if onchain_keys.default_script_type() == BitcoinScriptType.P2WPKH:
        return ScriptPubKey(script_serialize(["OP_0", b"\x00" * 20]))
    return ScriptPubKey(script_serialize(["OP_DUP", "OP_HASH160", b"\x00" * 20, "OP_EQUALVERIFY", "OP_CHECKSIG"]))


def current_fee_rate() -> float:
    """Explorer fee estimate for BITCOIN_FEE_TARGET_BLOCKS, clamped to the configured bounds."""
    rate = get_fee_rate_sat_per_vb(int(_setting("BITCOIN_FEE_TARGET_BLOCKS", 6)))
    low = float(_setting("BITCOIN_MIN_FEE_RATE", 1))
    high = float(_setting("BITCOIN_MAX_FEE_RATE", 500))
    return min(max(rate, low), high)


def validate_destination(address: str) -> ScriptPubKey:
    """Parse a withdrawal address and check it belongs to the configured network."""
    try:
        script_pub_key = ScriptPubKey.from_address(address)
    except Exception as exc:  # btclib raises several error types for bad checksums/lengths
        raise BitcoinWithdrawalError(f"Not a valid Bitcoin address: {exc}") from exc

    network = _setting("BITCOIN_NETWORK", "testnet4")
    allowed = {"mainnet"} if network == "mainnet" else {"testnet", "regtest"}
    if script_pub_key.network not in allowed:
        raise BitcoinWithdrawalError(
            f"That is a {script_pub_key.network} address, but this wallet runs on {network}."
        )
    return script_pub_key


def parse_bip21(uri: str) -> tuple[str, int | None]:
    """Split a `bitcoin:<address>?amount=<btc>` URI into (address, amount_sats)."""
    body = uri.split(":", 1)[1] if uri.lower().startswith("bitcoin:") else uri
    address, _, query = body.partition("?")
    amount_sats = None
    for part in query.split("&"):
        key, _, value = part.partition("=")
        if key.lower() == "amount" and value:
            try:
                amount_sats = int((Decimal(value) * SATS_PER_BTC).to_integral_value())
            except InvalidOperation as exc:
                raise ValueError(f"Invalid amount in Bitcoin URI: {value!r}") from exc
    return address.strip(), amount_sats


@dataclass
class CoinSelection:
    utxos: list[BitcoinUTXO]
    network_fee: int
    change: int
    fee_rate: float


class CustodialBitcoinService:
    """
    Custodial Bitcoin system:

    - On-chain BTC only for deposit/withdraw
    - Internal wallet is a SAT ledger
    - Deposits: pending below BITCOIN_DEPOSIT_CONFIRMATIONS, spendable after
    """

    # ─────────────────────────────────────
    # ADDRESSES
    # ─────────────────────────────────────

    def get_or_create_user_address(self, wallet: Wallet) -> dict:
        if not wallet.bitcoin_address:
            return self.new_user_address(wallet)

        # Addresses created before deposit addresses were linked to wallets.
        PlatformBitcoinAddress.objects.filter(address=wallet.bitcoin_address, wallet__isnull=True).update(wallet=wallet)
        return self._address_payload(wallet.bitcoin_address)

    def new_user_address(self, wallet: Wallet) -> dict:
        """Give the wallet a fresh deposit address. Earlier addresses stay linked
        to the wallet and keep being scanned, so late deposits to them still count."""
        row = onchain_keys.create_address(
            label=f"user-{wallet.user_id}", purpose=BitcoinAddressPurpose.DEPOSIT, wallet=wallet,
        )
        wallet.bitcoin_address = row.address
        wallet.save(update_fields=["bitcoin_address", "updated_at"])
        return self._address_payload(row.address, row)

    def _address_payload(self, address: str, row: PlatformBitcoinAddress | None = None) -> dict:
        row = row or PlatformBitcoinAddress.objects.filter(address=address).first()
        return {
            "address": address,
            "qr": self.generate_qr(f"bitcoin:{address}"),
            "script_type": row.script_type if row else "",
            "network": _setting("BITCOIN_NETWORK", "testnet4"),
            "confirmations_required": deposit_confirmations_required(),
        }

    # ─────────────────────────────────────
    # BALANCES
    # ─────────────────────────────────────

    def cached_balance(self) -> dict:
        """Platform on-chain holdings according to the local UTXO cache (no network calls)."""
        unspent = BitcoinUTXO.objects.filter(status=BitcoinUTXOStatus.UNSPENT)
        reserved = BitcoinUTXO.objects.filter(status=BitcoinUTXOStatus.RESERVED)
        return {
            "confirmed_sats": unspent.filter(block_height__isnull=False).aggregate(s=Sum("value"))["s"] or 0,
            "unconfirmed_sats": unspent.filter(block_height__isnull=True).aggregate(s=Sum("value"))["s"] or 0,
            "reserved_sats": reserved.aggregate(s=Sum("value"))["s"] or 0,
            "utxo_count": unspent.count(),
        }

    def get_platform_balance(self) -> tuple[int, bool]:
        """Sum of confirmed balances across every address the platform has ever generated,
        straight from the explorer — for reconciling against `cached_balance()`.

        Returns (total_sats, had_failures). had_failures=True means at least one
        address could not be checked (e.g. explorer unreachable) — the total is
        then an undercount, never a confirmed zero.
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
    # DEPOSIT SCANNING
    # ─────────────────────────────────────

    def _addresses_to_scan(self):
        """Every deposit address, plus change addresses still waiting for their first confirmation."""
        return (
            PlatformBitcoinAddress.objects
            .filter(
                Q(purpose=BitcoinAddressPurpose.DEPOSIT)
                | Q(utxos__status=BitcoinUTXOStatus.UNSPENT, utxos__block_height__isnull=True)
            )
            .distinct()
            .order_by("derivation_index")
        )

    def scan_address(self, row: PlatformBitcoinAddress) -> dict:
        """Sync one address with the explorer. Raises EsploraError if it couldn't be checked —
        that must never be mistaken for "no deposits"."""
        result = {"new": [], "promoted": [], "dropped": []}
        utxos = get_utxos(row.address)
        seen = set()

        for u in utxos:
            seen.add((u["txid"], u["vout"]))
            self._sync_utxo(row, u)
            if row.purpose == BitcoinAddressPurpose.DEPOSIT:
                outcome = self._record_deposit(row, u)
                if outcome:
                    result[outcome].append(f"{u['txid']}:{u['vout']}")

        for cached in row.utxos.filter(status=BitcoinUTXOStatus.UNSPENT):
            if (cached.txid, cached.vout) not in seen and self._handle_missing_utxo(cached):
                result["dropped"].append(f"{cached.txid}:{cached.vout}")
        return result

    def _sync_utxo(self, row: PlatformBitcoinAddress, u: dict) -> None:
        with db_transaction.atomic():
            utxo, created = BitcoinUTXO.objects.select_for_update().get_or_create(
                txid=u["txid"], vout=u["vout"],
                defaults={"address": row, "value": u["value"], "block_height": u["block_height"]},
            )
            if created:
                return
            fields = []
            if utxo.block_height != u["block_height"]:
                utxo.block_height = u["block_height"]  # confirmed, or unconfirmed again after a reorg
                fields.append("block_height")
            if utxo.status == BitcoinUTXOStatus.DROPPED:
                utxo.status = BitcoinUTXOStatus.UNSPENT  # it came back (e.g. rebroadcast by the sender)
                fields.append("status")
            if fields:
                utxo.save(update_fields=fields + ["updated_at"])

    def _record_deposit(self, row: PlatformBitcoinAddress, u: dict) -> str | None:
        """Create or advance the ledger entry for one deposit output. Returns
        "new", "promoted" or None (nothing changed worth reporting)."""
        if row.wallet_id is None:
            logger.warning("Deposit %s:%s to %s has no owning wallet; not credited.", u["txid"], u["vout"], row.address)
            return None

        required = deposit_confirmations_required()
        conf = int(u["confirmations"])
        amount = int(u["value"])
        now = timezone.now()

        with db_transaction.atomic():
            wallet = Wallet.objects.select_for_update().get(pk=row.wallet_id)
            tx = (
                WalletTransaction.objects.select_for_update()
                .filter(type=TransactionType.DEPOSIT, onchain_txid=u["txid"])
                .filter(Q(onchain_vout=u["vout"]) | Q(onchain_vout__isnull=True, onchain_address=row.address))
                .first()
            )

            if tx is None:
                confirmed = conf >= required
                if confirmed:
                    wallet.available_balance += amount
                    wallet.total_deposited += amount
                else:
                    wallet.pending_balance += amount
                wallet.save(update_fields=["available_balance", "pending_balance", "total_deposited", "updated_at"])
                WalletTransaction.objects.create(
                    user=wallet.user, wallet=wallet, type=TransactionType.DEPOSIT, amount=amount,
                    status=TransactionStatus.CONFIRMED if confirmed else TransactionStatus.PENDING,
                    confirmations=conf, onchain_txid=u["txid"], onchain_vout=u["vout"],
                    onchain_address=row.address, network=_setting("BITCOIN_NETWORK", "testnet4"),
                    settled_at=now if confirmed else None, balance_after=wallet.available_balance,
                    description=self._deposit_description(conf, required),
                )
                return "new"

            fields = []
            outcome = None
            if tx.onchain_vout is None:  # recorded before deposits were tracked per output
                tx.onchain_vout = u["vout"]
                fields.append("onchain_vout")

            if tx.status == TransactionStatus.PENDING and conf >= required:
                if wallet.pending_balance < tx.amount:
                    logger.error("Wallet %s pending_balance %s < deposit %s; clamping to 0.", wallet.pk, wallet.pending_balance, tx.amount)
                wallet.pending_balance = max(wallet.pending_balance - tx.amount, 0)
                wallet.available_balance += tx.amount
                wallet.total_deposited += tx.amount
                wallet.save(update_fields=["available_balance", "pending_balance", "total_deposited", "updated_at"])
                tx.status = TransactionStatus.CONFIRMED
                tx.settled_at = now
                tx.balance_after = wallet.available_balance
                fields += ["status", "settled_at", "balance_after"]
                outcome = "promoted"

            if tx.confirmations != conf and (tx.status == TransactionStatus.PENDING or outcome):
                tx.confirmations = conf
                tx.description = self._deposit_description(conf, required)
                fields += ["confirmations", "description"]
            if fields:
                tx.save(update_fields=fields)
            return outcome

    @staticmethod
    def _deposit_description(conf: int, required: int) -> str:
        if conf >= required:
            return "On-chain deposit confirmed"
        return f"On-chain deposit pending ({conf}/{required} confirmations)"

    def _handle_missing_utxo(self, utxo: BitcoinUTXO) -> bool:
        """An output we hold as unspent is no longer listed by the explorer. Returns True if
        it was treated as dropped (and its pending deposit cancelled)."""
        try:
            get_tx_status(utxo.txid)
        except EsploraNotFound:
            pass
        except EsploraError as exc:
            logger.warning("Could not check missing output %s:%s: %s", utxo.txid, utxo.vout, exc)
            return False
        else:
            # The transaction exists, so the output was spent — but not by us (our own
            # spends are RESERVED first). Only possible if the key is used elsewhere.
            logger.error("Output %s:%s was spent outside this platform. Marking it spent.", utxo.txid, utxo.vout)
            utxo.status = BitcoinUTXOStatus.SPENT
            utxo.save(update_fields=["status", "updated_at"])
            return False

        if utxo.block_height is not None:
            logger.error("Confirmed output %s:%s vanished from the chain (deep reorg?). Needs manual review.", utxo.txid, utxo.vout)
            return False

        grace = timedelta(hours=int(_setting("BITCOIN_DROPPED_TX_GRACE_HOURS", 24)))
        if timezone.now() - utxo.first_seen_at < grace:
            return False  # may just not have propagated to this provider yet

        with db_transaction.atomic():
            utxo.status = BitcoinUTXOStatus.DROPPED
            utxo.save(update_fields=["status", "updated_at"])
            deposit = (
                WalletTransaction.objects.select_for_update()
                .filter(type=TransactionType.DEPOSIT, status=TransactionStatus.PENDING,
                        onchain_txid=utxo.txid, onchain_vout=utxo.vout)
                .first()
            )
            if deposit:
                wallet = Wallet.objects.select_for_update().get(pk=deposit.wallet_id)
                wallet.pending_balance = max(wallet.pending_balance - deposit.amount, 0)
                wallet.save(update_fields=["pending_balance", "updated_at"])
                deposit.status = TransactionStatus.FAILED
                deposit.description = "On-chain deposit dropped before confirming (replaced or evicted from the mempool)"
                deposit.save(update_fields=["status", "description"])
        logger.warning("Unconfirmed output %s:%s was dropped from the mempool.", utxo.txid, utxo.vout)
        return True

    def process_deposits(self, wallet: Wallet) -> list[str]:
        """Scan every deposit address of one wallet; returns newly seen deposit outputs."""
        new = []
        for row in PlatformBitcoinAddress.objects.filter(wallet=wallet, purpose=BitcoinAddressPurpose.DEPOSIT):
            new.extend(self.scan_address(row)["new"])
        return new

    def scan_all_users(self) -> dict:
        """One full on-chain sync: scan addresses, then track pending withdrawals.

        Returns {"processed": [...], "promoted": [...], "dropped": [...],
        "withdrawals": {...}, "failed": [{"address", "wallet_id", "error"}, ...]}.
        A per-address explorer failure does not abort the batch, but it is never
        silently treated as "this address has no deposits".
        """
        processed, promoted, dropped, failed = [], [], [], []
        for row in self._addresses_to_scan().iterator():
            try:
                result = self.scan_address(row)
            except EsploraError as exc:
                logger.warning("Could not scan %s: %s", row.address, exc)
                failed.append({"wallet_id": str(row.wallet_id or ""), "address": row.address, "error": str(exc)})
                continue
            processed += result["new"]
            promoted += result["promoted"]
            dropped += result["dropped"]

        return {
            "processed": processed, "promoted": promoted, "dropped": dropped, "failed": failed,
            "withdrawals": self.track_withdrawals(),
        }

    # ─────────────────────────────────────
    # WITHDRAW
    # ─────────────────────────────────────

    def _eligible_utxos(self, tip: int, lock: bool) -> list[BitcoinUTXO]:
        """Spendable coins: confirmed change, and deposits that have been credited."""
        qs = (
            BitcoinUTXO.objects.filter(status=BitcoinUTXOStatus.UNSPENT, block_height__isnull=False)
            .select_related("address").order_by("-value", "txid", "vout")
        )
        if lock:
            qs = qs.select_for_update()
        required = deposit_confirmations_required()
        return [
            u for u in qs
            if u.confirmations(tip) >= (required if u.address.purpose == BitcoinAddressPurpose.DEPOSIT else 1)
        ]

    def select_coins(self, amount: int, dest: ScriptPubKey, fee_rate: float, tip: int, lock: bool = False) -> CoinSelection:
        """Largest-first coin selection, with a change output when the leftover is above dust."""
        change_spk = _placeholder_change_spk()
        selected: list[BitcoinUTXO] = []
        total = 0
        for utxo in self._eligible_utxos(tip, lock):
            selected.append(utxo)
            total += utxo.value
            fee_with_change = fee_for([u.address.script_type for u in selected], [dest, change_spk], fee_rate)
            if total < amount + fee_with_change:
                continue
            change = total - amount - fee_with_change
            if change > DUST_THRESHOLD_SATS:
                return CoinSelection(selected, fee_with_change, change, fee_rate)
            # Leftover is too small for a change output: it goes to the miner.
            return CoinSelection(selected, total - amount, 0, fee_rate)

        needed = amount + fee_for([onchain_keys.default_script_type()] * max(len(selected), 1), [dest, change_spk], fee_rate)
        raise BitcoinWithdrawalError(
            f"Insufficient on-chain liquidity: {total} sats in {len(selected)} spendable coin(s), "
            f"need about {needed} sats (amount + network fee). Try a Lightning withdrawal or a smaller amount."
        )

    def quote_withdrawal(self, address: str, amount: int) -> dict:
        """Read-only network-fee estimate for a withdrawal (no coins are reserved)."""
        dest = validate_destination(address)
        fee_rate = current_fee_rate()
        selection = self.select_coins(amount, dest, fee_rate, get_tip_height(), lock=False)
        return {
            "fee_rate_sat_per_vb": fee_rate,
            "network_fee_sats": selection.network_fee,
            "inputs": len(selection.utxos),
            "has_change": bool(selection.change),
        }

    def withdraw(self, wallet: Wallet, address: str, amount: int, platform_fee: int = 0, memo: str = "") -> tuple[WalletTransaction, dict]:
        """Send `amount` sats on-chain, debiting `amount + platform_fee` from the wallet.

        The network (miner) fee is paid by the platform out of its on-chain coins
        and recorded on the transaction as `network_fee_sats`.
        """
        if amount < DUST_THRESHOLD_SATS:
            raise BitcoinWithdrawalError(f"Amount is below the {DUST_THRESHOLD_SATS}-sat dust limit.")
        dest = validate_destination(address)
        try:
            fee_rate = current_fee_rate()
            tip = get_tip_height()
        except EsploraError as exc:
            raise BitcoinWithdrawalError(f"Bitcoin network is unreachable right now, nothing was sent: {exc}") from exc

        withdrawal, change = self._reserve(wallet, address, dest, amount, platform_fee, fee_rate, tip, memo)
        utxos = list(withdrawal.reserved_utxos.select_related("address").order_by("txid", "vout"))

        try:
            tx = self._build_and_sign(withdrawal, utxos, dest, change)
        except Exception as exc:
            logger.exception("Signing withdrawal %s failed", withdrawal.pk)
            self.refund_withdrawal(withdrawal, f"could not sign: {exc}")
            raise BitcoinWithdrawalError("Could not build the withdrawal transaction; your balance was not charged.") from exc

        withdrawal.onchain_txid = tx.id.hex()
        withdrawal.onchain_raw_tx = tx.serialize(include_witness=True).hex()
        withdrawal.save(update_fields=["onchain_txid", "onchain_raw_tx"])

        try:
            broadcast_tx(withdrawal.onchain_raw_tx)
        except EsploraRejected as exc:
            self.refund_withdrawal(withdrawal, f"rejected by the network: {exc}")
            raise BitcoinWithdrawalError(f"The network rejected the withdrawal; your balance was refunded. ({exc})") from exc
        except EsploraError as exc:
            # We can't tell whether it reached the network. Keep it pending: the
            # tracker rebroadcasts the stored transaction until it is seen.
            logger.warning("Broadcast of %s is uncertain, will retry: %s", withdrawal.onchain_txid, exc)
            return withdrawal, {"txid": withdrawal.onchain_txid, "status": "broadcast_pending", "fee_sats": withdrawal.network_fee_sats}

        self._mark_broadcast(withdrawal)
        return withdrawal, {
            "txid": withdrawal.onchain_txid, "status": "broadcasted", "fee_sats": withdrawal.network_fee_sats,
            "fee_rate_sat_per_vb": fee_rate, "inputs": len(utxos),
        }

    def _reserve(self, wallet, address, dest, amount, platform_fee, fee_rate, tip, memo):
        """Atomically debit the user, record the withdrawal and claim its coins.
        Returns (withdrawal, (change_address_row, change_value) | None)."""
        debit = amount + platform_fee
        with db_transaction.atomic():
            wallet = Wallet.objects.select_for_update().get(pk=wallet.pk)
            if wallet.available_balance < debit:
                raise BitcoinWithdrawalError(f"Insufficient balance. Available: {wallet.available_balance:,} sats.")
            selection = self.select_coins(amount, dest, fee_rate, tip, lock=True)

            wallet.available_balance -= debit
            wallet.total_withdrawn += amount
            wallet.save(update_fields=["available_balance", "total_withdrawn", "updated_at"])

            withdrawal = WalletTransaction.objects.create(
                user=wallet.user, wallet=wallet, type=TransactionType.WITHDRAWAL, amount=amount,
                status=TransactionStatus.PENDING, onchain_address=address,
                network=_setting("BITCOIN_NETWORK", "testnet4"), network_fee_sats=selection.network_fee,
                balance_after=wallet.available_balance, description=memo or "Bitcoin withdrawal",
            )
            if platform_fee:
                self._record_platform_fee(wallet, withdrawal, platform_fee)

            BitcoinUTXO.objects.filter(pk__in=[u.pk for u in selection.utxos]).update(
                status=BitcoinUTXOStatus.RESERVED, reserved_by=withdrawal, updated_at=timezone.now(),
            )
            change = None
            if selection.change:
                change_row = onchain_keys.create_address(label="change", purpose=BitcoinAddressPurpose.CHANGE)
                change = (change_row, selection.change)
        return withdrawal, change

    def _record_platform_fee(self, wallet: Wallet, withdrawal: WalletTransaction, fee: int) -> None:
        platform = Wallet.objects.select_for_update().get(pk=Wallet.get_platform_wallet().pk)
        platform.available_balance += fee
        platform.total_deposited += fee
        platform.save(update_fields=["available_balance", "total_deposited", "updated_at"])
        link = {"linked_object_type": "wallet.WalletTransaction", "linked_object_id": str(withdrawal.pk)}
        now = timezone.now()
        WalletTransaction.objects.create(
            user=wallet.user, wallet=wallet, type=TransactionType.FEE, amount=fee,
            balance_after=wallet.available_balance, status=TransactionStatus.CONFIRMED,
            description="Bitcoin withdrawal fee", settled_at=now, **link,
        )
        WalletTransaction.objects.create(
            user=wallet.user, wallet=platform, type=TransactionType.FEE, amount=fee,
            balance_after=platform.available_balance, status=TransactionStatus.CONFIRMED,
            description="Platform Bitcoin withdrawal fee", settled_at=now, **link,
        )

    def _build_and_sign(self, withdrawal: WalletTransaction, utxos: list[BitcoinUTXO], dest: ScriptPubKey, change) -> Tx:
        txins = [TxIn(OutPoint(bytes.fromhex(u.txid), u.vout), sequence=RBF_SEQUENCE) for u in utxos]
        txouts = [TxOut(withdrawal.amount, dest)]
        if change:
            change_row, change_value = change
            _scalar, _pub, change_spk = onchain_keys.load_signing_key(change_row)
            txouts.append(TxOut(change_value, change_spk))
        tx = Tx(2, 0, txins, txouts)

        prevouts, keys = [], []
        for u in utxos:
            scalar, pub_key, spk = onchain_keys.load_signing_key(u.address)
            prevouts.append(TxOut(u.value, spk))
            keys.append((scalar, pub_key, spk, u.address.script_type))

        for i, (scalar, pub_key, spk, script_type) in enumerate(keys):
            if script_type == BitcoinScriptType.P2WPKH:
                script_code = ScriptPubKey.p2pkh(pub_key, compressed=True, network=spk.network).script
                digest = sig_hash.segwit_v0(script_code, tx, i, SIGHASH_ALL, prevouts[i].value)
                tx.vin[i].script_witness = Witness([sign_(digest, scalar).serialize() + bytes([SIGHASH_ALL]), pub_key])
            else:
                digest = sig_hash.legacy(spk.script, tx, i, SIGHASH_ALL)
                tx.vin[i].script_sig = script_serialize([sign_(digest, scalar).serialize() + bytes([SIGHASH_ALL]), pub_key])

        # Re-verify locally before broadcasting: a signing bug raises here instead of
        # broadcasting a transaction that either fails or (worse) spends incorrectly.
        verify_transaction(prevouts, tx)

        fee_paid = sum(p.value for p in prevouts) - sum(o.value for o in tx.vout)
        if fee_paid != withdrawal.network_fee_sats:
            raise RuntimeError(f"Fee mismatch: inputs - outputs = {fee_paid}, expected {withdrawal.network_fee_sats}.")
        if tx.vsize > estimate_vsize([k[3] for k in keys], [o.script_pub_key for o in tx.vout]):
            raise RuntimeError("Signed transaction is larger than estimated; its fee rate would be too low.")
        return tx

    def _mark_broadcast(self, withdrawal: WalletTransaction) -> None:
        """Record that the withdrawal's transaction is on the network (idempotent):
        its inputs are spent and its change output becomes a (pending) platform coin."""
        txid = withdrawal.onchain_txid
        tx = Tx.parse(bytes.fromhex(withdrawal.onchain_raw_tx))
        # A parsed transaction doesn't know its network (btclib renders it as mainnet),
        # so re-render each output's address for the network we actually run on.
        network = onchain_keys.address_network_class()
        outputs = {
            vout: (ScriptPubKey(out.script_pub_key.script, network).address, out.value)
            for vout, out in enumerate(tx.vout)
            if out.script_pub_key.type in (BitcoinScriptType.P2PKH, BitcoinScriptType.P2WPKH)
        }
        with db_transaction.atomic():
            BitcoinUTXO.objects.filter(reserved_by=withdrawal, status=BitcoinUTXOStatus.RESERVED).update(
                status=BitcoinUTXOStatus.SPENT, spent_by_txid=txid, updated_at=timezone.now(),
            )
            change_rows = {
                row.address: row for row in PlatformBitcoinAddress.objects.filter(
                    purpose=BitcoinAddressPurpose.CHANGE, address__in=[a for a, _v in outputs.values()],
                )
            }
            for vout, (address, value) in outputs.items():
                row = change_rows.get(address)
                if row:
                    BitcoinUTXO.objects.get_or_create(
                        txid=txid, vout=vout, defaults={"address": row, "value": value, "block_height": None},
                    )

    def refund_withdrawal(self, withdrawal: WalletTransaction, reason: str) -> bool:
        """Undo a withdrawal that never reached the network: credit the user back
        (amount + platform fee), reverse the fee and release its coins.
        Returns False if the withdrawal was no longer pending (nothing done)."""
        with db_transaction.atomic():
            withdrawal = WalletTransaction.objects.select_for_update().get(pk=withdrawal.pk)
            if withdrawal.status != TransactionStatus.PENDING:
                return False
            wallet = Wallet.objects.select_for_update().get(pk=withdrawal.wallet_id)
            fees = list(WalletTransaction.objects.select_for_update().filter(
                type=TransactionType.FEE, linked_object_type="wallet.WalletTransaction",
                linked_object_id=str(withdrawal.pk), status=TransactionStatus.CONFIRMED,
            ))
            user_fee = sum(f.amount for f in fees if f.wallet_id == wallet.pk)
            wallet.available_balance += withdrawal.amount + user_fee
            wallet.total_withdrawn = max(wallet.total_withdrawn - withdrawal.amount, 0)
            wallet.save(update_fields=["available_balance", "total_withdrawn", "updated_at"])

            for fee in fees:
                fee.status = TransactionStatus.REFUNDED
                fee.save(update_fields=["status"])
                if fee.wallet_id != wallet.pk:
                    platform = Wallet.objects.select_for_update().get(pk=fee.wallet_id)
                    platform.available_balance -= fee.amount
                    platform.total_deposited = max(platform.total_deposited - fee.amount, 0)
                    platform.save(update_fields=["available_balance", "total_deposited", "updated_at"])

            BitcoinUTXO.objects.filter(reserved_by=withdrawal, status=BitcoinUTXOStatus.RESERVED).update(
                status=BitcoinUTXOStatus.UNSPENT, reserved_by=None, updated_at=timezone.now(),
            )
            withdrawal.status = TransactionStatus.REFUNDED
            withdrawal.balance_after = wallet.available_balance
            withdrawal.description = f"{withdrawal.description} — refunded ({reason})"[:1000]
            withdrawal.save(update_fields=["status", "balance_after", "description"])
        logger.warning("Refunded on-chain withdrawal %s: %s", withdrawal.pk, reason)
        return True

    def track_withdrawals(self) -> dict:
        """Advance pending on-chain withdrawals: count confirmations, rebroadcast ones the
        explorer doesn't know about, and mark them confirmed once deep enough."""
        summary = {"confirmed": [], "rebroadcast": [], "unresolved": []}
        pending = list(WalletTransaction.objects.filter(
            type=TransactionType.WITHDRAWAL, status=TransactionStatus.PENDING,
        ).exclude(onchain_raw_tx=""))
        if not pending:
            return summary

        try:
            tip = get_tip_height()
        except EsploraError as exc:
            logger.warning("Cannot track withdrawals, explorer unreachable: %s", exc)
            return summary
        required = withdrawal_confirmations_required()

        for withdrawal in pending:
            try:
                status = get_tx_status(withdrawal.onchain_txid)
            except EsploraNotFound:
                try:
                    broadcast_tx(withdrawal.onchain_raw_tx)
                except EsploraError as exc:
                    # Never auto-refund here: an earlier broadcast may still confirm.
                    logger.error("Withdrawal %s (%s) is not on the network and rebroadcast failed: %s. "
                                 "Refund it from the admin once you're sure it can't confirm.",
                                 withdrawal.pk, withdrawal.onchain_txid, exc)
                    summary["unresolved"].append(withdrawal.onchain_txid)
                else:
                    self._mark_broadcast(withdrawal)
                    summary["rebroadcast"].append(withdrawal.onchain_txid)
                continue
            except EsploraError as exc:
                logger.warning("Could not check withdrawal %s: %s", withdrawal.onchain_txid, exc)
                continue

            self._mark_broadcast(withdrawal)
            conf = max(tip - status["block_height"] + 1, 0) if status["confirmed"] else 0
            fields = []
            if conf != withdrawal.confirmations:
                withdrawal.confirmations = conf
                fields.append("confirmations")
            if conf >= required:
                withdrawal.status = TransactionStatus.CONFIRMED
                withdrawal.settled_at = timezone.now()
                fields += ["status", "settled_at"]
                summary["confirmed"].append(withdrawal.onchain_txid)
            if fields:
                withdrawal.save(update_fields=fields)
        return summary

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
