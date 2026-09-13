"""
On-chain Bitcoin key management: one BIP32 HD wallet for the whole platform.

Every address handed out (a user's deposit address, or an internal change
address created during a withdrawal) is a deterministic child of a single
root extended private key, at `m/44'/<coin_type>'/0'/0/<index>`. The root
xprv is generated once, encrypted with Fernet (`WALLET_ENCRYPTION_KEY`), and
stored in `BitcoinHDWallet` (a one-row table); `PlatformBitcoinAddress`
tracks the (address, derivation_index) pairs derived from it so a UTXO found
at some address can be signed for later without re-deriving every index.
"""
from __future__ import annotations

import os
import uuid

from btclib.bip32.bip32 import derive, prv_keyinfo_from_xprv, rootxprv_from_seed
from btclib.network import NETWORKS
from btclib.script.script_pub_key import ScriptPubKey
from btclib.to_pub_key import pub_keyinfo_from_prv_key
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import transaction as db_transaction

from wallet.models import BitcoinHDWallet, PlatformBitcoinAddress

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


def _derivation_path(network: str, index: int) -> str:
    coin_type = _COIN_TYPE_BY_NETWORK.get(network, 1)
    return f"m/44h/{coin_type}h/0h/0/{index}"


def _scalar_and_script_pub_key(root_xprv: str, network: str, index: int):
    child_xprv = derive(root_xprv, _derivation_path(network, index))
    scalar, resolved_network, _compressed = prv_keyinfo_from_xprv(child_xprv)
    script_pub_key = ScriptPubKey.p2pkh(scalar, compressed=True, network=resolved_network)
    return scalar, script_pub_key


def create_address(label: str = "") -> PlatformBitcoinAddress:
    """Derive and persist the next unused address in the platform's HD wallet."""
    _get_or_create_hd_wallet()  # ensure the singleton row exists before locking it below

    with db_transaction.atomic():
        hd_wallet = BitcoinHDWallet.objects.select_for_update().get(pk=_HD_WALLET_SINGLETON_ID)
        index = hd_wallet.next_index
        root_xprv = _decrypt_root_xprv(hd_wallet)
        _scalar, script_pub_key = _scalar_and_script_pub_key(root_xprv, hd_wallet.network, index)

        hd_wallet.next_index = index + 1
        hd_wallet.save(update_fields=["next_index"])

        return PlatformBitcoinAddress.objects.create(
            address=script_pub_key.address, derivation_index=index, label=label,
        )


def load_signing_key(address_row: PlatformBitcoinAddress) -> tuple[int, ScriptPubKey]:
    """Return (private_key_scalar, script_pub_key) to sign a spend from this address."""
    hd_wallet = BitcoinHDWallet.objects.get(pk=_HD_WALLET_SINGLETON_ID)
    root_xprv = _decrypt_root_xprv(hd_wallet)
    return _scalar_and_script_pub_key(root_xprv, hd_wallet.network, address_row.derivation_index)


def pub_key_bytes(scalar: int, network: str) -> bytes:
    """Compressed SEC-encoded public key for a private key scalar, for building a scriptSig."""
    pubkey, _network = pub_keyinfo_from_prv_key(scalar, network=network, compressed=True)
    return pubkey
