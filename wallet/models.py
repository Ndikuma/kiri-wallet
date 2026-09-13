import uuid
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone

SATOSHIS_PER_BTC = Decimal("100000000")


class TransactionType(models.TextChoices):
    DEPOSIT = "deposit", "Deposit"
    WITHDRAWAL = "withdrawal", "Withdrawal"
    FEE = "fee", "Platform Fee"
    EXCHANGE_SATS_TO_BIF = "exchange_sats_to_bif", "Exchange: SATS to BIF"
    BIF_TOPUP = "bif_topup", "BIF Top-up (AmatoPay)"
    POS_SETTLEMENT = "pos_settlement", "POS Settlement (BIF)"


class TransactionStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    CONFIRMED = "confirmed", "Confirmed"
    FAILED = "failed", "Failed"
    REFUNDED = "refunded", "Refunded"
    EXPIRED = "expired", "Expired"


class TransactionCurrency(models.TextChoices):
    SATS = "sats", "Satoshis"
    BIF = "bif", "Burundian Franc"


class WithdrawalTargetType(models.TextChoices):
    LIGHTNING_INVOICE = "lightning_invoice", "Lightning Invoice"
    LIGHTNING_ADDRESS = "lightning_address", "Lightning Address"
    LNURL = "lnurl", "LNURL Pay"
    BITCOIN_ADDRESS = "bitcoin_address", "Bitcoin Address"


class WithdrawalFeePolicy(models.Model):
    target_type = models.CharField(
        max_length=40,
        choices=WithdrawalTargetType.choices,
        unique=True,
    )
    display_label = models.CharField(max_length=80)
    fixed_fee_sats = models.PositiveIntegerField(default=0)
    percent_fee_bps = models.PositiveIntegerField(
        default=0,
        help_text="Percentage fee in basis points. 100 bps = 1%.",
    )
    min_fee_sats = models.PositiveIntegerField(default=0)
    max_fee_sats = models.PositiveIntegerField(
        default=0,
        help_text="0 means no maximum cap.",
    )
    charge_to_user = models.BooleanField(
        default=False,
        help_text="When enabled, the fee is deducted from user balance in addition to the withdrawal amount.",
    )
    is_active = models.BooleanField(default=True)
    description = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["target_type"]
        verbose_name = "Withdrawal Fee Policy"
        verbose_name_plural = "Withdrawal Fee Policies"

    def __str__(self):
        return self.display_label or self.get_target_type_display()

    def calculate_fee(self, amount_sats: int) -> int:
        amount = max(int(amount_sats or 0), 0)
        fee = int(self.fixed_fee_sats)
        if self.percent_fee_bps:
            fee += (amount * int(self.percent_fee_bps) + 9999) // 10000
        if fee and self.min_fee_sats:
            fee = max(fee, int(self.min_fee_sats))
        if self.max_fee_sats:
            fee = min(fee, int(self.max_fee_sats))
        return fee

    @classmethod
    def get_for_target_type(cls, target_type: str) -> "WithdrawalFeePolicy":
        defaults = DEFAULT_WITHDRAWAL_FEE_POLICIES.get(target_type, {})
        policy, _ = cls.objects.get_or_create(
            target_type=target_type,
            defaults={
                "display_label": defaults.get("display_label", target_type.replace("_", " ").title()),
                "fixed_fee_sats": defaults.get("fixed_fee_sats", 0),
                "percent_fee_bps": defaults.get("percent_fee_bps", 0),
                "min_fee_sats": defaults.get("min_fee_sats", 0),
                "max_fee_sats": defaults.get("max_fee_sats", 0),
                "charge_to_user": defaults.get("charge_to_user", False),
                "description": defaults.get("description", ""),
            },
        )
        return policy


DEFAULT_WITHDRAWAL_FEE_POLICIES = {
    WithdrawalTargetType.LIGHTNING_INVOICE: {
        "display_label": "Lightning invoice withdrawal",
        "fixed_fee_sats": 0,
        "percent_fee_bps": 100,
        "min_fee_sats": 10,
        "charge_to_user": True,
        "description": "1% withdrawal fee with 10 sats minimum for paying BOLT11 invoices through Blink.",
    },
    WithdrawalTargetType.LIGHTNING_ADDRESS: {
        "display_label": "Lightning address withdrawal",
        "fixed_fee_sats": 0,
        "percent_fee_bps": 100,
        "min_fee_sats": 10,
        "charge_to_user": True,
        "description": "1% withdrawal fee with 10 sats minimum for paying Lightning addresses through Blink.",
    },
    WithdrawalTargetType.LNURL: {
        "display_label": "LNURL withdrawal",
        "fixed_fee_sats": 0,
        "percent_fee_bps": 100,
        "min_fee_sats": 10,
        "charge_to_user": True,
        "description": "1% withdrawal fee with 10 sats minimum for paying static LNURL pay requests through Blink.",
    },
    WithdrawalTargetType.BITCOIN_ADDRESS: {
        "display_label": "Bitcoin on-chain withdrawal",
        "fixed_fee_sats": 0,
        "percent_fee_bps": 100,
        "min_fee_sats": 500,
        "charge_to_user": True,
        "description": "1% withdrawal fee with 500 sats minimum for on-chain Bitcoin withdrawals.",
    },
}


