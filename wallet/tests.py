"""
Tests for the on-chain Bitcoin flow, run against an in-memory fake block
explorer (`FakeChain`) so they never touch the network. Signed withdrawals are
checked independently with btclib's consensus engine.
"""
import os
from datetime import timedelta
from unittest import mock

from btclib.script.engine import verify_transaction
from btclib.script.script_pub_key import ScriptPubKey
from btclib.to_pub_key import pub_keyinfo_from_prv_key
from btclib.tx.tx import Tx
from btclib.tx.tx_out import TxOut
from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from wallet import onchain_keys
from wallet.bitcoin import (
    BitcoinWithdrawalError,
    CustodialBitcoinService,
    parse_bip21,
    validate_destination,
)
from wallet.esplora_client import EsploraError, EsploraNotFound, EsploraRejected
from wallet.models import (
    BitcoinAddressPurpose,
    BitcoinUTXO,
    BitcoinUTXOStatus,
    PlatformBitcoinAddress,
    TransactionStatus,
    TransactionType,
    Wallet,
    WalletTransaction,
)
from wallet.withdrawal import decode_withdrawal_target, estimate_withdrawal_fees, process_withdrawal

TEST_KEY = Fernet.generate_key().decode()


def random_address(network="testnet", script_type="p2wpkh"):
    pub, _ = pub_keyinfo_from_prv_key(int.from_bytes(os.urandom(31), "big") + 1, network=network, compressed=True)
    return onchain_keys.script_pub_key_for(pub, script_type, network).address


def fake_txid():
    return os.urandom(32).hex()


class FakeChain:
    """Stands in for wallet.esplora_client, patched into wallet.bitcoin."""

    def __init__(self):
        self.tip = 1000
        self.utxos = {}            # address -> list of {"txid", "vout", "value", "block_height"}
        self.known_txs = {}        # txid -> block_height | None
        self.broadcasts = []
        self.broadcast_error = None
        self.unreachable = False
        self.fee_rate = 2.0

    def fund(self, address, value, confirmations=0, txid=None, vout=0):
        txid = txid or fake_txid()
        height = self.tip - confirmations + 1 if confirmations else None
        self.utxos.setdefault(address, []).append({"txid": txid, "vout": vout, "value": value, "block_height": height})
        self.known_txs[txid] = height
        return txid

    def mine(self, blocks=1):
        self.tip += blocks
        for entries in self.utxos.values():
            for e in entries:
                if e["block_height"] is None:
                    e["block_height"] = self.tip
        for txid, height in self.known_txs.items():
            if height is None:
                self.known_txs[txid] = self.tip

    # -- patched functions -------------------------------------------------

    def _check(self):
        if self.unreachable:
            raise EsploraError("explorer unreachable")

    def get_utxos(self, address):
        self._check()
        out = []
        for e in self.utxos.get(address, []):
            conf = self.tip - e["block_height"] + 1 if e["block_height"] else 0
            out.append({**e, "confirmed": e["block_height"] is not None, "confirmations": conf})
        return out

    def get_tip_height(self):
        self._check()
        return self.tip

    def get_fee_rate_sat_per_vb(self, target_blocks=6):
        self._check()
        return self.fee_rate

    def get_tx_status(self, txid):
        self._check()
        if txid not in self.known_txs:
            raise EsploraNotFound(txid)
        height = self.known_txs[txid]
        return {"confirmed": height is not None, "block_height": height}

    def broadcast_tx(self, raw_hex):
        if self.broadcast_error:
            raise self.broadcast_error
        tx = Tx.parse(bytes.fromhex(raw_hex))
        txid = tx.id.hex()
        self.broadcasts.append(raw_hex)
        self.known_txs[txid] = None
        spent = {(i.prev_out.tx_id.hex(), i.prev_out.vout) for i in tx.vin}
        for entries in self.utxos.values():
            entries[:] = [e for e in entries if (e["txid"], e["vout"]) not in spent]
        for vout, out in enumerate(tx.vout):
            if out.script_pub_key.type in ("p2pkh", "p2wpkh"):
                address = ScriptPubKey(out.script_pub_key.script, "testnet").address
                self.utxos.setdefault(address, []).append(
                    {"txid": txid, "vout": vout, "value": out.value, "block_height": None})
        return txid


