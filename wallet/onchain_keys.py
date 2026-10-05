"""
On-chain Bitcoin key management: one BIP32 HD wallet for the whole platform.

Every address handed out (a user's deposit address, or an internal change
address created during a withdrawal) is a deterministic child of a single
root extended private key:

  - native SegWit (P2WPKH, bc1q/tb1q — the default): m/84'/<coin_type>'/0'/<chain>/<index>
  - legacy (P2PKH, 1.../m.../n...):                   m/44'/<coin_type>'/0'/<chain>/<index>

where <chain> is 0 for deposit addresses and 1 for change, per BIP44/BIP84. The
full path is stored on each `PlatformBitcoinAddress` row, so addresses created
before SegWit support (all legacy, m/44'/.../0/<index>) stay spendable. The root
xprv is generated once, encrypted with Fernet (`WALLET_ENCRYPTION_KEY`), and
stored in `BitcoinHDWallet` (a one-row table); `PlatformBitcoinAddress`
tracks the (address, derivation_index) pairs derived from it so a UTXO found
at some address can be signed for later without re-deriving every index.
"""
from __future__ import annotations

import os
import uuid

from btclib.bip32.bip32 import derive, prv_keyinfo_from_xprv, rootxprv_from_seed
from btclib.hashes import hash160
from btclib.network import NETWORKS
from btclib.script.script import serialize as script_serialize
from btclib.script.script_pub_key import ScriptPubKey
from btclib.to_pub_key import pub_keyinfo_from_prv_key
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import transaction as db_transaction

from wallet.models import BitcoinAddressPurpose, BitcoinHDWallet, BitcoinScriptType, PlatformBitcoinAddress

# BIP44 registered coin type: 0 = Bitcoin mainnet, 1 = any Bitcoin testnet/regtest/signet.
_COIN_TYPE_BY_NETWORK = {"mainnet": 0, "testnet": 1, "testnet4": 1, "regtest": 1, "signet": 1}

# Fixed primary key so get_or_create() is race-safe (a concurrent duplicate insert
# hits the PK's unique constraint and Django's get_or_create() falls back to a get()).
_HD_WALLET_SINGLETON_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


class KeyEncryptionError(RuntimeError):
    pass


