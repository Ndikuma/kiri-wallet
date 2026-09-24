"""
Thin client for Esplora-compatible block explorer APIs (blockstream.info,
mempool.space). Used for UTXO/balance lookups, fee estimation, and
broadcasting signed transactions — python-bitcoinlib itself has no network
layer, it only builds/signs/serializes.

Resilience has two layers:
  1. One quick retry on a transient *server-side* blip — a 502/503/504
     response (`_SESSION`'s Retry policy below). Deliberately not retried:
     connection failures/timeouts — see the comment on `_RETRY` for why
     retrying those is pure wasted latency here, not real resilience.
  2. Falling through to the next configured provider in `_providers()` when
     the current one fails outright.

For mainnet/testnet3/signet there are two real, independent providers to
fail over between. For testnet4 there is only one: mempool.space is the
only major free public Esplora-API provider that supports it — blockstream.info
has no testnet4 API at all (confirmed: its /testnet4/ path just serves the
generic explorer webpage, not real API data). If mempool.space is
unreachable from your network, no amount of retrying finds a second
provider that doesn't exist; the fix is checking your own network path
(firewall/VPN/DNS) to mempool.space, or running your own Esplora/electrs
instance and adding it here once you have one you trust.
"""
from __future__ import annotations

import logging
import socket
from typing import Any

import requests
import urllib3.util.connection as urllib3_connection
from django.conf import settings
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

DUST_THRESHOLD_SATS = 546

# Prefer IPv4: some networks (containers, certain VPS/cloud setups) advertise
# IPv6 DNS records for a host but have no working IPv6 route, which surfaces
# as "Network is unreachable" even though IPv4 to the same host works fine.
# This is a process-wide, best-effort preference — harmless where IPv6 works,
# and it only affects address selection, never blocks a genuine IPv6-only host.
def _prefer_ipv4_gai_family():
    return socket.AF_INET


urllib3_connection.allowed_gai_family = _prefer_ipv4_gai_family

# Retry only on a real HTTP response with a 5xx status (one quick retry,
# ~0.3s backoff) — that's a connection that already succeeded, so a retry is
# cheap and can genuinely recover a transient server-side blip.
#
# Deliberately connect=0/read=0 (no retry on connection errors/timeouts): a
# host can resolve to several IPs (mempool.space resolves to 7), and the
# socket layer already tries every one of them in turn within a *single*
# attempt — each retry we added on top just repeated that whole multi-IP
# sweep again, turning "provider is down" into a multi-times-longer stall
# for zero extra chance of success, before we ever reach the next provider
# or report the failure. Measured directly: retries=1 here took this from
# ~21s to ~42s to fail against an unreachable host, for no benefit.
_RETRY = Retry(
    total=1,
    connect=0,
    read=0,
    status=1,
    backoff_factor=0.3,
    status_forcelist=(500, 502, 503, 504),
    allowed_methods=("GET", "POST"),
)
_SESSION = requests.Session()
_SESSION.mount("https://", HTTPAdapter(max_retries=_RETRY))
_SESSION.mount("http://", HTTPAdapter(max_retries=_RETRY))


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


def explorer_web_base() -> str | None:
    """Base URL of a human-browsable block explorer *webpage* (not the JSON API) for
    the configured BITCOIN_NETWORK, for building "view on explorer" links e.g. in the
    admin. Prefers blockstream.info, same reasoning as `_providers()` above (and same
    testnet4 exception, since blockstream.info has no explorer pages for it either).
    Returns None for networks with no public web explorer (e.g. regtest)."""
    network = getattr(settings, "BITCOIN_NETWORK", "testnet")
    if network == "mainnet":
        return "https://blockstream.info"
    if network == "testnet4":
        return "https://mempool.space/testnet4"
    if network == "testnet":
        return "https://blockstream.info/testnet"
    if network == "signet":
        return "https://blockstream.info/signet"
    return None


class EsploraError(Exception):
    pass


def _get(path: str, timeout: tuple[int, int] = (2, 15)) -> Any:
    """`timeout` is (connect, read) seconds per attempt — short enough that a fully
    unreachable provider exhausts its retries and fails fast instead of stalling
    the whole scan cycle, but this still means testnet4 with just one provider
    configured has nothing left to fall through to once that provider is down."""
    errors = []
    for base in _providers():
        try:
            r = _SESSION.get(f"{base}{path}", timeout=timeout)
            r.raise_for_status()
            return r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text
        except requests.RequestException as exc:
            logger.debug("Provider %s failed for GET %s: %s", base, path, exc)
            errors.append(f"{base}: {exc}")
    raise EsploraError(f"All block explorer providers failed for GET {path}: {'; '.join(errors)}")


def _post(path: str, data: str, timeout: tuple[int, int] = (2, 20)) -> str:
    errors = []
    for base in _providers():
        try:
            r = _SESSION.post(f"{base}{path}", data=data, timeout=timeout)
            r.raise_for_status()
            return r.text.strip()
        except requests.RequestException as exc:
            logger.debug("Provider %s failed for POST %s: %s", base, path, exc)
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
