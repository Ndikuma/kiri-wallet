"""
Thin client for Esplora-compatible block explorer APIs (blockstream.info,
mempool.space). Used for UTXO/balance lookups, fee estimation, and
broadcasting signed transactions — python-bitcoinlib itself has no network
layer, it only builds/signs/serializes.

Tries providers in order and falls back on failure, since either can be
unreachable/rate-limited depending on the deployment network.
"""
from __future__ import annotations

import logging
from typing import Any

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

DUST_THRESHOLD_SATS = 546


def _providers() -> list[str]:
    """
    Base URLs to try, in order, for the configured BITCOIN_NETWORK.

    IMPORTANT: "testnet" (testnet3) and "testnet4" are *different chains* —
    same address format (so an address looks valid on both), completely
    different genesis block and transaction history. A deposit made on one
    will never show up when querying the other. They must not be mixed as
    if they were interchangeable fallbacks for the same data (that was a
    real bug here: testnet3 was checked first, got a valid-but-empty
    response, and testnet4 — where real testnet coins actually are today —
    was never even tried).

    blockstream.info has no testnet4 API (its /testnet4/ path just serves
    the generic explorer webpage, not real API data) — only mempool.space
    does. testnet3 is largely defunct in practice (very few faucets still
    issue it), so testnet4 is what "testnet" almost always means today.
    """
    network = getattr(settings, "BITCOIN_NETWORK", "testnet")
    if network == "mainnet":
        return ["https://blockstream.info/api", "https://mempool.space/api"]
    if network == "testnet4":
        return ["https://mempool.space/testnet4/api"]
    if network == "testnet":
        return ["https://blockstream.info/testnet/api", "https://mempool.space/testnet/api"]
    if network == "signet":
        return ["https://blockstream.info/signet/api", "https://mempool.space/signet/api"]
    raise EsploraError(
        f"No public block explorer for BITCOIN_NETWORK={network!r} (e.g. regtest is a private "
        "chain nobody else can serve) — point this at your own Esplora instance instead."
    )


class EsploraError(Exception):
    pass


def _get(path: str, timeout: tuple[int, int] = (3, 15)) -> Any:
    """`timeout` is (connect, read) seconds — a short connect timeout so a fully
    unreachable provider fails fast instead of retrying across its resolved IPs
    for a long time before we move on to the next provider."""
    errors = []
    for base in _providers():
        try:
            r = requests.get(f"{base}{path}", timeout=timeout)
            r.raise_for_status()
            return r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text
        except requests.RequestException as exc:
            errors.append(f"{base}: {exc}")
    raise EsploraError(f"All block explorer providers failed for GET {path}: {'; '.join(errors)}")


def _post(path: str, data: str, timeout: tuple[int, int] = (3, 20)) -> str:
    errors = []
    for base in _providers():
        try:
            r = requests.post(f"{base}{path}", data=data, timeout=timeout)
            r.raise_for_status()
            return r.text.strip()
        except requests.RequestException as exc:
            errors.append(f"{base}: {getattr(exc.response, 'text', '') or exc}")
    raise EsploraError(f"All block explorer providers failed for POST {path}: {'; '.join(errors)}")


def get_tip_height() -> int:
    return int(_get("/blocks/tip/height"))


def get_utxos(address: str) -> list[dict[str, Any]]:
    """Return this address's UTXOs, each with confirmations computed from the current tip."""
    data = _get(f"/address/{address}/utxo")
    tip = None
    utxos = []
    for entry in data:
        confirmed = bool(entry.get("status", {}).get("confirmed"))
        block_height = entry.get("status", {}).get("block_height")
        confirmations = 0
        if confirmed and block_height:
            if tip is None:
                tip = get_tip_height()
            confirmations = max(tip - block_height + 1, 0)
        utxos.append({
            "txid": entry["txid"],
            "vout": entry["vout"],
            "value": int(entry["value"]),
            "confirmed": confirmed,
            "confirmations": confirmations,
        })
    return utxos


def get_address_balance(address: str) -> int:
    """Confirmed balance in sats (funded - spent), ignoring unconfirmed mempool activity."""
    data = _get(f"/address/{address}")
    stats = data.get("chain_stats", {})
    return int(stats.get("funded_txo_sum", 0)) - int(stats.get("spent_txo_sum", 0))


def get_fee_rate_sat_per_vb(target_blocks: int = 6) -> float:
    """Recommended feerate (sat/vB) for confirmation within `target_blocks`."""
    estimates = _get("/fee-estimates")
    key = str(target_blocks)
    if key in estimates:
        return float(estimates[key])
    # fall back to the closest available target
    closest = min((int(k) for k in estimates), key=lambda k: abs(k - target_blocks))
    return float(estimates[str(closest)])


def broadcast_tx(raw_tx_hex: str) -> str:
    """Broadcast a raw signed transaction; returns its txid."""
    return _post("/tx", raw_tx_hex)