@override_settings(
    WALLET_ENCRYPTION_KEY=TEST_KEY, BITCOIN_NETWORK="testnet4", BITCOIN_ADDRESS_TYPE="p2wpkh",
    BITCOIN_DEPOSIT_CONFIRMATIONS=4, BITCOIN_WITHDRAWAL_CONFIRMATIONS=1,
    BITCOIN_MIN_FEE_RATE=1, BITCOIN_MAX_FEE_RATE=500, BITCOIN_DROPPED_TX_GRACE_HOURS=24,
)
class OnchainTestCase(TestCase):
    def setUp(self):
        self.chain = FakeChain()
        for name in ("get_utxos", "get_tip_height", "get_fee_rate_sat_per_vb", "get_tx_status", "broadcast_tx"):
            patcher = mock.patch(f"wallet.bitcoin.{name}", side_effect=getattr(self.chain, name))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.service = CustodialBitcoinService()
        self.alice = self.make_wallet("alice")
        self.bob = self.make_wallet("bob")

    def make_wallet(self, username):
        user = get_user_model().objects.create_user(username=username, email=f"{username}@example.com", password="pw-12345678")
        return Wallet.objects.get_or_create(user=user)[0]

    def address_of(self, wallet):
        return self.service.get_or_create_user_address(wallet)["address"]

    def fund_platform(self, value=200_000, confirmations=6):
        """Give alice a credited on-chain deposit, which becomes platform liquidity."""
        txid = self.chain.fund(self.address_of(self.alice), value, confirmations=confirmations)
        self.service.scan_all_users()
        self.alice.refresh_from_db()
        return txid

    def reload(self, *objs):
        for obj in objs:
            obj.refresh_from_db()


class AddressTests(OnchainTestCase):
    def test_new_addresses_are_native_segwit_on_bip84_paths(self):
        info = self.service.get_or_create_user_address(self.alice)
        row = PlatformBitcoinAddress.objects.get(address=info["address"])
        self.assertTrue(info["address"].startswith("tb1q"))
        self.assertEqual(row.script_type, "p2wpkh")
        self.assertEqual(row.derivation_path, f"m/84h/1h/0h/0/{row.derivation_index}")
        self.assertEqual(row.wallet, self.alice)
        self.assertEqual(info["confirmations_required"], 4)

        change = onchain_keys.create_address(label="change", purpose=BitcoinAddressPurpose.CHANGE)
        self.assertEqual(change.derivation_path, f"m/84h/1h/0h/1/{change.derivation_index}")

    def test_address_is_stable_and_rotation_keeps_old_address_linked(self):
        first = self.address_of(self.alice)
        self.assertEqual(self.address_of(self.alice), first)
        second = self.service.new_user_address(self.alice)["address"]
        self.assertNotEqual(first, second)
        self.assertEqual(
            set(PlatformBitcoinAddress.objects.filter(wallet=self.alice).values_list("address", flat=True)),
            {first, second},
        )

    @override_settings(BITCOIN_ADDRESS_TYPE="p2pkh")
    def test_legacy_address_type_setting(self):
        address = self.address_of(self.alice)
        self.assertIn(address[0], "mn")
        row = PlatformBitcoinAddress.objects.get(address=address)
        self.assertTrue(row.derivation_path.startswith("m/44h/1h/0h/0/"))

    def test_signing_key_reproduces_address_and_refuses_mismatch(self):
        row = PlatformBitcoinAddress.objects.get(address=self.address_of(self.alice))
        _scalar, _pub, spk = onchain_keys.load_signing_key(row)
        self.assertEqual(spk.address, row.address)

        row.derivation_path = row.derivation_path.rsplit("/", 1)[0] + "/999"
        with self.assertRaises(onchain_keys.KeyEncryptionError):
            onchain_keys.load_signing_key(row)

    def test_destination_network_and_bip21(self):
        validate_destination(random_address("testnet"))
        validate_destination(random_address("testnet", "p2pkh"))
        with self.assertRaisesMessage(BitcoinWithdrawalError, "mainnet address"):
            validate_destination(random_address("mainnet"))
        with self.assertRaises(BitcoinWithdrawalError):
            validate_destination("tb1qnotarealaddress")

        address = random_address()
        self.assertEqual(parse_bip21(f"bitcoin:{address}?amount=0.00012345&label=x"), (address, 12345))
        target = decode_withdrawal_target(f"bitcoin:{address}?amount=0.0005")
        self.assertEqual((target.target_type, target.amount_sats), ("bitcoin_address", 50000))
        with self.assertRaises(ValueError):
            decode_withdrawal_target(random_address("mainnet"))


