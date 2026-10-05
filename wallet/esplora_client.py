"""
Thin client for Esplora-compatible block explorer APIs (blockstream.info,
mempool.space). Used for UTXO/balance lookups, fee estimation, and
broadcasting signed transactions — btclib itself has no network layer, it
only builds/signs/serializes.

Resilience has two layers:
  1. One quick retry on a transient *server-side* blip — a 502/503/504
     response (`_SESSION`'s Retry policy below). Deliberately not retried:
     connection failures/timeouts — see the comment on `_RETRY` for why
     retrying those is pure wasted latency here, not real resilience.
  2. Falling through to the next configured provider in `_providers()` when
     the current one fails outright.

Providers are managed in the admin (Wallet → Block explorer providers): add
your own Esplora/electrs/mempool instance, set priorities, enable/disable
them. Every request updates the provider's health counters, providers that
keep failing are tried last, and `check_provider()` (the admin "Check now"
action and `manage.py check_providers`) verifies reachability, the chain
(genesis block), tip height lag and fee estimates.

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
import time
from dataclasses import dataclass
from typing import Any

import requests
import urllib3.util.connection as urllib3_connection
from django.conf import settings
from django.db import DatabaseError
from django.db.models import F
from django.utils import timezone
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


# Built-in providers, used only when the database has none configured for the
# network (e.g. before migrations ran). Manage the real list in the admin:
# Wallet → Block explorer providers.
#
# IMPORTANT: "testnet" (testnet3) and "testnet4" are *different chains* — same
# address format, different history. A deposit made on one never shows up on
# the other, so they must never be fallbacks for each other. blockstream.info
# has no testnet4 API (its /testnet4/ path serves the explorer webpage), so
# mempool.space is the only public testnet4 provider; add your own instance
# for redundancy.
DEFAULT_PROVIDERS = {
    "mainnet": [("Blockstream", "https://blockstream.info/api", "https://blockstream.info"),
                ("mempool.space", "https://mempool.space/api", "https://mempool.space")],
    "testnet4": [("mempool.space", "https://mempool.space/testnet4/api", "https://mempool.space/testnet4")],
    "testnet": [("Blockstream", "https://blockstream.info/testnet/api", "https://blockstream.info/testnet"),
                ("mempool.space", "https://mempool.space/testnet/api", "https://mempool.space/testnet")],
    "signet": [("Blockstream", "https://blockstream.info/signet/api", "https://blockstream.info/signet"),
               ("mempool.space", "https://mempool.space/signet/api", "https://mempool.space/signet")],
}

# Hash of block 0 per network: a health check fetches it to prove a provider is
# serving the chain we think it is (e.g. not testnet3 when we run testnet4).
GENESIS_HASHES = {
    "mainnet": "000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f",
    "testnet": "000000000933ea01ad0ee984209779baaec3ced90fa3f408719526f8d77f4943",
    "testnet4": "00000000da84f2bafbbc53dee25a72ae507ff4914b867c565be350b0da8bf043",
    "signet": "00000008819873e925422c1ff0f99f7cc9bbb232af63a077a480a3633bee1ef6",
    "regtest": "0f9188f13cb7b2c71f2a335e3a4fc328bf5beb436012afca590b1a11466e2206",
}

# After this many failures in a row a provider is tried after the healthy ones
# (still tried, so it is picked up again as soon as it recovers).
CIRCUIT_BREAKER_FAILURES = 3
# A provider whose tip is this many blocks behind the best one is flagged as lagging.
MAX_TIP_LAG_BLOCKS = 3


@dataclass(frozen=True)
class Provider:
    name: str
    api_url: str
    web_url: str = ""
    timeout: int = 15
    headers: tuple = ()
    pk: Any = None  # BlockExplorerProvider id, None for a built-in default


def _network() -> str:
    return getattr(settings, "BITCOIN_NETWORK", "testnet4")


def _configured_rows(network: str):
    """BlockExplorerProvider rows for the network, or None if the table isn't usable
    yet (migrations not applied) — the caller then falls back to the defaults."""
    try:
        from wallet.models import BlockExplorerProvider

        return list(BlockExplorerProvider.objects.filter(network=network))
    except (DatabaseError, LookupError):
        return None


def _providers() -> list[Provider]:
    """Providers to try, in order, for the configured BITCOIN_NETWORK."""
    network = _network()
    rows = _configured_rows(network)
    if rows:
        active = [r for r in rows if r.is_active]
        if not active:
            raise EsploraError(
                f"Every block explorer provider for {network} is disabled. "
                "Enable one in the admin (Wallet → Block explorer providers)."
            )
        active.sort(key=lambda r: (r.consecutive_failures >= CIRCUIT_BREAKER_FAILURES, r.priority, r.name))
        return [
            Provider(
                name=r.name, api_url=r.api_url.rstrip("/"), web_url=r.web_url.rstrip("/"),
                timeout=r.timeout_seconds or 15,
                headers=((r.auth_header, r.auth_value),) if r.auth_header else (), pk=r.pk,
            )
            for r in active
        ]
    if network in DEFAULT_PROVIDERS:
        return [Provider(name=n, api_url=a, web_url=w) for n, a, w in DEFAULT_PROVIDERS[network]]
    raise EsploraError(
        f"No block explorer configured for BITCOIN_NETWORK={network!r} (e.g. regtest is a private "
        "chain nobody else can serve) — add your own Esplora instance in the admin."
    )


def explorer_web_base() -> str | None:
    """Base URL of a human-browsable explorer *webpage* (not the JSON API) for the
    configured network, for "view on explorer" links. None if none is configured."""
    try:
        for provider in _providers():
            if provider.web_url:
                return provider.web_url
    except EsploraError:
        pass
    return None


class EsploraError(Exception):
    """No configured provider could answer (unreachable, timeout, 5xx...)."""


class EsploraNotFound(EsploraError):
    """A provider answered definitively: the requested object (e.g. a txid) does not exist."""


class EsploraRejected(EsploraError):
    """A provider answered definitively: the broadcast transaction was rejected (HTTP 400)."""


def _record(provider: Provider, ok: bool, error: str = "") -> None:
    """Update a provider's health counters after a real request (best effort)."""
    if provider.pk is None:
        return
    try:
        from wallet.models import BlockExplorerProvider

        now = timezone.now()
        qs = BlockExplorerProvider.objects.filter(pk=provider.pk)
        if ok:
            qs.update(total_requests=F("total_requests") + 1, consecutive_failures=0, last_success_at=now)
        else:
            qs.update(
                total_requests=F("total_requests") + 1, total_failures=F("total_failures") + 1,
                consecutive_failures=F("consecutive_failures") + 1, last_failure_at=now, last_error=error[:500],
            )
    except DatabaseError:
        logger.debug("Could not record provider health for %s", provider.name, exc_info=True)