class Wallet(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="wallet")
    is_platform = models.BooleanField(default=False, db_index=True)

    available_balance = models.BigIntegerField(default=0, help_text="Spendable BTC balance, in satoshis.")
    pending_balance = models.BigIntegerField(default=0, help_text="Unconfirmed incoming BTC balance, in satoshis.")
    locked_balance = models.BigIntegerField(default=0, help_text="Sats locked/escrowed, in satoshis.")

    bif_balance = models.BigIntegerField(default=0, help_text="Spendable fiat balance, in whole Burundian Francs.")

    total_deposited = models.BigIntegerField(default=0)
    total_withdrawn = models.BigIntegerField(default=0)
    total_rewarded = models.BigIntegerField(default=0)

    bitcoin_address = models.CharField(
        max_length=120, blank=True, default="",
        db_index=True,
        help_text="On-chain Bitcoin deposit address generated for this user.",
    )

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["user"]),
            models.Index(fields=["is_platform"]),
        ]

    # ── balance bookkeeping helpers ──────────────────────────

    def add_pending_balance(self, amount: int):
        if amount <= 0:
            raise ValueError("Deposit amount must be positive.")
        self.pending_balance += amount
        self.save(update_fields=["pending_balance", "updated_at"])

    def settle(self, amount: int, txn_type: str, **kwargs) -> "WalletTransaction":
        """Create a WalletTransaction row for this wallet."""
        kwargs.setdefault("balance_after", self.available_balance)
        return WalletTransaction.objects.create(
            user=self.user,
            wallet=self,
            type=txn_type,
            amount=amount,
            **kwargs,
        )

    def lock(self, amount: int, **kwargs) -> "WalletTransaction":
        if self.available_balance < amount:
            raise ValueError("Insufficient balance to lock.")
        self.available_balance -= amount
        self.locked_balance += amount
        self.save(update_fields=["available_balance", "locked_balance", "updated_at"])
        return self.settle(amount, TransactionType.FEE, balance_after=self.available_balance, **kwargs)

    def release_locked(self, amount: int, **kwargs) -> "WalletTransaction":
        if self.locked_balance < amount:
            raise ValueError("Insufficient locked balance.")
        self.locked_balance -= amount
        self.available_balance += amount
        self.save(update_fields=["locked_balance", "available_balance", "updated_at"])
        return self.settle(amount, TransactionType.FEE, balance_after=self.available_balance, **kwargs)

    def total_sats(self) -> int:
        return self.available_balance + self.pending_balance + self.locked_balance

    @classmethod
    def get_platform_wallet(cls) -> "Wallet":
        from django.contrib.auth import get_user_model

        User = get_user_model()
        user, created = User.objects.get_or_create(
            email="platform@wallet.local",
            defaults={"username": "wallet-platform", "is_staff": True},
        )
        if created:
            user.set_unusable_password()
            user.save(update_fields=["password"])
        wallet, _ = cls.objects.get_or_create(user=user, defaults={"is_platform": True})
        if not wallet.is_platform:
            wallet.is_platform = True
            wallet.save(update_fields=["is_platform", "updated_at"])
        return wallet

    def __str__(self):
        prefix = "Platform wallet" if self.is_platform else "Wallet"
        return f"{prefix}: {self.user} ({self.available_balance} sats / {self.bif_balance} BIF)"


class WalletTransaction(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="transactions")
    wallet = models.ForeignKey(Wallet, on_delete=models.CASCADE, related_name="transactions")

    type = models.CharField(max_length=30, choices=TransactionType.choices)
    currency = models.CharField(max_length=10, choices=TransactionCurrency.choices, default=TransactionCurrency.SATS)
    status = models.CharField(max_length=20, choices=TransactionStatus.choices, default=TransactionStatus.PENDING)

    amount = models.BigIntegerField(help_text="Always positive; magnitude in `currency` units.")
    balance_after = models.BigIntegerField(null=True, blank=True)

    lnd_invoice = models.CharField(max_length=1000, blank=True, default="")
    lnd_payment_hash = models.CharField(max_length=66, blank=True, default="", db_index=True)
    onchain_address = models.CharField(max_length=120, blank=True, default="")
    onchain_txid = models.CharField(max_length=100, blank=True, default="", db_index=True)
    confirmations = models.PositiveIntegerField(default=0)
    network = models.CharField(max_length=30, blank=True, default="")

    description = models.TextField(blank=True, default="")
    linked_object_type = models.CharField(max_length=50, blank=True, default="")
    linked_object_id = models.CharField(max_length=50, blank=True, default="")

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    settled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["wallet", "-created_at"]),
            models.Index(fields=["status"]),
            models.Index(fields=["type"]),
            models.Index(fields=["onchain_address"]),
            models.Index(fields=["onchain_txid"]),
        ]

    def __str__(self):
        return f"{self.get_type_display()} {self.amount} {self.currency} [{self.status}]"

    @classmethod
    def match_pending_deposit_by_payment_hash(cls, payment_hash: str) -> "WalletTransaction | None":
        return (
            cls.objects
            .filter(lnd_payment_hash=payment_hash, type=TransactionType.DEPOSIT, status=TransactionStatus.PENDING)
            .select_related("user", "wallet")
            .first()
        )