def _fernet() -> Fernet:
    key = getattr(settings, "WALLET_ENCRYPTION_KEY", "")
    if not key:
        raise KeyEncryptionError(
            "WALLET_ENCRYPTION_KEY is not set. Generate one with "
            "`python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"` "
            "and add it to .env before generating on-chain addresses."
        )
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except (ValueError, TypeError) as exc:
        raise KeyEncryptionError(f"WALLET_ENCRYPTION_KEY is not a valid Fernet key: {exc}") from exc


def _network() -> str:
    return getattr(settings, "BITCOIN_NETWORK", "testnet")


def _get_or_create_hd_wallet() -> BitcoinHDWallet:
    network = _network()
    hd_wallet = BitcoinHDWallet.objects.filter(pk=_HD_WALLET_SINGLETON_ID).first()

    if hd_wallet is None:
        seed = os.urandom(32)
        version = NETWORKS[network].bip32_prv
        root_xprv = rootxprv_from_seed(seed, version)
        encrypted = _fernet().encrypt(root_xprv.encode()).decode()
        # get_or_create(), not create(): if a concurrent request lost this same race,
        # its insert hits our unique PK and it falls back to fetching this row instead.
        hd_wallet, _created = BitcoinHDWallet.objects.get_or_create(
            id=_HD_WALLET_SINGLETON_ID, defaults={"encrypted_root_xprv": encrypted, "network": network},
        )

    if hd_wallet.network != network:
        raise RuntimeError(
            f"BitcoinHDWallet was created for network={hd_wallet.network!r} but "
            f"settings.BITCOIN_NETWORK={network!r}. Changing networks with existing "
            "on-chain addresses is not supported — restore the original setting."
        )
    return hd_wallet


def _decrypt_root_xprv(hd_wallet: BitcoinHDWallet) -> str:
    try:
        return _fernet().decrypt(hd_wallet.encrypted_root_xprv.encode()).decode()
    except InvalidToken as exc:
        raise KeyEncryptionError(
            "Could not decrypt the platform's BIP32 root key: WALLET_ENCRYPTION_KEY is wrong or has "
            "rotated since it was created. Every on-chain address is unrecoverable without the "
            "original key — restore it from backup before doing anything else."
        ) from exc


_PURPOSE_BY_SCRIPT_TYPE = {BitcoinScriptType.P2PKH: 44, BitcoinScriptType.P2WPKH: 84}
_CHAIN_BY_ADDRESS_PURPOSE = {BitcoinAddressPurpose.DEPOSIT: 0, BitcoinAddressPurpose.CHANGE: 1}


def default_script_type() -> str:
    script_type = getattr(settings, "BITCOIN_ADDRESS_TYPE", BitcoinScriptType.P2WPKH)
    if script_type not in BitcoinScriptType.values:
        raise ValueError(f"BITCOIN_ADDRESS_TYPE must be one of {BitcoinScriptType.values}, not {script_type!r}.")
    return script_type


def derivation_path(network: str, script_type: str, purpose: str, index: int) -> str:
    coin_type = _COIN_TYPE_BY_NETWORK.get(network, 1)
    bip_purpose = _PURPOSE_BY_SCRIPT_TYPE[script_type]
    chain = _CHAIN_BY_ADDRESS_PURPOSE[purpose]
    return f"m/{bip_purpose}h/{coin_type}h/0h/{chain}/{index}"


def script_pub_key_for(pub_key: bytes, script_type: str, network: str) -> ScriptPubKey:
    if script_type == BitcoinScriptType.P2WPKH:
        return ScriptPubKey(script_serialize(["OP_0", hash160(pub_key)]), network)
    return ScriptPubKey.p2pkh(pub_key, compressed=True, network=network)


def _derive_key(root_xprv: str, path: str, script_type: str) -> tuple[int, bytes, ScriptPubKey]:
    child_xprv = derive(root_xprv, path)
    scalar, resolved_network, _compressed = prv_keyinfo_from_xprv(child_xprv)
    pub_key, _network = pub_keyinfo_from_prv_key(scalar, network=resolved_network, compressed=True)
    return scalar, pub_key, script_pub_key_for(pub_key, script_type, resolved_network)


def create_address(
    label: str = "",
    *,
    purpose: str = BitcoinAddressPurpose.DEPOSIT,
    wallet=None,
    script_type: str | None = None,
) -> PlatformBitcoinAddress:
    """Derive and persist the next unused address in the platform's HD wallet."""
    script_type = script_type or default_script_type()
    _get_or_create_hd_wallet()  # ensure the singleton row exists before locking it below

    with db_transaction.atomic():
        hd_wallet = BitcoinHDWallet.objects.select_for_update().get(pk=_HD_WALLET_SINGLETON_ID)
        index = hd_wallet.next_index
        root_xprv = _decrypt_root_xprv(hd_wallet)
        path = derivation_path(hd_wallet.network, script_type, purpose, index)
        _scalar, _pub_key, script_pub_key = _derive_key(root_xprv, path, script_type)

        hd_wallet.next_index = index + 1
        hd_wallet.save(update_fields=["next_index"])

        return PlatformBitcoinAddress.objects.create(
            address=script_pub_key.address, derivation_index=index, derivation_path=path,
            script_type=script_type, purpose=purpose, wallet=wallet, label=label,
        )


def load_signing_key(address_row: PlatformBitcoinAddress) -> tuple[int, bytes, ScriptPubKey]:
    """Return (private_key_scalar, compressed_pub_key, script_pub_key) to sign a spend from this address.

    Re-derives the key from its stored path and refuses to return it if the
    result doesn't reproduce the stored address — a mismatch (wrong root key,
    corrupted path) must never produce a signature for coins it doesn't control.
    """
    hd_wallet = BitcoinHDWallet.objects.get(pk=_HD_WALLET_SINGLETON_ID)
    root_xprv = _decrypt_root_xprv(hd_wallet)
    path = address_row.derivation_path or derivation_path(
        hd_wallet.network, BitcoinScriptType.P2PKH, BitcoinAddressPurpose.DEPOSIT, address_row.derivation_index,
    )
    scalar, pub_key, script_pub_key = _derive_key(root_xprv, path, address_row.script_type)
    if script_pub_key.address != address_row.address:
        raise KeyEncryptionError(
            f"Re-derived key for {path} gives {script_pub_key.address}, not the stored address "
            f"{address_row.address}. Refusing to sign."
        )
    return scalar, pub_key, script_pub_key


def address_network_class(network: str | None = None) -> str:
    """btclib's address network for a BITCOIN_NETWORK: every test network (testnet3,
    testnet4, signet) shares the "testnet" address format; only mainnet differs."""
    return "mainnet" if (network or _network()) == "mainnet" else "testnet"