def _request(provider: Provider, method: str, path: str, data: str | None = None, timeout: tuple | None = None):
    return _SESSION.request(
        method, f"{provider.api_url}{path}", data=data, headers=dict(provider.headers),
        timeout=timeout or (3, provider.timeout),
    )


def _get(path: str, timeout: tuple[int, int] | None = None) -> Any:
    """GET from the first provider that answers. 404 is a definitive answer
    (EsploraNotFound); anything else that fails falls through to the next one."""
    errors = []
    for provider in _providers():
        try:
            r = _request(provider, "GET", path, timeout=timeout)
            if r.status_code == 404:
                _record(provider, ok=True)
                raise EsploraNotFound(f"{provider.api_url}{path} returned 404")
            r.raise_for_status()
            if "text/html" in r.headers.get("content-type", ""):
                # e.g. a path the provider doesn't serve as an API, answered with its website.
                raise requests.RequestException("returned an HTML page instead of API data (wrong API URL?)")
            _record(provider, ok=True)
            return r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text
        except requests.RequestException as exc:
            logger.debug("Provider %s failed for GET %s: %s", provider.name, path, exc)
            _record(provider, ok=False, error=str(exc))
            errors.append(f"{provider.name}: {exc}")
    raise EsploraError(f"All block explorer providers failed for GET {path}: {'; '.join(errors)}")


def _post(path: str, data: str, timeout: tuple[int, int] | None = None) -> str:
    errors = []
    for provider in _providers():
        try:
            r = _request(provider, "POST", path, data=data, timeout=timeout)
            if r.status_code == 400:
                # The provider parsed and validated the transaction and refused it
                # (bad signature, inputs already spent, fee too low...). Another
                # provider would refuse it too — this is a definitive answer.
                _record(provider, ok=True)
                raise EsploraRejected(f"Transaction rejected by {provider.name}: {r.text.strip()[:300]}")
            r.raise_for_status()
            _record(provider, ok=True)
            return r.text.strip()
        except requests.RequestException as exc:
            logger.debug("Provider %s failed for POST %s: %s", provider.name, path, exc)
            _record(provider, ok=False, error=str(exc))
            errors.append(f"{provider.name}: {getattr(exc.response, 'text', '') or exc}")
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
            "block_height": int(block_height) if confirmed and block_height else None,
            "confirmations": confirmations,
        })
    return utxos


