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
    network = getattr(settings, "BITCOIN_NETWORK", "testnet")
    if network == "mainnet":
        return ["https://blockstream.info/api", "https://mempool.space/api"]
    # testnet/regtest/signet: blockstream serves testnet3, mempool.space serves testnet4.
    # Both are fine as alternate providers for address/UTXO/broadcast purposes.
    return ["https://blockstream.info/testnet/api", "https://mempool.space/testnet4/api"]


class EsploraError(Exception):
    pass


def _get(path: str, timeout: int = 15) -> Any:
    errors = []
    for base in _providers():
        try:
            r = requests.get(f"{base}{path}", timeout=timeout)
            r.raise_for_status()
            return r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text
        except requests.RequestException as exc:
            errors.append(f"{base}: {exc}")
    raise EsploraError(f"All block explorer providers failed for GET {path}: {'; '.join(errors)}")


def _post(path: str, data: str, timeout: int = 20) -> str:
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
