from rest_framework import serializers

from .models import Transaction, Wallet


class WalletSerializer(serializers.ModelSerializer):
    class Meta:
        model = Wallet
        fields = [
            "id",
            "available_balance",
            "pending_balance",
            "bitcoin_address",
            "lightning_address",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class TransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Transaction
        fields = [
            "id",
            "type",
            "status",
            "amount_sats",
            "balance_after",
            "lightning_invoice",
            "payment_hash",
            "onchain_address",
            "onchain_txid",
            "memo",
            "created_at",
            "settled_at",
        ]
        read_only_fields = fields


class CreateDepositSerializer(serializers.Serializer):
    amount_sats = serializers.IntegerField(min_value=1)
    memo = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")


class CreateWithdrawalSerializer(serializers.Serializer):
    amount_sats = serializers.IntegerField(min_value=1)
    lightning_invoice = serializers.CharField(max_length=1000, required=False, allow_blank=True, default="")
    onchain_address = serializers.CharField(max_length=120, required=False, allow_blank=True, default="")

    def validate(self, attrs):
        if not attrs.get("lightning_invoice") and not attrs.get("onchain_address"):
            raise serializers.ValidationError(
                "Provide either lightning_invoice or onchain_address as the withdrawal destination."
            )
        return attrs