class DepositTests(OnchainTestCase):
    def test_pending_deposit_is_promoted_once_confirmed(self):
        self.chain.fund(self.address_of(self.alice), 50_000, confirmations=1)

        result = self.service.scan_all_users()
        self.assertEqual(len(result["processed"]), 1)
        self.reload(self.alice)
        self.assertEqual((self.alice.pending_balance, self.alice.available_balance), (50_000, 0))
        deposit = WalletTransaction.objects.get(type=TransactionType.DEPOSIT)
        self.assertEqual((deposit.status, deposit.confirmations), (TransactionStatus.PENDING, 1))

        self.chain.mine(2)
        self.service.scan_all_users()
        deposit.refresh_from_db()
        self.assertEqual((deposit.status, deposit.confirmations), (TransactionStatus.PENDING, 3))

        self.chain.mine(1)
        result = self.service.scan_all_users()
        self.assertEqual(len(result["promoted"]), 1)
        self.reload(self.alice, deposit)
        self.assertEqual((self.alice.pending_balance, self.alice.available_balance), (0, 50_000))
        self.assertEqual(self.alice.total_deposited, 50_000)
        self.assertEqual(deposit.status, TransactionStatus.CONFIRMED)

        # Further scans never credit the same output twice.
        self.chain.mine(5)
        result = self.service.scan_all_users()
        self.assertEqual((result["processed"], result["promoted"]), ([], []))
        self.reload(self.alice)
        self.assertEqual(self.alice.available_balance, 50_000)
        self.assertEqual(WalletTransaction.objects.filter(type=TransactionType.DEPOSIT).count(), 1)

    def test_deeply_confirmed_deposit_is_credited_directly(self):
        self.chain.fund(self.address_of(self.alice), 30_000, confirmations=10)
        self.service.scan_all_users()
        self.reload(self.alice)
        self.assertEqual((self.alice.pending_balance, self.alice.available_balance), (0, 30_000))

    def test_one_transaction_paying_two_users_credits_both(self):
        txid = fake_txid()
        self.chain.fund(self.address_of(self.alice), 10_000, confirmations=5, txid=txid, vout=0)
        self.chain.fund(self.address_of(self.bob), 20_000, confirmations=5, txid=txid, vout=1)
        self.service.scan_all_users()
        self.reload(self.alice, self.bob)
        self.assertEqual((self.alice.available_balance, self.bob.available_balance), (10_000, 20_000))

    def test_deposits_to_a_previous_address_still_count(self):
        old = self.address_of(self.alice)
        self.service.new_user_address(self.alice)
        self.chain.fund(old, 7_000, confirmations=6)
        self.service.scan_all_users()
        self.reload(self.alice)
        self.assertEqual(self.alice.available_balance, 7_000)

    def test_legacy_pending_record_without_vout_is_promoted(self):
        """Deposits recorded by the old scanner (txid only, no vout) get promoted, not duplicated."""
        address = self.address_of(self.alice)
        txid = self.chain.fund(address, 15_000, confirmations=2)
        self.alice.pending_balance = 15_000
        self.alice.save()
        WalletTransaction.objects.create(
            user=self.alice.user, wallet=self.alice, type=TransactionType.DEPOSIT, amount=15_000,
            status=TransactionStatus.PENDING, onchain_txid=txid, onchain_address=address, confirmations=2,
        )
        self.chain.mine(3)
        self.service.scan_all_users()
        self.reload(self.alice)
        self.assertEqual((self.alice.pending_balance, self.alice.available_balance), (0, 15_000))
        deposit = WalletTransaction.objects.get(type=TransactionType.DEPOSIT)
        self.assertEqual((deposit.status, deposit.onchain_vout), (TransactionStatus.CONFIRMED, 0))

    def test_dropped_unconfirmed_deposit_is_cancelled_after_grace_period(self):
        address = self.address_of(self.alice)
        txid = self.chain.fund(address, 9_000, confirmations=0)
        self.service.scan_all_users()
        self.reload(self.alice)
        self.assertEqual(self.alice.pending_balance, 9_000)

        # The transaction disappears (replaced / evicted).
        self.chain.utxos[address] = []
        del self.chain.known_txs[txid]
        result = self.service.scan_all_users()
        self.assertEqual(result["dropped"], [])  # still within the grace period
        self.reload(self.alice)
        self.assertEqual(self.alice.pending_balance, 9_000)

        BitcoinUTXO.objects.update(first_seen_at=timezone.now() - timedelta(hours=25))
        result = self.service.scan_all_users()
        self.assertEqual(len(result["dropped"]), 1)
        self.reload(self.alice)
        self.assertEqual((self.alice.pending_balance, self.alice.available_balance), (0, 0))
        self.assertEqual(WalletTransaction.objects.get(type=TransactionType.DEPOSIT).status, TransactionStatus.FAILED)
        self.assertEqual(BitcoinUTXO.objects.get().status, BitcoinUTXOStatus.DROPPED)

    def test_unreachable_explorer_is_reported_not_treated_as_empty(self):
        self.chain.fund(self.address_of(self.alice), 5_000, confirmations=6)
        self.chain.unreachable = True
        result = self.service.scan_all_users()
        self.assertEqual(len(result["failed"]), 1)
        self.assertEqual(result["processed"], [])
        self.reload(self.alice)
        self.assertEqual(self.alice.available_balance, 0)