class ExchangeRate(models.Model):
    """Admin-configurable BTC ↔ BIF exchange rate, expressed as BIF per whole BTC."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    bif_per_btc = models.DecimalField(max_digits=20, decimal_places=2)
    source = models.CharField(max_length=40, default="manual", help_text="e.g. manual, coingecko, binance.")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"1 BTC = {self.bif_per_btc} BIF"

    @classmethod
    def current(cls) -> "ExchangeRate":
        rate = cls.objects.filter(is_active=True).order_by("-created_at").first()
        if rate:
            return rate
        return cls.objects.create(bif_per_btc=Decimal("150000000.00"), source="default-seed")

    def sats_to_bif(self, amount_sats: int) -> int:
        btc = Decimal(int(amount_sats)) / SATOSHIS_PER_BTC
        bif = btc * self.bif_per_btc
        return int(bif.quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    def bif_to_sats(self, amount_bif: int) -> int:
        btc = Decimal(int(amount_bif)) / self.bif_per_btc
        sats = btc * SATOSHIS_PER_BTC
        return int(sats.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


class AmatoPayCheckoutSessionStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    CONFIRMED = "confirmed", "Confirmed"
    FAILED = "failed", "Failed"
    EXPIRED = "expired", "Expired"


class AmatoPayCheckoutSession(models.Model):
    """A BIF top-up requested through AmatoPay's hosted checkout."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    wallet = models.ForeignKey(Wallet, on_delete=models.CASCADE, related_name="amatopay_sessions")

    session_id = models.CharField(max_length=64, unique=True, help_text="AmatoPay session_id (UUID).")
    payer_alias = models.CharField(max_length=160)
    amount_bif = models.BigIntegerField()
    checkout_url = models.URLField(max_length=500, blank=True, default="")
    status = models.CharField(
        max_length=20,
        choices=AmatoPayCheckoutSessionStatus.choices,
        default=AmatoPayCheckoutSessionStatus.PENDING,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["wallet", "-created_at"])]

    def __str__(self):
        return f"AmatoPay session {self.session_id} ({self.amount_bif} BIF, {self.status})"


class POSChargeStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    PAID = "paid", "Paid"
    EXPIRED = "expired", "Expired"


class POSCharge(models.Model):
    """A merchant-facing point-of-sale charge: quote a BIF amount, collect it in sats over Lightning."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    wallet = models.ForeignKey(Wallet, on_delete=models.CASCADE, related_name="pos_charges")

    amount_sats = models.BigIntegerField()
    bif_equivalent = models.BigIntegerField()
    rate_bif_per_btc = models.DecimalField(max_digits=20, decimal_places=2)

    payment_hash = models.CharField(max_length=66, blank=True, default="", db_index=True)
    payment_request = models.CharField(max_length=1000, blank=True, default="")
    memo = models.CharField(max_length=255, blank=True, default="")

    status = models.CharField(max_length=20, choices=POSChargeStatus.choices, default=POSChargeStatus.PENDING)

    created_at = models.DateTimeField(auto_now_add=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["wallet", "-created_at"]), models.Index(fields=["payment_hash"])]

    def __str__(self):
        return f"POS charge {self.amount_sats} sats → {self.bif_equivalent} BIF [{self.status}]"


class BitcoinHDWallet(models.Model):
    """
    Singleton: the platform's one BIP32 root extended private key, encrypted
    at rest (Fernet, `WALLET_ENCRYPTION_KEY`). Every on-chain address the
    platform hands out is a deterministic child of this one root key
    (m/44'/coin_type'/0'/0/<next_index>) — decrypted in-process only to
    derive a specific child key when signing a withdrawal.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    encrypted_root_xprv = models.TextField(help_text="Fernet-encrypted BIP32 root xprv.")
    network = models.CharField(max_length=20, help_text="btclib network name this root key was created for.")
    next_index = models.PositiveIntegerField(default=0, help_text="Next unused BIP44 address_index to derive.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Bitcoin HD wallet (singleton)"

    def __str__(self):
        return f"BitcoinHDWallet({self.network}, next_index={self.next_index})"


class PlatformBitcoinAddress(models.Model):
    """One address derived from `BitcoinHDWallet`, for a user deposit or internal change."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    address = models.CharField(max_length=120, unique=True, db_index=True)
    derivation_index = models.PositiveIntegerField(unique=True)
    label = models.CharField(max_length=100, blank=True, default="", help_text="e.g. user-<wallet_id> or change.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["derivation_index"]

    def __str__(self):
        return f"{self.address} (#{self.derivation_index}, {self.label or 'unlabeled'})"