def get_tx_status(txid: str) -> dict[str, Any]:
    """{"confirmed": bool, "block_height": int|None} for a known transaction.

    Raises EsploraNotFound if the explorer does not know the txid at all (never
    broadcast, or dropped from the mempool) and EsploraError if it can't be reached.
    """
    data = _get(f"/tx/{txid}/status")
    confirmed = bool(data.get("confirmed"))
    return {"confirmed": confirmed, "block_height": data.get("block_height") if confirmed else None}


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


# ─────────────────────────────────────────────────────────────
# Health checks
# ─────────────────────────────────────────────────────────────

def _probe(provider: Provider, path: str) -> requests.Response:
    r = _request(provider, "GET", path, timeout=(5, provider.timeout))
    r.raise_for_status()
    if "text/html" in r.headers.get("content-type", ""):
        raise requests.RequestException(f"{path} returned an HTML page, not API data — is the API URL right?")
    return r


def probe_provider(provider: Provider, network: str) -> dict[str, Any]:
    """Run the health probes against one provider, without touching the database.

    Checks, in order: it answers with the current tip height; block 0 is the
    genesis block of `network` (so it serves the right chain); fee estimates work.
    Returns {"ok", "message", "latency_ms", "tip_height", "fee_rate_sat_per_vb"}.
    """
    result = {"ok": False, "message": "", "latency_ms": None, "tip_height": None, "fee_rate_sat_per_vb": None}
    started = time.monotonic()
    try:
        tip_text = _probe(provider, "/blocks/tip/height").text.strip()
        result["latency_ms"] = int((time.monotonic() - started) * 1000)
        if not tip_text.isdigit():
            raise ValueError(f"tip height is not a number: {tip_text[:60]!r}")
        result["tip_height"] = int(tip_text)

        expected = GENESIS_HASHES.get(network)
        if expected:
            genesis = _probe(provider, "/block-height/0").text.strip()
            if genesis != expected:
                raise ValueError(
                    f"wrong chain: block 0 is {genesis[:16]}…, expected the {network} genesis {expected[:16]}…"
                )

        estimates = _probe(provider, "/fee-estimates").json()
        if not isinstance(estimates, dict) or not estimates:
            raise ValueError("fee estimates are empty")
        result["fee_rate_sat_per_vb"] = float(estimates.get("6") or next(iter(estimates.values())))
    except (requests.RequestException, ValueError) as exc:
        result["message"] = str(exc)[:500]
        return result

    result["ok"] = True
    result["message"] = f"OK — tip {result['tip_height']}, {result['latency_ms']} ms"
    return result


def check_provider(row) -> dict[str, Any]:
    """Health-check one BlockExplorerProvider row and save the result on it."""
    provider = Provider(
        name=row.name, api_url=row.api_url.rstrip("/"), timeout=row.timeout_seconds or 15,
        headers=((row.auth_header, row.auth_value),) if row.auth_header else (), pk=row.pk,
    )
    result = probe_provider(provider, row.network)
    now = timezone.now()
    row.last_checked_at = now
    row.last_check_ok = result["ok"]
    row.last_check_message = result["message"]
    row.last_latency_ms = result["latency_ms"]
    if result["tip_height"] is not None:
        row.last_tip_height = result["tip_height"]
    if result["ok"]:
        row.consecutive_failures = 0
        row.last_success_at = now
    else:
        row.consecutive_failures += 1
        row.last_failure_at = now
        row.last_error = result["message"]
    row.save(update_fields=[
        "last_checked_at", "last_check_ok", "last_check_message", "last_latency_ms", "last_tip_height",
        "consecutive_failures", "last_success_at", "last_failure_at", "last_error", "updated_at",
    ])
    return result


def check_providers(rows) -> list[dict[str, Any]]:
    """Health-check several providers, then flag any that lag behind the best tip
    of their network (a stale provider would under-report confirmations)."""
    rows = list(rows)
    results = [(row, check_provider(row)) for row in rows]

    best_tip: dict[str, int] = {}
    for row, result in results:
        if result["ok"]:
            best_tip[row.network] = max(best_tip.get(row.network, 0), result["tip_height"])
    for row, result in results:
        lag = best_tip.get(row.network, 0) - (result["tip_height"] or 0)
        if result["ok"] and lag > MAX_TIP_LAG_BLOCKS:
            result["ok"] = False
            result["message"] = f"lagging: tip {result['tip_height']} is {lag} blocks behind {best_tip[row.network]}"
            row.last_check_ok = False
            row.last_check_message = row.last_error = result["message"]
            row.save(update_fields=["last_check_ok", "last_check_message", "last_error", "updated_at"])
    return [{"provider": row, **result} for row, result in results]