class WithdrawalTests(OnchainTestCase):
    def verify_broadcast(self, raw_hex):
        """Independently check the broadcast transaction against the coins it spends."""
        tx = Tx.parse(bytes.fromhex(raw_hex))
        prevouts = []
        for txin in tx.vin:
            utxo = BitcoinUTXO.objects.get(txid=txin.prev_out.tx_id.hex(), vout=txin.prev_out.vout)
            prevouts.append(TxOut(utxo.value, ScriptPubKey.from_address(utxo.address.address)))
        verify_transaction(prevouts, tx)
        return tx, prevouts

    def test_successful_withdrawal(self):
        self.fund_platform(200_000)
        dest = random_address()

        tx, result = process_withdrawal(wallet=self.alice, destination=dest, amount_sats=50_000)

        self.assertEqual(result["provider_result"]["status"], "broadcasted")
        self.assertEqual(len(self.chain.broadcasts), 1)
        signed, prevouts = self.verify_broadcast(self.chain.broadcasts[0])
        self.assertEqual(signed.vout[0].value, 50_000)
        self.assertEqual(ScriptPubKey(signed.vout[0].script_pub_key.script, "testnet").address, dest)
        self.assertEqual(sum(p.value for p in prevouts) - sum(o.value for o in signed.vout), tx.network_fee_sats)
        self.assertTrue(all(i.sequence == 0xFFFFFFFD for i in signed.vin))  # RBF enabled

        # User pays amount + 1% policy fee (min 500); the platform wallet receives the fee.
        self.reload(self.alice)
        self.assertEqual(self.alice.available_balance, 200_000 - 50_000 - 500)
        self.assertEqual(Wallet.get_platform_wallet().available_balance, 500)
        self.assertEqual(tx.status, TransactionStatus.PENDING)
        self.assertEqual(tx.onchain_txid, signed.id.hex())

        # Inputs are spent; change is tracked as an unconfirmed platform coin.
        self.assertEqual(BitcoinUTXO.objects.filter(status=BitcoinUTXOStatus.SPENT).count(), 1)
        change = BitcoinUTXO.objects.get(address__purpose=BitcoinAddressPurpose.CHANGE)
        self.assertEqual(change.value, signed.vout[1].value)
        self.assertIsNone(change.block_height)

        # Once mined, the tracker confirms the withdrawal and the change confirms too.
        self.chain.mine()
        result = self.service.scan_all_users()
        self.assertEqual(result["withdrawals"]["confirmed"], [tx.onchain_txid])
        self.reload(tx, change)
        self.assertEqual((tx.status, tx.confirmations), (TransactionStatus.CONFIRMED, 1))
        self.assertIsNotNone(change.block_height)

    def test_spends_mixed_legacy_and_segwit_coins(self):
        with override_settings(BITCOIN_ADDRESS_TYPE="p2pkh"):
            legacy = self.service.new_user_address(self.alice)["address"]
        segwit = self.service.new_user_address(self.alice)["address"]
        self.chain.fund(legacy, 30_000, confirmations=6)
        self.chain.fund(segwit, 30_000, confirmations=6)
        self.service.scan_all_users()
        self.reload(self.alice)

        process_withdrawal(wallet=self.alice, destination=random_address(), amount_sats=45_000)
        signed, _ = self.verify_broadcast(self.chain.broadcasts[0])
        self.assertEqual(len(signed.vin), 2)
        self.assertTrue(signed.is_segwit)

    def test_rejected_broadcast_refunds_everything(self):
        self.fund_platform(100_000)
        self.chain.broadcast_error = EsploraRejected("bad-txns-inputs-missingorspent")

        with self.assertRaisesMessage(ValueError, "refunded"):
            process_withdrawal(wallet=self.alice, destination=random_address(), amount_sats=20_000)

        self.reload(self.alice)
        self.assertEqual((self.alice.available_balance, self.alice.total_withdrawn), (100_000, 0))
        self.assertEqual(Wallet.get_platform_wallet().available_balance, 0)
        self.assertEqual(WalletTransaction.objects.get(type=TransactionType.WITHDRAWAL).status, TransactionStatus.REFUNDED)
        self.assertFalse(WalletTransaction.objects.filter(type=TransactionType.FEE, status=TransactionStatus.CONFIRMED).exists())
        self.assertEqual(BitcoinUTXO.objects.get().status, BitcoinUTXOStatus.UNSPENT)

    def test_uncertain_broadcast_is_retried_by_the_tracker(self):
        self.fund_platform(100_000)
        self.chain.broadcast_error = EsploraError("timeout")

        tx, result = process_withdrawal(wallet=self.alice, destination=random_address(), amount_sats=20_000)
        self.assertEqual(result["provider_result"]["status"], "broadcast_pending")
        self.assertEqual(tx.status, TransactionStatus.PENDING)
        self.assertEqual(BitcoinUTXO.objects.get().status, BitcoinUTXOStatus.RESERVED)
        self.reload(self.alice)
        self.assertEqual(self.alice.available_balance, 100_000 - 20_000 - 500)  # stays debited

        self.chain.broadcast_error = None
        self.assertEqual(self.service.track_withdrawals()["rebroadcast"], [tx.onchain_txid])
        self.chain.mine()
        self.assertEqual(self.service.track_withdrawals()["confirmed"], [tx.onchain_txid])
        self.assertEqual(BitcoinUTXO.objects.get(address__purpose=BitcoinAddressPurpose.DEPOSIT).status, BitcoinUTXOStatus.SPENT)

    def test_cannot_spend_unconfirmed_deposits_or_more_than_the_platform_holds(self):
        self.chain.fund(self.address_of(self.alice), 80_000, confirmations=2)  # pending, not yet spendable
        self.service.scan_all_users()
        self.alice.refresh_from_db()
        self.alice.available_balance = 1_000_000  # e.g. Lightning-funded sats
        self.alice.save()

        with self.assertRaisesMessage(ValueError, "Insufficient on-chain liquidity"):
            process_withdrawal(wallet=self.alice, destination=random_address(), amount_sats=10_000)
        self.reload(self.alice)
        self.assertEqual(self.alice.available_balance, 1_000_000)
        self.assertFalse(WalletTransaction.objects.filter(type=TransactionType.WITHDRAWAL).exists())

    def test_insufficient_user_balance(self):
        self.fund_platform(100_000)
        with self.assertRaisesMessage(ValueError, "Insufficient balance"):
            process_withdrawal(wallet=self.bob, destination=random_address(), amount_sats=10_000)

    def test_coins_are_not_reused_by_a_second_withdrawal(self):
        self.fund_platform(60_000)
        process_withdrawal(wallet=self.alice, destination=random_address(), amount_sats=20_000)
        # The only confirmed coin is spent and the change is unconfirmed: nothing left to spend yet.
        with self.assertRaisesMessage(ValueError, "Insufficient on-chain liquidity"):
            process_withdrawal(wallet=self.alice, destination=random_address(), amount_sats=5_000)
        self.chain.mine()
        self.service.scan_all_users()
        process_withdrawal(wallet=self.alice, destination=random_address(), amount_sats=5_000)
        spent_inputs = [
            (i.prev_out.tx_id.hex(), i.prev_out.vout)
            for raw in self.chain.broadcasts for i in Tx.parse(bytes.fromhex(raw)).vin
        ]
        self.assertEqual(len(spent_inputs), 2)
        self.assertEqual(len(spent_inputs), len(set(spent_inputs)))

    def test_fee_quote_includes_network_fee(self):
        self.fund_platform(100_000)
        quote = estimate_withdrawal_fees(wallet=self.alice, destination=random_address(), amount_sats=10_000)
        self.assertTrue(quote["can_withdraw"])
        self.assertGreater(quote["onchain"]["network_fee_sats"], 0)
        self.assertEqual(quote["onchain"]["fee_rate_sat_per_vb"], 2.0)

    def test_dust_amount_is_refused(self):
        self.fund_platform(100_000)
        with self.assertRaises(ValueError):
            self.service.withdraw(self.alice, random_address(), 100)


