import uuid

from django.conf import settings
from django.db import models


class TransactionType(models.TextChoices):
    DEPOSIT = "deposit", "Deposit"
    WITHDRAWAL = "withdrawal", "Withdrawal"


class TransactionStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    CONFIRMED = "confirmed", "Confirmed"
    FAILED = "failed", "Failed"
    EXPIRED = "expired", "Expired"


class Wallet(models.Model):
    """One wallet per user. Balances are stored in satoshis (integers) to avoid float rounding issues."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="wallet")

    available_balance = models.BigIntegerField(default=0, help_text="Spendable balance, in satoshis.")
    pending_balance = models.BigIntegerField(default=0, help_text="Unconfirmed incoming balance, in satoshis.")

    bitcoin_address = models.CharField(max_length=120, blank=True, default="")
    lightning_address = models.CharField(max_length=255, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Wallet({self.user})"


class Transaction(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    wallet = models.ForeignKey(Wallet, on_delete=models.CASCADE, related_name="transactions")

    type = models.CharField(max_length=20, choices=TransactionType.choices)
    status = models.CharField(max_length=20, choices=TransactionStatus.choices, default=TransactionStatus.PENDING)

    amount_sats = models.BigIntegerField(help_text="Transaction amount in satoshis (always positive).")
    balance_after = models.BigIntegerField(null=True, blank=True)

    lightning_invoice = models.CharField(max_length=1000, blank=True, default="")
    payment_hash = models.CharField(max_length=66, blank=True, default="", db_index=True)
    onchain_address = models.CharField(max_length=120, blank=True, default="")
    onchain_txid = models.CharField(max_length=100, blank=True, default="", db_index=True)

    memo = models.CharField(max_length=255, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    settled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["wallet", "-created_at"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self):
        return f"{self.get_type_display()} {self.amount_sats} sats [{self.status}]"