class ApiTests(OnchainTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(self.alice.user)

    def test_address_and_deposit_endpoints(self):
        self.assertEqual(self.client.get("/api/wallet/bitcoin/").status_code, 404)
        created = self.client.post("/api/wallet/bitcoin/").json()["data"]
        self.assertTrue(created["address"].startswith("tb1q"))
        shown = self.client.get("/api/wallet/bitcoin/").json()["data"]
        self.assertEqual(shown["bitcoin_address"], created["address"])
        self.assertEqual(shown["confirmations_required"], 4)

        rotated = self.client.post("/api/wallet/bitcoin/new-address/").json()["data"]
        self.assertNotEqual(rotated["address"], created["address"])

        self.chain.fund(created["address"], 12_345, confirmations=1)
        self.service.scan_all_users()
        deposits = self.client.get("/api/wallet/bitcoin/deposits/").json()["data"]
        self.assertEqual(len(deposits), 1)
        self.assertEqual((deposits[0]["amount"], deposits[0]["status"], deposits[0]["confirmations"]), (12_345, "pending", 1))

    def test_withdraw_endpoint(self):
        self.fund_platform(100_000)
        response = self.client.post("/api/wallet/withdraw/", {"target": random_address(), "amount": 20_000}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertEqual(data["available_balance"], 100_000 - 20_000 - 500)
        self.assertEqual(data["withdrawal"]["provider_result"]["status"], "broadcasted")

        response = self.client.post("/api/wallet/withdraw/", {"target": random_address("mainnet"), "amount": 20_000}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_web_bitcoin_page(self):
        self.client.force_login(self.alice.user)
        self.assertEqual(self.client.get("/app/bitcoin/").status_code, 200)
        page = self.client.post("/app/bitcoin/")
        self.assertContains(page, "tb1q")
        self.assertContains(page, "Generate a new address")
